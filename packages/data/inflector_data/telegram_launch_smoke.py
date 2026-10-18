"""Explicit, operator-authorized one-delivery Telegram launch smoke."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.model_release import (
    ModelReleaseIntegrityError,
    project_release_score_for_cycle,
    resolve_cycle_release_context,
)
from inflector_data.model_release_policy import ModelReleasePolicy
from inflector_data.notification_delivery_policy import (
    NotificationDeliveryPolicy,
    canonical_json_sha256,
)
from inflector_data.research_notification_delivery import (
    deliver_claimed_notification,
    delivery_key,
)
from inflector_data.telegram_notification_transport import TelegramBotApiTransport
from inflector_database.models import (
    ModelReleaseProductionScore,
    OpportunityChangeItem,
    OpportunityDiscoveryItem,
    ResearchNotificationDelivery,
)
from inflector_database.research_notification_delivery_repository import (
    ResearchNotificationDeliveryRepository,
)

SMOKE_FLAG = "INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE"
TOKEN_VARIABLE = "INFLECTOR_TELEGRAM_BOT_TOKEN"
CHAT_VARIABLE = "INFLECTOR_TELEGRAM_CHAT_ID"


class TelegramLaunchSmokeError(RuntimeError):
    """Raised before network I/O when the reviewed smoke cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class TelegramLaunchSmokeResult:
    status: str
    delivery_id: str
    delivery_key_sha256: str
    outbox_id: str
    notification_key_sha256: str
    attempt_number: int
    provider_message_id: str | None
    started_at: str | None
    completed_at: str | None
    delivery_policy_code: str
    delivery_policy_checksum_sha256: str
    release_provenance: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe, credential-free operator result."""

        return asdict(self)


def run_operator_telegram_smoke(
    session: Session,
    *,
    delivery_id: UUID,
    policy: NotificationDeliveryPolicy,
    release_policy: ModelReleasePolicy,
    environment: Mapping[str, str] | None = None,
    transport: TelegramBotApiTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> TelegramLaunchSmokeResult:
    """Attempt exactly one explicitly reviewed existing Production O delivery."""

    values = environment if environment is not None else os.environ
    bot_token, chat_id = _authorized_credentials(values)
    now = clock or (lambda: datetime.now(UTC))
    started_at = _utc(now(), "Telegram launch smoke start")
    repository = ResearchNotificationDeliveryRepository(session)
    delivery = repository.get(delivery_id)
    if delivery is None:
        raise TelegramLaunchSmokeError("reviewed notification delivery is unavailable")
    release_provenance = _validate_reviewed_delivery(
        session,
        delivery=delivery,
        policy=policy,
        release_policy=release_policy,
    )
    if delivery.status == "delivered":
        return _result(
            delivery,
            status="already_delivered",
            attempt_number=delivery.attempt_count,
            provider_message_id=delivery.provider_message_id,
            started_at=None,
            completed_at=delivery.delivered_at,
            policy=policy,
            release_provenance=release_provenance,
        )
    _require_attempt_eligible(delivery, at=started_at, maximum_attempts=policy.maximum_attempts)
    worker_token = f"launch-smoke:{delivery.id}"
    claim = repository.claim_one(
        delivery_id=delivery.id,
        worker_token=worker_token,
        now=started_at,
        lease_seconds=policy.claim_lease_seconds,
        maximum_attempts=policy.maximum_attempts,
    )
    if claim is None:
        session.rollback()
        raise TelegramLaunchSmokeError("reviewed notification delivery could not be claimed")
    attempt = deliver_claimed_notification(
        session,
        repository=repository,
        policy=policy,
        transport=transport or TelegramBotApiTransport(),
        claim=claim,
        bot_token=bot_token,
        chat_id=chat_id,
        worker_token=worker_token,
        started_at=started_at,
        clock=now,
    )
    persisted = repository.get(delivery.id)
    if persisted is None:
        raise TelegramLaunchSmokeError("delivery disappeared after the smoke attempt")
    return _result(
        persisted,
        status=attempt.result_state,
        attempt_number=attempt.attempt_number,
        provider_message_id=attempt.provider_message_id,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        policy=policy,
        release_provenance=release_provenance,
    )


def _authorized_credentials(environment: Mapping[str, str]) -> tuple[str, str]:
    if environment.get(SMOKE_FLAG) != "1":
        raise TelegramLaunchSmokeError("live Telegram launch smoke is not explicitly authorized")
    token = environment.get(TOKEN_VARIABLE, "").strip()
    chat_id = environment.get(CHAT_VARIABLE, "").strip()
    if not token or not chat_id:
        raise TelegramLaunchSmokeError("Telegram launch smoke credentials are unavailable")
    return token, chat_id


def _validate_reviewed_delivery(
    session: Session,
    *,
    delivery: ResearchNotificationDelivery,
    policy: NotificationDeliveryPolicy,
    release_policy: ModelReleasePolicy,
) -> tuple[dict[str, str], ...]:
    outbox = delivery.outbox
    if (
        delivery.delivery_policy_code != policy.code
        or delivery.delivery_policy_checksum_sha256 != policy.checksum_sha256
        or delivery.transport_code != policy.transport_code
        or delivery.target_code != policy.target_code
        or delivery.rendering_version != policy.rendering_version
        or delivery.notification_key_sha256 != outbox.notification_key_sha256
        or delivery.delivery_key_sha256 != delivery_key(outbox, policy)
    ):
        raise TelegramLaunchSmokeError("reviewed delivery identity failed Production O validation")
    change = session.get(OpportunityChangeItem, outbox.source_change_item_id)
    change_run = outbox.source_change_run
    if (
        change is None
        or change.opportunity_change_run_id != outbox.source_change_run_id
        or change.id != outbox.source_change_item_id
        or change_run.id != outbox.source_change_run_id
        or change_run.status != "completed"
    ):
        raise TelegramLaunchSmokeError("reviewed delivery does not bind its Production N source")
    matched = tuple(
        value for value in outbox.matched_trigger_codes_json if isinstance(value, str)
    )
    expected_notification_key = canonical_json_sha256(
        {
            "alert_policy_checksum_sha256": outbox.alert_policy_checksum_sha256,
            "source_change_run_id": str(change_run.id),
            "source_change_run_key_sha256": change_run.run_key_sha256,
            "source_change_item_id": str(change.id),
            "matched_trigger_codes": list(matched),
            "payload_schema_version": outbox.payload_schema_version,
        }
    )
    if (
        len(matched) != len(outbox.matched_trigger_codes_json)
        or len(matched) != len(set(matched))
        or outbox.notification_key_sha256 != expected_notification_key
    ):
        raise TelegramLaunchSmokeError("reviewed Production N outbox identity failed validation")
    provenance: list[dict[str, str]] = []
    for item_id in (change.baseline_discovery_item_id, change.current_discovery_item_id):
        if item_id is None:
            continue
        discovery = session.get(OpportunityDiscoveryItem, item_id)
        if discovery is None:
            raise TelegramLaunchSmokeError("notification discovery provenance is unavailable")
        if discovery.model_release_production_score_id is None:
            continue
        provenance.append(
            _validate_release_score(
                session,
                discovery=discovery,
                release_policy=release_policy,
            )
        )
    return tuple(provenance)


def _validate_release_score(
    session: Session,
    *,
    discovery: OpportunityDiscoveryItem,
    release_policy: ModelReleasePolicy,
) -> dict[str, str]:
    score = session.get(
        ModelReleaseProductionScore,
        discovery.model_release_production_score_id,
    )
    if (
        score is None
        or discovery.score_snapshot_id != score.source_score_snapshot_id
        or discovery.score_snapshot is None
        or discovery.model_release_production_score is None
    ):
        raise TelegramLaunchSmokeError("notification release-score provenance is incomplete")
    try:
        context = resolve_cycle_release_context(
            session,
            policy=release_policy,
            operational_run_id=score.operational_run_id,
        )
        validated = project_release_score_for_cycle(
            session,
            policy=release_policy,
            context=context,
            snapshot=discovery.score_snapshot,
        )
    except ModelReleaseIntegrityError as error:
        raise TelegramLaunchSmokeError(
            "notification release-score provenance failed integrity validation"
        ) from error
    if validated.id != score.id:
        raise TelegramLaunchSmokeError("notification release-score identity changed")
    return {
        "operational_run_id": str(score.operational_run_id),
        "cycle_release_binding_id": str(score.cycle_release_binding_id),
        "deployment_id": str(score.deployment_id),
        "release_candidate_id": str(score.release_candidate_id),
        "subject_code": score.subject_code,
        "projection_fingerprint_sha256": score.projection_fingerprint_sha256,
    }


def _require_attempt_eligible(
    delivery: ResearchNotificationDelivery,
    *,
    at: datetime,
    maximum_attempts: int,
) -> None:
    if delivery.status == "dead_letter":
        raise TelegramLaunchSmokeError("reviewed delivery is terminal dead-letter")
    if delivery.attempt_count >= maximum_attempts:
        raise TelegramLaunchSmokeError("reviewed delivery exhausted its accepted attempts")
    if delivery.status == "claimed" and (
        delivery.claim_expires_at is None or _stored_utc(delivery.claim_expires_at) > at
    ):
        raise TelegramLaunchSmokeError("reviewed delivery has an active lease")
    if delivery.status == "retry_wait" and (
        delivery.next_attempt_at is None or _stored_utc(delivery.next_attempt_at) > at
    ):
        raise TelegramLaunchSmokeError("reviewed delivery retry is not yet due")
    if delivery.status not in {"pending", "claimed", "retry_wait"}:
        raise TelegramLaunchSmokeError("reviewed delivery state is not attempt-eligible")


def _result(
    delivery: ResearchNotificationDelivery,
    *,
    status: str,
    attempt_number: int,
    provider_message_id: str | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    policy: NotificationDeliveryPolicy,
    release_provenance: tuple[dict[str, str], ...],
) -> TelegramLaunchSmokeResult:
    return TelegramLaunchSmokeResult(
        status=status,
        delivery_id=str(delivery.id),
        delivery_key_sha256=delivery.delivery_key_sha256,
        outbox_id=str(delivery.outbox_id),
        notification_key_sha256=delivery.notification_key_sha256,
        attempt_number=attempt_number,
        provider_message_id=provider_message_id,
        started_at=started_at.isoformat() if started_at is not None else None,
        completed_at=completed_at.isoformat() if completed_at is not None else None,
        delivery_policy_code=policy.code,
        delivery_policy_checksum_sha256=policy.checksum_sha256,
        release_provenance=release_provenance,
    )


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise TelegramLaunchSmokeError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
