"""Operator-controlled, exact-delivery Telegram launch smoke contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_data.model_release_policy import load_model_release_policy
from inflector_data.notification_delivery_operations import NotificationDeliveryOrchestrator
from inflector_data.notification_delivery_policy import (
    canonical_json_sha256,
    load_notification_delivery_policy,
)
from inflector_data.operations_profile import load_operations_profile
from inflector_data.research_notification_delivery import prepare_notification_deliveries
from inflector_data.telegram_launch_smoke import (
    TelegramLaunchSmokeError,
    run_operator_telegram_smoke,
)
from inflector_data.telegram_notification_transport import (
    TelegramBotApiTransport,
    TelegramTransportResult,
)
from inflector_database.models import (
    OpportunityChangeItem,
    OpportunityChangeRun,
    ResearchNotificationDelivery,
    ResearchNotificationDeliveryAttempt,
    ResearchNotificationOutbox,
)
from inflector_database.research_notification_delivery_repository import (
    ResearchNotificationDeliveryRepository,
)

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
N_CHECKSUM = "e3d1a65eaa80017760d646ed8b03cb7fcf1d0107903c935c70be627a82e33619"
AUTH = {
    "INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE": "1",
    "INFLECTOR_TELEGRAM_BOT_TOKEN": "runtime-token",
    "INFLECTOR_TELEGRAM_CHAT_ID": "runtime-chat",
}


class _Transport(TelegramBotApiTransport):
    def __init__(self, results: list[TelegramTransportResult]) -> None:
        self.results = results
        self.calls = 0

    def send_message(
        self, *, bot_token: str, chat_id: str, text: str, timeout_seconds: int
    ) -> TelegramTransportResult:
        assert bot_token == "runtime-token"
        assert chat_id == "runtime-chat"
        assert text and timeout_seconds == 10
        self.calls += 1
        return self.results.pop(0)


def _success(message_id: str = "launch-1") -> TelegramTransportResult:
    return TelegramTransportResult(200, True, message_id, None, None, None, False)


def _failure() -> TelegramTransportResult:
    return TelegramTransportResult(
        500,
        False,
        None,
        500,
        None,
        "fixture_failure",
        False,
    )


def _policy():
    return load_notification_delivery_policy(
        ROOT / "config/notifications/production_notification_delivery_v1.json",
        repository_root=ROOT,
    )


def _release_policy():
    return load_model_release_policy(
        ROOT / "config/operations/production_model_release_v1.json"
    )


def _delivery(session: Session, token: str) -> ResearchNotificationDelivery:
    change_run = OpportunityChangeRun(
        run_key_sha256=f"{token}-change-run".ljust(64, "r")[:64],
        change_policy_code="fixture-change-policy",
        change_policy_checksum_sha256="c" * 64,
        baseline_discovery_run_id=uuid4(),
        current_discovery_run_id=uuid4(),
        baseline_run_key_sha256="b" * 64,
        current_run_key_sha256="d" * 64,
        baseline_snapshot_set_checksum_sha256="e" * 64,
        current_snapshot_set_checksum_sha256="f" * 64,
        comparison_version="fixture-change-v1",
        inputs_json={},
        status="completed",
        summary_json={},
        completed_at=NOW,
    )
    session.add(change_run)
    session.flush()
    change_item = OpportunityChangeItem(
        opportunity_change_run_id=change_run.id,
        identity_key=f"{token}-identity",
        identity_basis="fixture",
        company_id=None,
        security_id=None,
        baseline_symbol=token,
        current_symbol=token,
        baseline_discovery_item_id=None,
        current_discovery_item_id=None,
        baseline_score_snapshot_id=None,
        current_score_snapshot_id=None,
        change_codes_json=["score_increased"],
        changed=True,
        baseline_rankable=None,
        current_rankable=True,
        baseline_unranked_reason=None,
        current_unranked_reason=None,
        baseline_final_score=None,
        current_final_score=None,
        score_delta=None,
        baseline_score_rank=None,
        current_score_rank=None,
        rank_delta=None,
        baseline_confidence=None,
        current_confidence=None,
        confidence_delta=None,
        components_gained_json=[],
        components_lost_json=[],
        component_change_detail_json={},
        detail_json={},
    )
    session.add(change_item)
    session.flush()
    notification_key = canonical_json_sha256(
        {
            "alert_policy_checksum_sha256": N_CHECKSUM,
            "source_change_run_id": str(change_run.id),
            "source_change_run_key_sha256": change_run.run_key_sha256,
            "source_change_item_id": str(change_item.id),
            "matched_trigger_codes": ["score_increased"],
            "payload_schema_version": "research_notification_payload_v1",
        }
    )
    outbox = ResearchNotificationOutbox(
        notification_key_sha256=notification_key,
        alert_policy_code="production_research_alert_policy_v1",
        alert_policy_checksum_sha256=N_CHECKSUM,
        payload_schema_version="research_notification_payload_v1",
        source_change_run_id=change_run.id,
        source_change_item_id=change_item.id,
        company_id=None,
        security_id=None,
        baseline_symbol=token,
        current_symbol=token,
        matched_trigger_codes_json=["score_increased"],
        payload_json={
            "current_final_score": "70.25",
            "current_score_rank": 8,
            "matched_trigger_codes": ["score_increased"],
            "score_delta": None,
        },
        delivery_status="pending",
    )
    session.add(outbox)
    session.flush()
    prepare_notification_deliveries(session, policy=_policy())
    delivery = session.scalar(
        select(ResearchNotificationDelivery).where(
            ResearchNotificationDelivery.outbox_id == outbox.id
        )
    )
    assert delivery is not None
    return delivery


def _smoke(
    session: Session,
    delivery: ResearchNotificationDelivery,
    transport: _Transport,
    *,
    environment: dict[str, str] | None = None,
    at: datetime = NOW,
):
    return run_operator_telegram_smoke(
        session,
        delivery_id=delivery.id,
        policy=_policy(),
        release_policy=_release_policy(),
        environment=AUTH if environment is None else environment,
        transport=transport,
        clock=lambda: at,
    )


@pytest.mark.parametrize(
    "environment",
    (
        {},
        {**AUTH, "INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE": "0"},
        {**AUTH, "INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE": "yes"},
        {key: value for key, value in AUTH.items() if key != "INFLECTOR_TELEGRAM_BOT_TOKEN"},
        {key: value for key, value in AUTH.items() if key != "INFLECTOR_TELEGRAM_CHAT_ID"},
    ),
)
def test_smoke_authorization_fails_before_network(
    session: Session, environment: dict[str, str]
) -> None:
    delivery = _delivery(session, "AUTH")
    transport = _Transport([_success()])
    with pytest.raises(TelegramLaunchSmokeError):
        _smoke(session, delivery, transport, environment=environment)
    assert transport.calls == 0
    assert delivery.status == "pending" and delivery.attempt_count == 0


def test_smoke_sends_exact_reviewed_delivery_and_leaves_backlog_untouched(
    session: Session,
) -> None:
    selected = _delivery(session, "SELECTED")
    other_one = _delivery(session, "OTHER1")
    other_two = _delivery(session, "OTHER2")
    transport = _Transport([_success("provider-88")])
    result = _smoke(session, selected, transport)
    assert transport.calls == 1
    assert result.status == "delivered" and result.provider_message_id == "provider-88"
    session.refresh(other_one)
    session.refresh(other_two)
    assert (other_one.status, other_one.attempt_count) == ("pending", 0)
    assert (other_two.status, other_two.attempt_count) == ("pending", 0)
    attempt = session.scalar(
        select(ResearchNotificationDeliveryAttempt).where(
            ResearchNotificationDeliveryAttempt.delivery_id == selected.id
        )
    )
    assert attempt is not None and attempt.provider_message_id == "provider-88"


def test_foreign_delivery_and_already_delivered_are_never_sent(session: Session) -> None:
    transport = _Transport([_success()])
    with pytest.raises(TelegramLaunchSmokeError):
        run_operator_telegram_smoke(
            session,
            delivery_id=uuid4(),
            policy=_policy(),
            release_policy=_release_policy(),
            environment=AUTH,
            transport=transport,
            clock=lambda: NOW,
        )
    assert transport.calls == 0
    delivery = _delivery(session, "ONCE")
    first = _smoke(session, delivery, transport)
    repeated = _smoke(session, delivery, transport)
    assert first.status == "delivered"
    assert repeated.status == "already_delivered"
    assert transport.calls == 1


def test_active_lease_and_not_due_retry_fail_closed(session: Session) -> None:
    leased = _delivery(session, "LEASED")
    claim = ResearchNotificationDeliveryRepository(session).claim_one(
        delivery_id=leased.id,
        worker_token="other-worker",
        now=NOW,
        lease_seconds=300,
        maximum_attempts=5,
    )
    assert claim is not None
    session.commit()
    transport = _Transport([_success()])
    with pytest.raises(TelegramLaunchSmokeError, match="active lease"):
        _smoke(session, leased, transport)

    waiting = _delivery(session, "WAITING")
    waiting.status = "retry_wait"
    waiting.attempt_count = 1
    waiting.next_attempt_at = NOW + timedelta(seconds=60)
    session.commit()
    with pytest.raises(TelegramLaunchSmokeError, match="not yet due"):
        _smoke(session, waiting, transport)
    assert transport.calls == 0


def test_due_retry_uses_normal_failure_and_success_lifecycle(session: Session) -> None:
    delivery = _delivery(session, "RETRY")
    failing = _Transport([_failure()])
    failed = _smoke(session, delivery, failing)
    assert failed.status == "retry_wait"
    session.refresh(delivery)
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at is not None
    assert delivery.next_attempt_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=60)
    succeeding = _Transport([_success("provider-retry")])
    succeeded = _smoke(
        session,
        delivery,
        succeeding,
        at=NOW + timedelta(seconds=60),
    )
    assert succeeded.status == "delivered" and succeeded.attempt_number == 2
    assert succeeding.calls == 1
    assert session.scalar(
        select(func.count())
        .select_from(ResearchNotificationDeliveryAttempt)
        .where(ResearchNotificationDeliveryAttempt.delivery_id == delivery.id)
    ) == 2


def test_smoke_summary_is_secret_free(session: Session) -> None:
    delivery = _delivery(session, "SAFE")
    result = _smoke(session, delivery, _Transport([_success()])).as_dict()
    serialized = json.dumps(result, sort_keys=True)
    assert "runtime-token" not in serialized
    assert "runtime-chat" not in serialized
    assert set(result) == {
        "status",
        "delivery_id",
        "delivery_key_sha256",
        "outbox_id",
        "notification_key_sha256",
        "attempt_number",
        "provider_message_id",
        "started_at",
        "completed_at",
        "delivery_policy_code",
        "delivery_policy_checksum_sha256",
        "release_provenance",
    }


def test_normal_orchestrator_does_not_require_launch_smoke_flag(session: Session) -> None:
    _delivery(session, "NORMAL")
    profile = load_operations_profile(
        ROOT / "config/operations/production_operations_v6.json"
    )

    class _Plan:
        operations_profile = profile
        run_key_sha256 = "a" * 64

    environment = {
        "INFLECTOR_TELEGRAM_BOT_TOKEN": "runtime-token",
        "INFLECTOR_TELEGRAM_CHAT_ID": "runtime-chat",
    }
    transport = _Transport([_success("ordinary-o")])
    result, succeeded = NotificationDeliveryOrchestrator(
        ROOT,
        environment=environment,
        clock=lambda: NOW,
        transport=transport,
    ).execute(session, _Plan())
    assert succeeded and result["delivered"] == 1
    assert transport.calls == 1


def test_normal_orchestrator_and_scheduler_paths_do_not_depend_on_smoke() -> None:
    operations = (
        ROOT / "packages/data/inflector_data/notification_delivery_operations.py"
    ).read_text(encoding="utf-8")
    production_cycle = (ROOT / "packages/data/inflector_data/production_operations.py").read_text(
        encoding="utf-8"
    )
    scheduler = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((ROOT / "scripts").glob("*.ps1"))
    )
    assert "INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE" not in operations
    assert "telegram_launch_smoke" not in production_cycle
    assert "telegram_launch_smoke" not in scheduler
