"""Production O preparation, claiming, and delivery lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from inflector_data.notification_delivery_policy import (
    NotificationDeliveryPolicy,
    canonical_json_sha256,
)
from inflector_data.telegram_notification_renderer import (
    render_telegram_notification,
)
from inflector_data.telegram_notification_transport import (
    TelegramBotApiTransport,
    TelegramTransportResult,
)
from inflector_database.models import (
    ResearchNotificationDelivery,
    ResearchNotificationOutbox,
)
from inflector_database.research_notification_delivery_repository import (
    ResearchNotificationDeliveryRepository,
    ResearchNotificationDeliveryWrite,
)


@dataclass(frozen=True, slots=True)
class DeliveryPreparationResult:
    outbox_rows_considered: int
    delivery_rows_created: int
    delivery_rows_reused: int


@dataclass(frozen=True, slots=True)
class DeliveryDrainResult:
    preparation: DeliveryPreparationResult
    attempts_started: int
    delivered: int
    retry_wait: int
    dead_letter: int
    backlog_count: int
    due_count: int


def delivery_key(
    outbox: ResearchNotificationOutbox, policy: NotificationDeliveryPolicy
) -> str:
    """Bind one immutable N event to one policy, transport, target, and renderer."""

    _validate_outbox_policy(outbox, policy)
    return canonical_json_sha256(
        {
            "notification_key_sha256": outbox.notification_key_sha256,
            "outbox_id": str(outbox.id),
            "delivery_policy_checksum_sha256": policy.checksum_sha256,
            "transport_code": policy.transport_code,
            "target_code": policy.target_code,
            "rendering_version": policy.rendering_version,
        }
    )


def prepare_notification_deliveries(
    session: Session,
    *,
    policy: NotificationDeliveryPolicy,
    limit: int | None = None,
) -> DeliveryPreparationResult:
    """Create missing delivery identities in neutral N-outbox chronology."""

    maximum = limit if limit is not None else policy.maximum_messages_per_cycle
    if not 1 <= maximum <= policy.maximum_messages_per_cycle:
        raise ValueError("delivery preparation limit exceeds policy bound")
    already_prepared = exists().where(
        and_(
            ResearchNotificationDelivery.outbox_id == ResearchNotificationOutbox.id,
            ResearchNotificationDelivery.delivery_policy_checksum_sha256
            == policy.checksum_sha256,
            ResearchNotificationDelivery.transport_code == policy.transport_code,
            ResearchNotificationDelivery.target_code == policy.target_code,
            ResearchNotificationDelivery.rendering_version == policy.rendering_version,
        )
    )
    outbox_rows = tuple(
        session.scalars(
            select(ResearchNotificationOutbox)
            .where(
                ResearchNotificationOutbox.delivery_status == "pending",
                ResearchNotificationOutbox.alert_policy_code
                == policy.research_alert_policy_code,
                ResearchNotificationOutbox.alert_policy_checksum_sha256
                == policy.research_alert_policy_checksum_sha256,
                ~already_prepared,
            )
            .order_by(
                ResearchNotificationOutbox.created_at.asc(),
                ResearchNotificationOutbox.notification_key_sha256.asc(),
            )
            .limit(maximum)
        )
    )
    repository = ResearchNotificationDeliveryRepository(session)
    created = 0
    reused = 0
    for outbox in outbox_rows:
        _, was_created = repository.create_or_reuse(
            ResearchNotificationDeliveryWrite(
                delivery_key_sha256=delivery_key(outbox, policy),
                outbox_id=outbox.id,
                notification_key_sha256=outbox.notification_key_sha256,
                delivery_policy_code=policy.code,
                delivery_policy_checksum_sha256=policy.checksum_sha256,
                transport_code=policy.transport_code,
                target_code=policy.target_code,
                rendering_version=policy.rendering_version,
            )
        )
        created += int(was_created)
        reused += int(not was_created)
    return DeliveryPreparationResult(len(outbox_rows), created, reused)


def drain_notification_deliveries(
    session: Session,
    *,
    policy: NotificationDeliveryPolicy,
    transport: TelegramBotApiTransport,
    bot_token: str,
    chat_id: str,
    worker_token: str,
    clock: Callable[[], datetime] | None = None,
) -> DeliveryDrainResult:
    """Prepare and process due backlog with network calls outside transactions."""

    now = clock or (lambda: datetime.now(UTC))
    preparation = prepare_notification_deliveries(session, policy=policy)
    session.commit()
    attempts = delivered = retry_wait = dead_letter = 0
    repository = ResearchNotificationDeliveryRepository(session)
    while attempts < policy.maximum_messages_per_cycle:
        started_at = _utc(now(), "delivery attempt start")
        claim = repository.claim_next(
            worker_token=worker_token,
            now=started_at,
            lease_seconds=policy.claim_lease_seconds,
            maximum_attempts=policy.maximum_attempts,
        )
        if claim is None:
            session.commit()
            break
        delivery = repository.get(claim.delivery_id)
        if delivery is None:
            raise RuntimeError("claimed notification delivery is unavailable")
        message = render_telegram_notification(
            delivery.outbox,
            delivery_key_sha256=delivery.delivery_key_sha256,
            rendering_version=delivery.rendering_version,
        )
        # Transaction A freezes the claim and started attempt before any network I/O.
        session.commit()
        result = transport.send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=message,
            timeout_seconds=policy.network_timeout_seconds,
        )
        completed_at = _utc(now(), "delivery attempt completion")
        attempts += 1
        if result.ok and result.provider_message_id is not None:
            repository.record_delivered(
                claim=claim,
                worker_token=worker_token,
                completed_at=completed_at,
                provider_message_id=result.provider_message_id,
                http_status=200,
            )
            delivered += 1
        else:
            retryable, error_code = _failure_semantics(policy, result)
            delay = None
            if retryable and claim.attempt_number < policy.maximum_attempts:
                delay = policy.retry_delay_seconds(claim.attempt_number)
                if result.retry_after_seconds is not None:
                    delay = max(delay, result.retry_after_seconds)
            repository.record_failure(
                claim=claim,
                worker_token=worker_token,
                completed_at=completed_at,
                retryable=retryable,
                retry_delay_seconds=delay,
                maximum_attempts=policy.maximum_attempts,
                error_code=error_code,
                detail=_safe_failure_detail(result),
                http_status=result.http_status,
                telegram_error_code=result.telegram_error_code,
            )
            terminal = not retryable or claim.attempt_number >= policy.maximum_attempts
            dead_letter += int(terminal)
            retry_wait += int(not terminal)
        # Transaction B records the provider result and state transition.
        session.commit()
    observed_at = _utc(now(), "delivery drain summary")
    return DeliveryDrainResult(
        preparation=preparation,
        attempts_started=attempts,
        delivered=delivered,
        retry_wait=retry_wait,
        dead_letter=dead_letter,
        backlog_count=repository.backlog_count(),
        due_count=repository.due_count(now=observed_at),
    )


def _failure_semantics(
    policy: NotificationDeliveryPolicy, result: TelegramTransportResult
) -> tuple[bool, str]:
    error_code = result.error_code or "telegram_unverified_response"
    http_status = result.http_status
    telegram_code = result.telegram_error_code
    if (
        http_status in policy.permanent_http_statuses
        or telegram_code in policy.permanent_telegram_error_codes
    ):
        return False, error_code
    if (
        http_status is not None and policy.is_retryable_http_status(http_status)
    ) or (
        telegram_code is not None
        and policy.is_retryable_telegram_error_code(telegram_code)
    ):
        return True, error_code
    if http_status is None:
        return True, error_code
    if result.malformed_response:
        return True, error_code
    return False, "telegram_permanent_provider_failure"


def _safe_failure_detail(result: TelegramTransportResult) -> dict[str, object]:
    return {
        "classification": (
            "malformed_response" if result.malformed_response else "provider_failure"
        ),
        "retry_after_seconds": result.retry_after_seconds,
    }


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_outbox_policy(
    outbox: ResearchNotificationOutbox, policy: NotificationDeliveryPolicy
) -> None:
    if (
        outbox.alert_policy_code != policy.research_alert_policy_code
        or outbox.alert_policy_checksum_sha256
        != policy.research_alert_policy_checksum_sha256
    ):
        raise ValueError("notification outbox does not match the bound N policy")
