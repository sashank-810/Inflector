"""Production O reliable Telegram notification delivery contracts."""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from inflector_data import production_operations
from inflector_data.notification_delivery_operations import (
    NotificationDeliveryOrchestrator,
    load_bound_notification_delivery_policy,
)
from inflector_data.notification_delivery_policy import (
    NotificationDeliveryPolicy,
    load_notification_delivery_policy,
)
from inflector_data.operations_profile import load_operations_profile
from inflector_data.production_operations import (
    CycleInputs,
    CyclePlan,
    ProductionCycleService,
    doctor,
    operational_stage_definitions,
    plan_cycle,
)
from inflector_data.research_notification_delivery import (
    delivery_key,
    drain_notification_deliveries,
    prepare_notification_deliveries,
)
from inflector_data.research_profile import load_research_profile
from inflector_data.telegram_notification_renderer import (
    render_telegram_notification,
)
from inflector_data.telegram_notification_transport import (
    TelegramBotApiTransport,
    TelegramTransportResult,
)
from inflector_database.models import (
    ResearchNotificationDelivery,
    ResearchNotificationDeliveryAttempt,
    ResearchNotificationOutbox,
)
from inflector_database.research_notification_delivery_repository import (
    ResearchNotificationDeliveryIntegrityError,
    ResearchNotificationDeliveryRepository,
    ResearchNotificationDeliveryWrite,
)

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config/notifications/production_notification_delivery_v1.json"
OPERATIONS = tuple(
    ROOT / f"config/operations/production_operations_v{version}.json"
    for version in range(1, 7)
)
NOW = datetime(2026, 10, 3, 14, 30, tzinfo=UTC)
N_CHECKSUM = "e3d1a65eaa80017760d646ed8b03cb7fcf1d0107903c935c70be627a82e33619"
O_CHECKSUM = "307756eac5ba4e63d7631e22e59057f9f301dd5ed431180e89dab54ed610360e"
V1_V5_CHECKSUMS = (
    "ffefef5ba249ef93eb8ac7db26297a579ed6d59ad02204ad263a7bc354d39d4c",
    "129392617256d4808b03bacf138ef54d020329fcece9d6738c28d28169716f23",
    "fc2db825f7397f4b6aee76155c45ba01e3db082d15b0418badee9c47b8c50976",
    "5043c8054d0f292c76e0d0e4fb0491fc527a49580f521eb0dec79595939992d0",
    "e89a2405d51ea2ece8ad7780911e7159b724e6f399e947e25ebc368fe4d4ae42",
)


def _policy() -> NotificationDeliveryPolicy:
    return load_notification_delivery_policy(POLICY_PATH, repository_root=ROOT)


def _plan(tmp_path: Path) -> CyclePlan:
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("AAA\n", encoding="utf-8")
    raw_root = tmp_path / "raw"
    gdelt_root = tmp_path / "gdelt"
    raw_root.mkdir()
    gdelt_root.mkdir()
    profile = load_operations_profile(OPERATIONS[5])
    research_path = ROOT / "config/research/production_research_v4.json"
    return plan_cycle(
        CycleInputs(
            operations_profile_path=OPERATIONS[5],
            research_profile_path=research_path,
            model_family="delivery-fixture-v1",
            symbols_file=symbols_file,
            symbols=("AAA",),
            fiscal_year=None,
            fiscal_quarter=None,
            cycle_at=NOW,
            raw_root=raw_root,
            nse_license_class="fixture-reviewed",
            gdelt_raw_root=gdelt_root,
            gdelt_license_class="fixture-reviewed",
            model_semantic_version="fixture-v1",
            git_sha="fixture",
            model_effective_from=NOW,
        ),
        profile,
        load_research_profile(research_path),
    )


def _outbox(
    session: Session,
    *,
    token: str = "AAA",
    triggers: list[str] | None = None,
    score: str | None = "70.25",
    rank: int | None = 8,
) -> ResearchNotificationOutbox:
    codes = triggers or ["score_increased"]
    row = ResearchNotificationOutbox(
        notification_key_sha256=f"{token}-notification".ljust(64, "n")[:64],
        alert_policy_code="production_research_alert_policy_v1",
        alert_policy_checksum_sha256=N_CHECKSUM,
        payload_schema_version="research_notification_payload_v1",
        source_change_run_id=uuid4(),
        source_change_item_id=uuid4(),
        company_id=None,
        security_id=None,
        baseline_symbol=token,
        current_symbol=token,
        matched_trigger_codes_json=list(codes),
        payload_json={
            "current_final_score": score,
            "current_score_rank": rank,
            "matched_trigger_codes": list(codes),
            "score_delta": None,
        },
        delivery_status="pending",
    )
    session.add(row)
    session.flush()
    return row


def test_delivery_policy_operations_v6_and_protected_profile_checksums() -> None:
    policy = _policy()
    assert policy.checksum_sha256 == O_CHECKSUM
    assert policy.research_alert_policy_checksum_sha256 == N_CHECKSUM
    assert policy.retry_schedule_seconds == (60, 300, 1800, 7200)
    assert policy.maximum_attempts == 5 and policy.claim_lease_seconds == 300
    profiles = tuple(load_operations_profile(path) for path in OPERATIONS)
    assert tuple(profile.checksum_sha256 for profile in profiles[:5]) == V1_V5_CHECKSUMS
    assert all(not profile.notification_delivery_enabled for profile in profiles[:5])
    v6 = profiles[5]
    assert v6.checksum_sha256 == "f71631f7f5baddd42796aae67c42c2732b1435a4d0a13c22ebe06031e70ae41f"
    assert v6.notification_delivery_policy_checksum_sha256 == O_CHECKSUM
    names = tuple(name for name, _ in operational_stage_definitions(v6))
    assert names[-2:] == ("notification_projection", "notification_delivery")
    assert load_bound_notification_delivery_policy(v6, repository_root=ROOT) == policy
    serialized = POLICY_PATH.read_text(encoding="utf-8")
    assert "INFLECTOR_TELEGRAM" not in serialized


def test_operations_v6_policy_tamper_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "tampered"
    shutil.copytree(ROOT / "config", root / "config")
    policy_path = root / "config/notifications/production_notification_delivery_v1.json"
    value = json.loads(policy_path.read_text(encoding="utf-8"))
    value["maximum_messages_per_cycle"] = 99
    policy_path.write_text(json.dumps(value), encoding="utf-8")
    profile = load_operations_profile(
        root / "config/operations/production_operations_v6.json"
    )
    with pytest.raises(ValueError, match="binding mismatch"):
        load_bound_notification_delivery_policy(profile, repository_root=root)


def test_delivery_identity_target_policy_and_preparation_idempotency(
    session: Session,
) -> None:
    outbox = _outbox(session)
    policy = _policy()
    first = delivery_key(outbox, policy)
    assert first == delivery_key(outbox, policy)
    assert first != delivery_key(outbox, replace(policy, checksum_sha256="f" * 64))
    assert first != delivery_key(outbox, replace(policy, target_code="secondary"))
    created = prepare_notification_deliveries(session, policy=policy)
    repeated = prepare_notification_deliveries(session, policy=policy)
    assert created.delivery_rows_created == 1
    assert repeated.delivery_rows_created == 0
    assert session.scalar(select(func.count()).select_from(ResearchNotificationDelivery)) == 1


def test_preparation_uses_only_the_bound_n_policy(session: Session) -> None:
    accepted = _outbox(session, token="ACCEPTED")
    rejected = _outbox(session, token="OTHER")
    rejected.alert_policy_checksum_sha256 = "f" * 64
    session.flush()
    result = prepare_notification_deliveries(session, policy=_policy())
    delivery = session.scalar(select(ResearchNotificationDelivery))
    assert result.delivery_rows_created == 1
    assert delivery is not None and delivery.outbox_id == accepted.id
    with pytest.raises(ValueError, match="bound N policy"):
        delivery_key(rejected, _policy())


def test_delivery_identity_conflict_fails_closed(session: Session) -> None:
    outbox = _outbox(session)
    policy = _policy()
    prepare_notification_deliveries(session, policy=policy)
    delivery = session.scalar(select(ResearchNotificationDelivery))
    assert delivery is not None
    conflicting = ResearchNotificationDeliveryWrite(
        delivery_key_sha256=delivery.delivery_key_sha256,
        outbox_id=outbox.id,
        notification_key_sha256=outbox.notification_key_sha256,
        delivery_policy_code=policy.code,
        delivery_policy_checksum_sha256=policy.checksum_sha256,
        transport_code=policy.transport_code,
        target_code="conflicting-target",
        rendering_version=policy.rendering_version,
    )
    with pytest.raises(ResearchNotificationDeliveryIntegrityError, match="conflicts"):
        ResearchNotificationDeliveryRepository(session).create_or_reuse(conflicting)


def test_claim_exclusion_expiry_recovery_and_delivered_terminal(session: Session) -> None:
    _outbox(session)
    policy = _policy()
    prepare_notification_deliveries(session, policy=policy)
    repository = ResearchNotificationDeliveryRepository(session)
    first = repository.claim_next(
        worker_token="worker-a",
        now=NOW,
        lease_seconds=policy.claim_lease_seconds,
        maximum_attempts=policy.maximum_attempts,
    )
    assert first is not None
    assert (
        repository.claim_next(
            worker_token="worker-b",
            now=NOW + timedelta(seconds=299),
            lease_seconds=policy.claim_lease_seconds,
            maximum_attempts=policy.maximum_attempts,
        )
        is None
    )
    recovered = repository.claim_next(
        worker_token="worker-b",
        now=NOW + timedelta(seconds=300),
        lease_seconds=policy.claim_lease_seconds,
        maximum_attempts=policy.maximum_attempts,
    )
    assert recovered is not None and recovered.attempt_number == 2
    repository.record_delivered(
        claim=recovered,
        worker_token="worker-b",
        completed_at=NOW + timedelta(seconds=301),
        provider_message_id="42",
        http_status=200,
    )
    delivery = repository.get(recovered.delivery_id)
    assert delivery is not None and delivery.status == "delivered"
    assert delivery.provider_message_id == "42" and delivery.attempt_count == 2
    assert repository.claim_next(
        worker_token="worker-c",
        now=NOW + timedelta(days=1),
        lease_seconds=300,
        maximum_attempts=5,
    ) is None


def test_renderer_preserves_nulls_and_multi_trigger_without_recommendations(
    session: Session,
) -> None:
    row = _outbox(
        session,
        triggers=["became_rankable", "coverage_completed", "recovered_from_stale"],
        score=None,
        rank=None,
    )
    rendered = render_telegram_notification(
        row, delivery_key_sha256="a" * 64, rendering_version=_policy().rendering_version
    )
    assert rendered.count("Inflector research update") == 1
    assert "Became rankable" in rendered and "Research coverage completed" in rendered
    assert "Score: 0" not in rendered and "Rank: 0" not in rendered
    prohibited = ("buy", "sell", "accumulate", "avoid", "bullish", "bearish", "target price")
    assert not any(word in rendered.lower() for word in prohibited)


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, amount: int = -1) -> bytes:
        return self._payload[:amount]

    def close(self) -> None:
        return None


class _Opener:
    def __init__(self, result: _Response | Exception) -> None:
        self.result = result
        self.request: Request | None = None
        self.timeout: int | None = None

    def open(self, request: Request, timeout: int) -> _Response:
        self.request = request
        self.timeout = timeout
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    ("status", "payload", "expected_ok", "retry_after"),
    (
        (200, {"ok": True, "result": {"message_id": 42}}, True, None),
        (200, {"ok": False, "error_code": 500}, False, None),
        (500, {"ok": False, "error_code": 500}, False, None),
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 900}}, False, 900),
    ),
)
def test_telegram_response_validation(
    status: int,
    payload: object,
    expected_ok: bool,
    retry_after: int | None,
) -> None:
    opener = _Opener(_Response(status, payload))
    result = TelegramBotApiTransport(opener=opener).send_message(
        bot_token="secret-token", chat_id="secret-chat", text="factual", timeout_seconds=10
    )
    assert result.ok is expected_ok and result.retry_after_seconds == retry_after
    assert opener.timeout == 10
    if expected_ok:
        assert result.provider_message_id == "42"


def test_telegram_http_errors_timeout_and_malformed_are_not_success() -> None:
    for status in (400, 401, 403, 500):
        body = io.BytesIO(json.dumps({"ok": False, "error_code": status}).encode())
        error = HTTPError("sanitized", status, "failure", Message(), body)
        result = TelegramBotApiTransport(opener=_Opener(error)).send_message(
            bot_token="secret-token", chat_id="secret-chat", text="factual", timeout_seconds=10
        )
        assert not result.ok and result.http_status == status
    timeout = TelegramBotApiTransport(opener=_Opener(URLError(TimeoutError()))).send_message(
        bot_token="secret-token", chat_id="secret-chat", text="factual", timeout_seconds=10
    )
    assert not timeout.ok and timeout.error_code == "telegram_timeout"
    connection = TelegramBotApiTransport(opener=_Opener(URLError("offline"))).send_message(
        bot_token="secret-token", chat_id="secret-chat", text="factual", timeout_seconds=10
    )
    assert not connection.ok and connection.error_code == "telegram_connection"
    malformed = TelegramBotApiTransport(
        opener=_Opener(_Response(200, ["not", "object"]))
    ).send_message(
        bot_token="secret-token", chat_id="secret-chat", text="factual", timeout_seconds=10
    )
    assert not malformed.ok and malformed.malformed_response


class _Transport(TelegramBotApiTransport):
    def __init__(self, results: list[TelegramTransportResult]) -> None:
        self.results = results
        self.messages: list[str] = []

    def send_message(
        self, *, bot_token: str, chat_id: str, text: str, timeout_seconds: int
    ) -> TelegramTransportResult:
        assert bot_token == "runtime-token" and chat_id == "runtime-chat"
        assert timeout_seconds == 10
        self.messages.append(text)
        return self.results.pop(0)


_MIRROR_HTTP_STATUS = object()


def _failure(
    status: int | None,
    *,
    telegram_error_code: int | None | object = _MIRROR_HTTP_STATUS,
    retry_after: int | None = None,
    malformed: bool = False,
) -> TelegramTransportResult:
    if telegram_error_code is _MIRROR_HTTP_STATUS:
        provider_code = status
    elif telegram_error_code is None or isinstance(telegram_error_code, int):
        provider_code = telegram_error_code
    else:
        raise AssertionError("invalid fixture Telegram error code")
    return TelegramTransportResult(
        http_status=status,
        ok=False,
        provider_message_id=None,
        telegram_error_code=provider_code,
        retry_after_seconds=retry_after,
        error_code="fixture_failure",
        malformed_response=malformed,
    )


def test_delivery_success_and_secret_hygiene(session: Session) -> None:
    _outbox(session)
    transport = _Transport(
        [TelegramTransportResult(200, True, "88", None, None, None, False)]
    )
    result = drain_notification_deliveries(
        session,
        policy=_policy(),
        transport=transport,
        bot_token="runtime-token",
        chat_id="runtime-chat",
        worker_token="worker",
        clock=lambda: NOW,
    )
    assert result.delivered == 1 and result.attempts_started == 1
    delivery = session.scalar(select(ResearchNotificationDelivery))
    attempt = session.scalar(select(ResearchNotificationDeliveryAttempt))
    assert delivery is not None and delivery.status == "delivered"
    assert attempt is not None and attempt.outcome == "delivered"
    serialized = json.dumps(
        {
            "delivery": delivery.last_error_detail_json,
            "attempt": attempt.detail_json,
            "message": transport.messages[0],
        }
    )
    assert "runtime-token" not in serialized and "runtime-chat" not in serialized


@pytest.mark.parametrize(
    ("provider_result", "status", "delay", "attempt_outcome"),
    (
        (_failure(500, telegram_error_code=None), "retry_wait", 60, "retryable_failure"),
        (_failure(None, telegram_error_code=None), "retry_wait", 60, "retryable_failure"),
        (
            _failure(429, telegram_error_code=None, retry_after=900),
            "retry_wait",
            900,
            "retryable_failure",
        ),
        (
            _failure(400, telegram_error_code=None, malformed=True),
            "dead_letter",
            None,
            "permanent_failure",
        ),
        (
            _failure(401, telegram_error_code=500, malformed=True),
            "dead_letter",
            None,
            "permanent_failure",
        ),
        (
            _failure(403, telegram_error_code=None, malformed=True),
            "dead_letter",
            None,
            "permanent_failure",
        ),
        (
            _failure(200, telegram_error_code=500),
            "retry_wait",
            60,
            "retryable_failure",
        ),
        (
            _failure(200, telegram_error_code=429, retry_after=900),
            "retry_wait",
            900,
            "retryable_failure",
        ),
        (
            _failure(200, telegram_error_code=401),
            "dead_letter",
            None,
            "permanent_failure",
        ),
        (
            _failure(200, telegram_error_code=None, malformed=True),
            "retry_wait",
            60,
            "retryable_failure",
        ),
    ),
)
def test_retry_and_permanent_failure_semantics(
    session: Session,
    provider_result: TelegramTransportResult,
    status: str,
    delay: int | None,
    attempt_outcome: str,
) -> None:
    _outbox(session)
    drain_notification_deliveries(
        session,
        policy=_policy(),
        transport=_Transport([provider_result]),
        bot_token="runtime-token",
        chat_id="runtime-chat",
        worker_token="worker",
        clock=lambda: NOW,
    )
    delivery = session.scalar(select(ResearchNotificationDelivery))
    attempt = session.scalar(select(ResearchNotificationDeliveryAttempt))
    assert delivery is not None and delivery.status == status
    assert attempt is not None and attempt.outcome == attempt_outcome
    if delay is None:
        assert delivery.next_attempt_at is None
    else:
        expected = NOW + timedelta(seconds=delay)
        observed = delivery.next_attempt_at
        assert observed is not None and observed.replace(tzinfo=UTC) == expected


def test_max_attempts_dead_letters_and_attempt_count_is_exact(session: Session) -> None:
    _outbox(session)
    policy = _policy()
    prepare_notification_deliveries(session, policy=policy)
    repository = ResearchNotificationDeliveryRepository(session)
    now = NOW
    for attempt_number in range(1, 6):
        claim = repository.claim_next(
            worker_token="worker",
            now=now,
            lease_seconds=policy.claim_lease_seconds,
            maximum_attempts=policy.maximum_attempts,
        )
        assert claim is not None and claim.attempt_number == attempt_number
        delay = policy.retry_delay_seconds(attempt_number) if attempt_number < 5 else None
        repository.record_failure(
            claim=claim,
            worker_token="worker",
            completed_at=now,
            retryable=True,
            retry_delay_seconds=delay,
            maximum_attempts=5,
            error_code="fixture",
            detail={},
            http_status=500,
            telegram_error_code=500,
        )
        now += timedelta(seconds=(delay or 0))
    delivery = session.scalar(select(ResearchNotificationDelivery))
    assert delivery is not None and delivery.status == "dead_letter"
    assert delivery.attempt_count == 5
    assert (
        session.scalar(
            select(func.count()).select_from(ResearchNotificationDeliveryAttempt)
        )
        == 5
    )
    assert repository.claim_next(
        worker_token="other", now=now + timedelta(days=1), lease_seconds=300, maximum_attempts=5
    ) is None


def test_due_backlog_is_processed_by_later_cycle(session: Session) -> None:
    _outbox(session)
    first = drain_notification_deliveries(
        session,
        policy=_policy(),
        transport=_Transport([_failure(500)]),
        bot_token="runtime-token",
        chat_id="runtime-chat",
        worker_token="cycle-one",
        clock=lambda: NOW,
    )
    assert first.retry_wait == 1
    later = drain_notification_deliveries(
        session,
        policy=_policy(),
        transport=_Transport(
            [TelegramTransportResult(200, True, "99", None, None, None, False)]
        ),
        bot_token="runtime-token",
        chat_id="runtime-chat",
        worker_token="cycle-two",
        clock=lambda: NOW + timedelta(seconds=60),
    )
    assert later.delivered == 1
    delivery = session.scalar(select(ResearchNotificationDelivery))
    assert delivery is not None and delivery.attempt_count == 2


def test_operations_missing_credentials_prepares_backlog_without_network(
    session: Session,
) -> None:
    _outbox(session)
    profile = load_operations_profile(OPERATIONS[5])

    class _Plan:
        operations_profile = profile
        run_key_sha256 = "a" * 64

    result, succeeded = NotificationDeliveryOrchestrator(
        ROOT, environment={}
    ).execute(session, _Plan())
    assert succeeded and result["status"] == "transport_unavailable"
    assert result["credentials_available"] is False
    assert result["backlog_count"] == 1 and result["attempts_started"] == 0


def test_doctor_v6_reports_credential_availability_without_values(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    session.execute(
        text(
            "INSERT INTO alembic_version (version_num) VALUES ('20261003_0022')"
        )
    )
    monkeypatch.setattr(production_operations, "production_preflight", lambda *args: None)
    monkeypatch.setenv("INFLECTOR_TELEGRAM_BOT_TOKEN", "doctor-secret-token")
    monkeypatch.delenv("INFLECTOR_TELEGRAM_CHAT_ID", raising=False)
    result = doctor(session, plan=plan)
    delivery = result["notification_delivery"]
    assert isinstance(delivery, dict)
    assert delivery["status"] == "configured"
    assert delivery["telegram_credentials"] == "unavailable"
    assert "doctor-secret-token" not in json.dumps(result)


class _StageExecutor:
    def __init__(self, failing: str | None = None) -> None:
        self.failing = failing
        self.calls: list[str] = []

    def execute(
        self, stage: str, *, session: Session, plan: CyclePlan
    ) -> tuple[dict[str, object], bool]:
        del session
        self.calls.append(stage)
        if stage == self.failing:
            raise RuntimeError("fixture integrity failure")
        if stage == "current_research":
            return {
                "status": "completed",
                "results": [
                    {"symbol": symbol, "status": "completed", "snapshot_id": None}
                    for symbol in plan.inputs.symbols
                ],
            }, True
        return {"status": "completed"}, True


def _clock():
    current = NOW

    def tick() -> datetime:
        nonlocal current
        current += timedelta(seconds=1)
        return current

    return tick


def test_delivery_integrity_failure_resume_skips_research_k_l_n(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    failed_executor = _StageExecutor(failing="notification_delivery")
    summary, code = ProductionCycleService(
        session, failed_executor, clock=_clock()
    ).run(plan, owner_token="first")
    assert code == 1
    resumed_executor = _StageExecutor()
    resumed, resumed_code = ProductionCycleService(
        session, resumed_executor, clock=_clock()
    ).resume(
        UUID(str(summary["operational_run_id"])),
        plan,
        owner_token="resume",
    )
    assert resumed_code == 0 and resumed["status"] == "completed"
    assert resumed_executor.calls == ["notification_delivery"]


def test_delivery_modules_do_not_recompute_n_l_k_or_backtest_semantics() -> None:
    paths = (
        ROOT / "packages/data/inflector_data/research_notification_delivery.py",
        ROOT / "packages/data/inflector_data/telegram_notification_renderer.py",
        ROOT / "packages/data/inflector_data/telegram_notification_transport.py",
        ROOT / "packages/data/inflector_data/notification_delivery_operations.py",
    )
    imports = "\n".join(
        line
        for path in paths
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("from ", "import "))
    )
    for forbidden in (
        "OpportunityChangeItem",
        "OpportunityDiscovery",
        "ScoreSnapshot",
        "Backtest",
        "backtest_",
        "inflector_core",
        "production_research",
    ):
        assert forbidden not in imports


def test_migration_0022_adds_only_delivery_tables_and_preserves_n(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    command.upgrade(config, "20261003_0021")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    engine.dispose()
    command.upgrade(config, "20261003_0022")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    after = set(inspect(engine).get_table_names())
    assert after - before == {
        "research_notification_deliveries",
        "research_notification_delivery_attempts",
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar() == "20261003_0022"
    engine.dispose()
    command.downgrade(config, "20261003_0021")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    assert set(inspect(engine).get_table_names()) == before
    assert "research_notification_outbox" in before
    engine.dispose()
