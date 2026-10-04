"""Production P portability and read-only operator health regressions."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from inflector_data.ops_cli import main as operations_main
from inflector_data.production_health import (
    ACCEPTED_CHECKSUMS,
    accepted_asset_readiness,
    production_health,
)
from inflector_database.models import (
    ModelVersion,
    OperationalRun,
    ResearchNotificationDelivery,
    ResearchNotificationOutbox,
)

ROOT = Path(__file__).parents[1]
OBSERVED_AT = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_accepted_runtime_chain_assets_and_checksums_are_ready() -> None:
    readiness = accepted_asset_readiness(ROOT)

    assert set(readiness) == set(ACCEPTED_CHECKSUMS)
    for name, expected in ACCEPTED_CHECKSUMS.items():
        item = cast(dict[str, object], readiness[name])
        assert item == {
            "status": "ready",
            "code": item["code"],
            "checksum_sha256": expected,
        }


def test_health_distinguishes_missing_configuration_without_secrets(
    monkeypatch,
) -> None:
    token = "secret-bot-token"
    chat_id = "secret-chat-id"
    monkeypatch.setenv("INFLECTOR_TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("INFLECTOR_TELEGRAM_CHAT_ID", chat_id)

    report = production_health(
        repository_root=ROOT,
        observed_at=OBSERVED_AT,
        session=None,
        raw_root=None,
        gdelt_raw_root=None,
        model_family=None,
        telegram_bot_token_present=True,
        telegram_chat_id_present=True,
    )
    encoded = json.dumps(report, default=str, sort_keys=True)

    assert report["status"] == "configuration_unavailable"
    assert report["database"] == {"status": "configuration_unavailable"}
    assert report["credentials"] == {
        "telegram_bot_token": "available",
        "telegram_chat_id": "available",
    }
    assert token not in encoded
    assert chat_id not in encoded


def test_database_connected_health_reports_state_and_is_read_only(
    session: Session, tmp_path: Path
) -> None:
    _migration(session)
    model = ModelVersion(
        model_family="accepted_model",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="active",
        activated_at=OBSERVED_AT - timedelta(days=2),
    )
    latest_run = OperationalRun(
        run_key_sha256="1" * 64,
        operations_profile_code="nse_daily_operations_v6",
        operations_profile_checksum_sha256=ACCEPTED_CHECKSUMS["operations_v6"],
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256=ACCEPTED_CHECKSUMS["research_v4"],
        model_family="accepted_model",
        fiscal_year=2026,
        fiscal_quarter=2,
        cycle_at=OBSERVED_AT - timedelta(hours=1),
        knowledge_cutoff=OBSERVED_AT - timedelta(hours=1),
        symbol_set_checksum_sha256="2" * 64,
        ordered_symbols_json=["AAA"],
        inputs_json={},
        status="completed",
        planned_at=OBSERVED_AT - timedelta(hours=2),
        started_at=OBSERVED_AT - timedelta(hours=2),
        completed_at=OBSERVED_AT - timedelta(minutes=30),
        summary_json={},
    )
    outbox = ResearchNotificationOutbox(
        notification_key_sha256="3" * 64,
        alert_policy_code="production_research_alert_policy_v1",
        alert_policy_checksum_sha256=ACCEPTED_CHECKSUMS["research_alert_v1"],
        payload_schema_version="research_notification_payload_v1",
        source_change_run_id=uuid4(),
        source_change_item_id=uuid4(),
        matched_trigger_codes_json=["became_rankable"],
        payload_json={},
        delivery_status="pending",
    )
    session.add_all((model, latest_run, outbox))
    session.flush()
    deliveries = (
        _delivery(outbox.id, "4", "pending"),
        _delivery(
            outbox.id,
            "5",
            "retry_wait",
            next_attempt_at=OBSERVED_AT - timedelta(seconds=1),
        ),
        _delivery(
            outbox.id,
            "6",
            "claimed",
            claimed_at=OBSERVED_AT - timedelta(minutes=3),
            claim_expires_at=OBSERVED_AT - timedelta(seconds=1),
        ),
        _delivery(outbox.id, "7", "delivered"),
        _delivery(outbox.id, "8", "dead_letter"),
    )
    session.add_all(deliveries)
    session.commit()
    tables = (OperationalRun, ResearchNotificationOutbox, ResearchNotificationDelivery)
    before = tuple(session.scalar(select(func.count()).select_from(table)) for table in tables)

    first = production_health(
        repository_root=ROOT,
        observed_at=OBSERVED_AT,
        session=session,
        raw_root=tmp_path,
        gdelt_raw_root=tmp_path,
        model_family="accepted_model",
        telegram_bot_token_present=True,
        telegram_chat_id_present=True,
    )
    second = production_health(
        repository_root=ROOT,
        observed_at=OBSERVED_AT,
        session=session,
        raw_root=tmp_path,
        gdelt_raw_root=tmp_path,
        model_family="accepted_model",
        telegram_bot_token_present=True,
        telegram_chat_id_present=True,
    )
    after = tuple(session.scalar(select(func.count()).select_from(table)) for table in tables)

    assert first == second
    assert first["status"] == "ready"
    assert first["read_only"] is True
    database = cast(dict[str, object], first["database"])
    migration = cast(dict[str, object], database["migration"])
    runs = cast(dict[str, object], database["operational_runs"])
    latest = cast(dict[str, object], runs["latest"])
    notifications = cast(dict[str, object], database["notifications"])
    delivery = cast(dict[str, object], database["deliveries"])
    assert migration["current"] == "20261004_0023"
    assert latest["id"] == latest_run.id
    assert notifications["pending_outbox"] == 1
    assert delivery["counts_by_status"] == {
        "claimed": 1,
        "dead_letter": 1,
        "delivered": 1,
        "pending": 1,
        "retry_wait": 1,
    }
    assert delivery["due_now"] == 2
    assert delivery["due_retry_wait"] == 1
    assert delivery["expired_claims"] == 1
    assert before == after == (1, 1, 5)
    assert not session.new
    assert not session.dirty
    assert not session.deleted


def test_health_cli_missing_runtime_is_deterministic_and_read_only(
    monkeypatch, capsys
) -> None:
    for name in (
        "INFLECTOR_PRODUCTION_DATABASE_URL",
        "INFLECTOR_PRODUCTION_RAW_ROOT",
        "INFLECTOR_GDELT_RAW_ROOT",
        "INFLECTOR_MODEL_FAMILY",
        "INFLECTOR_TELEGRAM_BOT_TOKEN",
        "INFLECTOR_TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    code = operations_main(
        [
            "health",
            "--repository-root",
            str(ROOT),
            "--observed-at",
            OBSERVED_AT.isoformat(),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 2
    assert report["status"] == "configuration_unavailable"
    assert report["read_only"] is True
    assert report["credentials"] == {
        "telegram_bot_token": "unavailable",
        "telegram_chat_id": "unavailable",
    }


def test_health_module_has_no_mutating_workflow_or_transport_dependency() -> None:
    source = (ROOT / "packages/data/inflector_data/production_health.py").read_text(
        encoding="utf-8"
    )

    for prohibited in (
        "from inflector_data.production_research import",
        "from inflector_data.score_orchestration import",
        "ProductionCycleService",
        "OperationsRepository",
        "ResearchNotificationDeliveryRepository",
        "prepare_notification_deliveries",
        "claim_next",
        "send_message",
        "from inflector_data.telegram_notification_transport import",
    ):
        assert prohibited not in source


def _migration(session: Session) -> None:
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
        {"version": "20261004_0023"},
    )


def _delivery(
    outbox_id,
    key_prefix: str,
    status: str,
    *,
    next_attempt_at: datetime | None = None,
    claimed_at: datetime | None = None,
    claim_expires_at: datetime | None = None,
) -> ResearchNotificationDelivery:
    return ResearchNotificationDelivery(
        delivery_key_sha256=key_prefix * 64,
        outbox_id=outbox_id,
        notification_key_sha256="3" * 64,
        delivery_policy_code="production_notification_delivery_v1",
        delivery_policy_checksum_sha256=ACCEPTED_CHECKSUMS["notification_delivery_v1"],
        transport_code="telegram_bot_api_v1",
        target_code="primary_telegram",
        rendering_version="research_notification_telegram_v1",
        status=status,
        attempt_count=0,
        next_attempt_at=next_attempt_at,
        claim_owner_token="worker" if claimed_at else None,
        claimed_at=claimed_at,
        claim_expires_at=claim_expires_at,
        last_error_detail_json={},
    )
