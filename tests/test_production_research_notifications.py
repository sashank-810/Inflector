"""Production N deterministic notification projection and outbox contracts."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from inflector_data import production_operations
from inflector_data.notification_operations import NotificationProjectionOrchestrator
from inflector_data.operations_profile import load_operations_profile
from inflector_data.production_operations import (
    CycleInputs,
    CyclePlan,
    ProductionCycleService,
    doctor,
    operational_stage_definitions,
    plan_cycle,
)
from inflector_data.research_alert_policy import load_research_alert_policy
from inflector_data.research_notifications import project_research_notifications
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    OperationalRun,
    OpportunityChangeItem,
    OpportunityChangeRun,
    ResearchNotificationOutbox,
)
from inflector_database.operations_repository import OperationsRepository
from inflector_database.research_notification_repository import (
    ResearchNotificationIntegrityError,
    ResearchNotificationRepository,
    ResearchNotificationWrite,
)

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config/notifications/production_research_alert_policy_v1.json"
OPERATIONS = tuple(
    ROOT / f"config/operations/production_operations_v{version}.json"
    for version in range(1, 6)
)
RESEARCH_V4 = ROOT / "config/research/production_research_v4.json"
CUTOFF = datetime(2026, 10, 4, 14, 30, tzinfo=UTC)
L_CHECKSUM = "2189bdcf6243545524fa8cca8721cd4c5301b775a8c9f89f83c481c55bd646a9"
N_CHECKSUM = "e3d1a65eaa80017760d646ed8b03cb7fcf1d0107903c935c70be627a82e33619"
V1_V4_CHECKSUMS = (
    "ffefef5ba249ef93eb8ac7db26297a579ed6d59ad02204ad263a7bc354d39d4c",
    "129392617256d4808b03bacf138ef54d020329fcece9d6738c28d28169716f23",
    "fc2db825f7397f4b6aee76155c45ba01e3db082d15b0418badee9c47b8c50976",
    "5043c8054d0f292c76e0d0e4fb0491fc527a49580f521eb0dec79595939992d0",
)


def _policy():
    return load_research_alert_policy(POLICY_PATH, repository_root=ROOT)


def _change_run(
    session: Session,
    *,
    token: str = "source",
    status: str = "completed",
    policy_checksum: str = L_CHECKSUM,
) -> OpportunityChangeRun:
    baseline_id = uuid4()
    current_id = uuid4()
    baseline_key = f"{token}-baseline".ljust(64, "b")[:64]
    current_key = f"{token}-current".ljust(64, "c")[:64]
    baseline_snapshots = f"{token}-baseline-snapshots".ljust(64, "d")[:64]
    current_snapshots = f"{token}-current-snapshots".ljust(64, "e")[:64]
    run = OpportunityChangeRun(
        run_key_sha256=f"{token}-change".ljust(64, "a")[:64],
        change_policy_code="production_opportunity_change_v1",
        change_policy_checksum_sha256=policy_checksum,
        baseline_discovery_run_id=baseline_id,
        current_discovery_run_id=current_id,
        baseline_run_key_sha256=baseline_key,
        current_run_key_sha256=current_key,
        baseline_snapshot_set_checksum_sha256=baseline_snapshots,
        current_snapshot_set_checksum_sha256=current_snapshots,
        comparison_version="opportunity_discovery_run_comparison_v1",
        inputs_json={
            "baseline_discovery_run_id": str(baseline_id),
            "current_discovery_run_id": str(current_id),
            "baseline_run_key": baseline_key,
            "current_run_key": current_key,
            "baseline_snapshot_set_checksum": baseline_snapshots,
            "current_snapshot_set_checksum": current_snapshots,
        },
        status=status,
        summary_json={},
        completed_at=CUTOFF if status == "completed" else None,
    )
    session.add(run)
    session.flush()
    return run


def _item(
    session: Session,
    run: OpportunityChangeRun,
    *,
    token: str,
    codes: list[str],
    score_delta: Decimal | None = None,
    rank_delta: int | None = None,
    components_gained: list[str] | None = None,
    components_lost: list[str] | None = None,
) -> OpportunityChangeItem:
    item = OpportunityChangeItem(
        opportunity_change_run_id=run.id,
        identity_key=f"symbol:{token}",
        identity_basis="requested_symbol",
        company_id=None,
        security_id=None,
        baseline_symbol=token,
        current_symbol=f"{token}NEW" if "symbol_changed" in codes else token,
        baseline_discovery_item_id=None,
        current_discovery_item_id=None,
        baseline_score_snapshot_id=None,
        current_score_snapshot_id=None,
        change_codes_json=codes,
        changed=True,
        baseline_rankable=False if "became_rankable" in codes else True,
        current_rankable=False if "lost_rankability" in codes else True,
        baseline_unranked_reason=(
            "stale_snapshot" if "became_rankable" in codes else None
        ),
        current_unranked_reason=(
            "partial_score" if "lost_rankability" in codes else None
        ),
        baseline_final_score=None if "became_rankable" in codes else Decimal("60"),
        current_final_score=None if "lost_rankability" in codes else Decimal("60.0001"),
        score_delta=score_delta,
        baseline_score_rank=4 if rank_delta is not None else None,
        current_score_rank=2 if rank_delta is not None else None,
        rank_delta=rank_delta,
        baseline_confidence=None,
        current_confidence=None,
        confidence_delta=None,
        components_gained_json=list(components_gained or []),
        components_lost_json=list(components_lost or []),
        component_change_detail_json={},
        detail_json={"fixture": token},
    )
    session.add(item)
    session.flush()
    return item


def _plan(tmp_path: Path, *, cutoff: datetime = CUTOFF) -> CyclePlan:
    tmp_path.mkdir(parents=True, exist_ok=True)
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("AAA\nBBB\n", encoding="utf-8")
    raw_root = tmp_path / "raw"
    gdelt_root = tmp_path / "gdelt"
    raw_root.mkdir()
    gdelt_root.mkdir()
    return plan_cycle(
        CycleInputs(
            operations_profile_path=OPERATIONS[4],
            research_profile_path=RESEARCH_V4,
            model_family="notification-fixture-v1",
            symbols_file=symbols_file,
            symbols=("AAA", "BBB"),
            fiscal_year=None,
            fiscal_quarter=None,
            cycle_at=cutoff,
            raw_root=raw_root,
            nse_license_class="fixture-reviewed",
            gdelt_raw_root=gdelt_root,
            gdelt_license_class="fixture-gdelt",
            model_semantic_version="1",
            git_sha="a" * 40,
            model_effective_from=cutoff - timedelta(days=1),
        ),
        load_operations_profile(OPERATIONS[4]),
        load_research_profile(RESEARCH_V4),
    )


def _operational_run(session: Session, plan: CyclePlan) -> OperationalRun:
    run = OperationsRepository(session).create_run(
        run_key_sha256=plan.run_key_sha256,
        operations_profile_code=plan.operations_profile.operations_profile_code,
        operations_profile_checksum_sha256=plan.operations_profile.checksum_sha256,
        research_profile_code=plan.research_profile.research_profile_code,
        research_profile_checksum_sha256=plan.research_profile.checksum_sha256,
        model_family=plan.inputs.model_family,
        fiscal_year=None,
        fiscal_quarter=None,
        cycle_at=plan.inputs.cycle_at,
        knowledge_cutoff=plan.inputs.knowledge_cutoff,
        symbol_set_checksum_sha256=plan.symbol_set_checksum_sha256,
        ordered_symbols=plan.inputs.symbols,
        inputs=plan.persisted_inputs,
        planned_at=CUTOFF,
        stages=operational_stage_definitions(plan.operations_profile),
    )
    run.status = "running"
    session.flush()
    return run


def test_alert_policy_is_versioned_bound_and_rejects_unknown_codes(tmp_path: Path) -> None:
    policy = _policy()
    assert policy.code == "production_research_alert_policy_v1"
    assert policy.checksum_sha256 == N_CHECKSUM
    assert policy.opportunity_change_policy_checksum_sha256 == L_CHECKSUM
    copy = tmp_path / "policy.json"
    copy.write_bytes(POLICY_PATH.read_bytes())
    assert load_research_alert_policy(copy, repository_root=ROOT).checksum_sha256 == N_CHECKSUM

    root = tmp_path / "root"
    shutil.copytree(ROOT / "config", root / "config")
    target = root / "config/notifications/production_research_alert_policy_v1.json"
    value = json.loads(target.read_text(encoding="utf-8"))
    value["included_trigger_codes"].append("unknown_change")
    target.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown L change codes"):
        load_research_alert_policy(target, repository_root=root)
    value["included_trigger_codes"].pop()
    value["opportunity_change_policy_checksum_sha256"] = "0" * 64
    target.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="Production L change policy checksum"):
        load_research_alert_policy(target, repository_root=root)


def test_zero_eligible_change_run_succeeds_without_outbox_row(session: Session) -> None:
    run = _change_run(session)
    _item(
        session,
        run,
        token="QUIET",
        codes=["snapshot_changed", "score_unchanged", "rank_unchanged"],
    )
    result = project_research_notifications(
        session, policy=_policy(), source_change_run_id=run.id
    )
    assert result.eligible_items == 0
    assert result.outbox_rows_created == result.outbox_rows_reused == 0
    assert session.scalar(select(func.count()).select_from(ResearchNotificationOutbox)) == 0


def test_projection_filters_noise_preserves_nulls_and_is_idempotent(
    session: Session,
) -> None:
    run = _change_run(session)
    multi = _item(
        session,
        run,
        token="AAA",
        codes=["recovered_from_stale", "became_rankable", "coverage_completed"],
    )
    for token, codes in (
        (
            "LOW",
            [
                "snapshot_changed",
                "remained_rankable",
                "score_unchanged",
                "rank_unchanged",
                "eligibility_unchanged",
            ],
        ),
        ("CONF", ["snapshot_changed", "confidence_changed"]),
        ("INTERNAL", ["component_scores_changed", "component_contributions_changed"]),
    ):
        _item(session, run, token=token, codes=codes)
    tiny = _item(
        session,
        run,
        token="TINY",
        codes=["score_increased", "rank_unchanged"],
        score_delta=Decimal("0.0001"),
    )
    rank_only = _item(
        session,
        run,
        token="RANK",
        codes=["score_unchanged", "rank_moved_up"],
        rank_delta=2,
    )
    score_only = _item(
        session,
        run,
        token="SCORE",
        codes=["score_increased", "rank_unchanged"],
        score_delta=Decimal("1"),
    )
    gained = _item(
        session,
        run,
        token="COMP",
        codes=["components_gained"],
        components_gained=["valuation"],
    )
    entered = _item(
        session, run, token="ENTER", codes=["entered_compared_universe"]
    )
    renamed = _item(session, run, token="OLD", codes=["symbol_changed"])

    result = project_research_notifications(
        session, policy=_policy(), source_change_run_id=run.id
    )
    assert result.source_total_items == 10
    assert result.eligible_items == result.outbox_rows_created == 7
    rows = tuple(session.scalars(select(ResearchNotificationOutbox)))
    assert len(rows) == 7
    multi_row = next(row for row in rows if row.source_change_item_id == multi.id)
    assert multi_row.matched_trigger_codes_json == [
        "became_rankable",
        "coverage_completed",
        "recovered_from_stale",
    ]
    assert multi_row.payload_json["score_delta"] is None
    assert all(row.delivery_status == "pending" for row in rows)
    tiny_row = next(row for row in rows if row.source_change_item_id == tiny.id)
    assert tiny_row.payload_json["score_delta"] == "0.0001"
    rank_row = next(row for row in rows if row.source_change_item_id == rank_only.id)
    assert rank_row.matched_trigger_codes_json == ["rank_moved_up"]
    score_row = next(row for row in rows if row.source_change_item_id == score_only.id)
    assert score_row.matched_trigger_codes_json == ["score_increased"]
    gained_row = next(row for row in rows if row.source_change_item_id == gained.id)
    assert gained_row.payload_json["components_gained"] == ["valuation"]
    assert any(row.source_change_item_id == entered.id for row in rows)
    assert sum(row.source_change_item_id == renamed.id for row in rows) == 1
    rendered_payloads = json.dumps([row.payload_json for row in rows]).lower()
    for prohibited in (
        "buy",
        "sell",
        "accumulate",
        "avoid",
        "bullish",
        "bearish",
        "target price",
        "strong opportunity",
    ):
        assert prohibited not in rendered_payloads

    repeated = project_research_notifications(
        session, policy=_policy(), source_change_run_id=run.id
    )
    assert repeated.outbox_rows_created == 0
    assert repeated.outbox_rows_reused == 7
    assert session.scalar(select(func.count()).select_from(ResearchNotificationOutbox)) == 7


def test_outbox_conflict_and_policy_change_identity(session: Session) -> None:
    run = _change_run(session)
    _item(session, run, token="AAA", codes=["score_increased"], score_delta=Decimal("1"))
    policy = _policy()
    project_research_notifications(session, policy=policy, source_change_run_id=run.id)
    row = session.scalar(select(ResearchNotificationOutbox))
    assert row is not None
    conflict = ResearchNotificationWrite(
        notification_key_sha256=row.notification_key_sha256,
        alert_policy_code=row.alert_policy_code,
        alert_policy_checksum_sha256=row.alert_policy_checksum_sha256,
        payload_schema_version=row.payload_schema_version,
        source_change_run_id=row.source_change_run_id,
        source_change_item_id=row.source_change_item_id,
        company_id=row.company_id,
        security_id=row.security_id,
        baseline_symbol=row.baseline_symbol,
        current_symbol=row.current_symbol,
        matched_trigger_codes_json=row.matched_trigger_codes_json,
        payload_json={**row.payload_json, "conflict": True},
    )
    with pytest.raises(ResearchNotificationIntegrityError, match="conflicts"):
        ResearchNotificationRepository(session).create_or_reuse(conflict)

    changed_policy = replace(policy, checksum_sha256="f" * 64)
    result = project_research_notifications(
        session, policy=changed_policy, source_change_run_id=run.id
    )
    assert result.outbox_rows_created == 1
    assert session.scalar(select(func.count()).select_from(ResearchNotificationOutbox)) == 2


def test_source_status_policy_and_lineage_fail_closed(session: Session) -> None:
    planned = _change_run(session, token="planned", status="planned")
    with pytest.raises(ValueError, match="must be completed"):
        project_research_notifications(
            session, policy=_policy(), source_change_run_id=planned.id
        )
    wrong = _change_run(session, token="wrong", policy_checksum="0" * 64)
    with pytest.raises(ValueError, match="policy binding"):
        project_research_notifications(session, policy=_policy(), source_change_run_id=wrong.id)
    malformed = _change_run(session, token="malformed")
    malformed.inputs_json = {**malformed.inputs_json, "current_run_key": "wrong"}
    session.flush()
    with pytest.raises(ValueError, match="lineage mismatch"):
        project_research_notifications(
            session, policy=_policy(), source_change_run_id=malformed.id
        )


def test_operations_v5_profile_stage_order_and_policy_tamper(tmp_path: Path) -> None:
    profiles = tuple(load_operations_profile(path) for path in OPERATIONS)
    assert tuple(profile.checksum_sha256 for profile in profiles[:4]) == V1_V4_CHECKSUMS
    assert all(not profile.notification_projection_enabled for profile in profiles[:4])
    v5 = profiles[4]
    assert v5.operations_profile_code == "nse_daily_operations_v5"
    assert v5.checksum_sha256 == (
        "e89a2405d51ea2ece8ad7780911e7159b724e6f399e947e25ebc368fe4d4ae42"
    )
    assert v5.research_alert_policy_checksum_sha256 == N_CHECKSUM
    names = tuple(name for name, _ in operational_stage_definitions(v5))
    assert names.index("opportunity_change") < names.index("notification_projection")

    root = tmp_path / "tampered"
    shutil.copytree(ROOT / "config", root / "config")
    target = root / "config/notifications/production_research_alert_policy_v1.json"
    value = json.loads(target.read_text(encoding="utf-8"))
    value["payload_schema_version"] = "tampered"
    target.write_text(json.dumps(value), encoding="utf-8")
    profile_value = json.loads(OPERATIONS[4].read_text(encoding="utf-8"))
    profile_path = root / "config/operations/production_operations_v5.json"
    profile_path.write_text(json.dumps(profile_value), encoding="utf-8")
    from inflector_data.notification_operations import load_bound_research_alert_policy

    with pytest.raises(ValueError):
        load_bound_research_alert_policy(
            load_operations_profile(profile_path), repository_root=root
        )


def test_operational_projection_uses_exact_stage_l_run_and_handles_no_run(
    session: Session, tmp_path: Path
) -> None:
    no_run_plan = _plan(tmp_path / "no-run")
    no_run = _operational_run(session, no_run_plan)
    no_run_change = next(
        stage for stage in no_run.stages if stage.stage_name == "opportunity_change"
    )
    no_run_change.status = "completed"
    no_run_change.result_summary_json = {
        "status": "no_compatible_baseline",
        "opportunity_change_run_id": None,
    }
    session.flush()
    result, succeeded = NotificationProjectionOrchestrator(ROOT).execute(
        session, no_run_plan
    )
    assert succeeded and result["status"] == "no_change_run"
    assert result["outbox_rows_created"] == 0

    source_plan = _plan(tmp_path / "source", cutoff=CUTOFF + timedelta(days=1))
    operational = _operational_run(session, source_plan)
    source = _change_run(session, token="monitored")
    source_item = _item(session, source, token="SOURCE", codes=["score_increased"])
    manual = _change_run(session, token="manual")
    _item(session, manual, token="MANUAL", codes=["score_increased"])
    stage = next(
        item for item in operational.stages if item.stage_name == "opportunity_change"
    )
    stage.status = "completed"
    stage.result_summary_json = {
        "status": "completed",
        "opportunity_change_run_id": str(source.id),
        "opportunity_change_run_key": source.run_key_sha256,
        "baseline_discovery_run_id": str(source.baseline_discovery_run_id),
        "current_discovery_run_id": str(source.current_discovery_run_id),
    }
    session.flush()
    projected, succeeded = NotificationProjectionOrchestrator(ROOT).execute(
        session, source_plan
    )
    assert succeeded and projected["source_change_run_id"] == str(source.id)
    rows = tuple(session.scalars(select(ResearchNotificationOutbox)))
    assert len(rows) == 1 and rows[0].source_change_item_id == source_item.id


class _Executor:
    def __init__(self, failing: str | None = None) -> None:
        self.failing = failing
        self.calls: list[str] = []

    def execute(
        self, stage: str, *, session: Session, plan: CyclePlan
    ) -> tuple[dict[str, object], bool]:
        del session
        self.calls.append(stage)
        if stage == self.failing:
            return {"status": "failed"}, False
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
    value = CUTOFF

    def tick() -> datetime:
        nonlocal value
        value += timedelta(seconds=1)
        return value

    return tick


def test_notification_failure_resume_skips_completed_k_l(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    failing = _Executor(failing="notification_projection")
    summary, code = ProductionCycleService(session, failing, clock=_clock()).run(
        plan, owner_token="first"
    )
    assert code == 1
    run_id = UUID(str(summary["operational_run_id"]))
    resumed_executor = _Executor()
    resumed, resumed_code = ProductionCycleService(
        session, resumed_executor, clock=_clock()
    ).resume(run_id, plan, owner_token="resume")
    assert resumed_code == 0 and resumed["status"] == "completed"
    assert resumed_executor.calls == ["notification_projection"]
    assert failing.calls.index("opportunity_change") < failing.calls.index(
        "notification_projection"
    )


def test_doctor_v5_validates_notification_without_transport_credentials(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES ('20261003_0021')")
    )
    monkeypatch.setattr(production_operations, "production_preflight", lambda *args: None)
    result = doctor(session, plan=plan)
    notification = result["notification_projection"]
    assert isinstance(notification, dict)
    assert notification["status"] == "ready"
    assert notification["policy_checksum_sha256"] == N_CHECKSUM
    assert notification["transport_credentials_required"] is False


def test_notification_modules_have_no_delivery_research_or_backtest_dependency() -> None:
    files = (
        ROOT / "packages/data/inflector_data/research_notifications.py",
        ROOT / "packages/data/inflector_data/notification_operations.py",
        ROOT / "packages/database/inflector_database/research_notification_repository.py",
    )
    import_text = "\n".join(
        line
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("from ", "import "))
    )
    for forbidden in (
        "requests",
        "httpx",
        "smtplib",
        "telegram",
        "slack",
        "twilio",
        "backtest",
        "BacktestRun",
        "BacktestOutcome",
        "production_research",
        "score_orchestration",
        "inflector_core",
    ):
        assert forbidden not in import_text


def test_migration_0021_adds_and_removes_only_notification_outbox(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    command.upgrade(config, "20261003_0020")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    engine.dispose()
    command.upgrade(config, "20261003_0021")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    after = set(inspect(engine).get_table_names())
    assert after - before == {"research_notification_outbox"}
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            "20261003_0021"
        )
    engine.dispose()
    command.downgrade(config, "20261003_0020")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()
