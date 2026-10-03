"""Production M scheduled K/L monitoring orchestration contracts."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from inflector_data import opportunity_monitoring, production_operations
from inflector_data.operations_profile import load_operations_profile
from inflector_data.opportunity_monitoring import (
    OpportunityMonitoringOrchestrator,
    load_monitoring_policies,
)
from inflector_data.production_operations import (
    CycleInputs,
    CyclePlan,
    ProductionCycleService,
    doctor,
    operational_stage_definitions,
    plan_cycle,
)
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    ModelVersion,
    OperationalRun,
    OpportunityChangeRun,
    OpportunityDiscoveryRun,
    ScoringConfiguration,
)
from inflector_database.operations_repository import OperationsRepository

ROOT = Path(__file__).parents[1]
OPERATIONS = tuple(
    ROOT / f"config/operations/production_operations_v{version}.json"
    for version in range(1, 5)
)
RESEARCH_V4 = ROOT / "config/research/production_research_v4.json"
CYCLE_AT = datetime(2026, 10, 3, 14, 30, tzinfo=UTC)
EXPECTED_PROFILE_CHECKSUMS = (
    "ffefef5ba249ef93eb8ac7db26297a579ed6d59ad02204ad263a7bc354d39d4c",
    "129392617256d4808b03bacf138ef54d020329fcece9d6738c28d28169716f23",
    "fc2db825f7397f4b6aee76155c45ba01e3db082d15b0418badee9c47b8c50976",
)


def _plan(
    tmp_path: Path,
    *,
    cycle_at: datetime = CYCLE_AT,
    symbols: tuple[str, ...] = ("AAA", "BBB"),
) -> CyclePlan:
    symbol_file = tmp_path / f"symbols-{cycle_at.timestamp()}-{'-'.join(symbols)}.txt"
    symbol_file.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    raw_root = tmp_path / f"raw-{cycle_at.timestamp()}"
    gdelt_root = tmp_path / f"gdelt-{cycle_at.timestamp()}"
    raw_root.mkdir(exist_ok=True)
    gdelt_root.mkdir(exist_ok=True)
    return plan_cycle(
        CycleInputs(
            operations_profile_path=OPERATIONS[3],
            research_profile_path=RESEARCH_V4,
            model_family="fictional_monitoring_v1",
            symbols_file=symbol_file,
            symbols=symbols,
            fiscal_year=None,
            fiscal_quarter=None,
            cycle_at=cycle_at,
            raw_root=raw_root,
            nse_license_class="fictional-reviewed-terms",
            gdelt_raw_root=gdelt_root,
            gdelt_license_class="fictional-gdelt-terms",
            model_semantic_version="1",
            git_sha="a" * 40,
            model_effective_from=CYCLE_AT - timedelta(days=1),
        ),
        load_operations_profile(OPERATIONS[3]),
        load_research_profile(RESEARCH_V4),
    )


def _configuration(session: Session) -> ScoringConfiguration:
    model = ModelVersion(
        model_family="fictional_monitoring_v1",
        semantic_version="5",
        git_sha="b" * 40,
        status="active",
    )
    session.add(model)
    session.flush()
    configuration = ScoringConfiguration(
        model_version_id=model.id,
        configuration_name="nse_current_research_v4",
        configuration_version="4",
        status="active",
        configuration_json={},
        checksum_sha256="c" * 64,
    )
    session.add(configuration)
    session.flush()
    return configuration


def _discovery_run(
    session: Session,
    configuration: ScoringConfiguration,
    *,
    token: str,
    cutoff: datetime,
    compatible: bool = True,
) -> OpportunityDiscoveryRun:
    run = OpportunityDiscoveryRun(
        run_key_sha256=(token.encode().hex() + "0" * 64)[:64],
        discovery_policy_code="production_opportunity_discovery_v1",
        discovery_policy_checksum_sha256=(
            "f90e463d3c8940cf9f93210e4d0806bdf4d535340a7d4140ba7d3e517d2e99d2"
        ),
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=(
            configuration.checksum_sha256 if compatible else "9" * 64
        ),
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256=(
            "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
        ),
        financial_primitive_policy_checksum_sha256=(
            "d41513bf624f24f11c5a54a3979b4865a0f524514a77cf5849693f57ace95d92"
        ),
        financial_endpoint_policy_checksum_sha256=(
            "8d101bbec5c58c6939cb0650aa7ed428d9aa2b34ea01913aa48a2b5ce84661d5"
        ),
        model_family="fictional_monitoring_v1",
        discovery_cutoff=cutoff,
        universe_mode="explicit_symbols_file",
        ordered_symbols_json=["AAA"],
        symbol_set_checksum_sha256=(token + "1" * 64)[:64],
        selected_snapshot_set_checksum_sha256=(token + "2" * 64)[:64],
        snapshot_selection_version="latest_visible_bound_score_snapshot_v1",
        ranking_version="v5_final_score_dense_rank_v1",
        inputs_json={"token": token},
        status="completed",
        summary_json={},
        completed_at=cutoff,
    )
    session.add(run)
    session.flush()
    return run


def _operational_run(
    session: Session,
    plan: CyclePlan,
    *,
    discovery_run_id: UUID | None,
    status: str,
) -> OperationalRun:
    repository = OperationsRepository(session)
    run = repository.create_run(
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
        planned_at=plan.inputs.cycle_at,
        stages=operational_stage_definitions(plan.operations_profile),
    )
    run.status = status
    if status in {"completed", "completed_with_symbol_failures"}:
        run.completed_at = plan.inputs.cycle_at
    discovery = next(stage for stage in run.stages if stage.stage_name == "opportunity_discovery")
    if discovery_run_id is not None:
        discovery.status = "completed"
        discovery.result_summary_json = {
            "status": "completed",
            "opportunity_discovery_run_id": str(discovery_run_id),
        }
    change = next(stage for stage in run.stages if stage.stage_name == "opportunity_change")
    if status == "running":
        change.status = "running"
        change.attempt_count = 1
    session.flush()
    return run


def _clock(start: datetime = CYCLE_AT):
    current = start

    def tick() -> datetime:
        nonlocal current
        current += timedelta(seconds=1)
        return current

    return tick


def test_operations_v4_extends_unchanged_profiles_and_binds_exact_policies(
    tmp_path: Path,
) -> None:
    profiles = tuple(load_operations_profile(path) for path in OPERATIONS)
    assert tuple(profile.checksum_sha256 for profile in profiles[:3]) == (
        EXPECTED_PROFILE_CHECKSUMS
    )
    assert all(not profile.opportunity_monitoring_enabled for profile in profiles[:3])
    v4 = profiles[3]
    assert v4.operations_profile_code == "nse_daily_operations_v4"
    assert v4.opportunity_monitoring_enabled
    assert v4.opportunity_discovery_policy_checksum_sha256 == (
        "f90e463d3c8940cf9f93210e4d0806bdf4d535340a7d4140ba7d3e517d2e99d2"
    )
    assert v4.opportunity_change_policy_checksum_sha256 == (
        "2189bdcf6243545524fa8cca8721cd4c5301b775a8c9f89f83c481c55bd646a9"
    )
    reordered = tmp_path / "operations-v4.json"
    value = json.loads(OPERATIONS[3].read_text(encoding="utf-8"))
    reordered.write_text(json.dumps(dict(reversed(tuple(value.items())))), encoding="utf-8")
    assert load_operations_profile(reordered).checksum_sha256 == v4.checksum_sha256


def test_monitoring_policy_tamper_fails_closed(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    target = tmp_path / "config/opportunity"
    discovery = json.loads(
        (ROOT / "config/opportunity/production_opportunity_discovery_v1.json").read_text()
    )
    discovery["maximum_symbols"] = 4999
    (target / "production_opportunity_discovery_v1.json").write_text(
        json.dumps(discovery), encoding="utf-8"
    )
    (target / "production_opportunity_change_v1.json").write_text(
        (ROOT / "config/opportunity/production_opportunity_change_v1.json").read_text(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="checksum"):
        load_monitoring_policies(
            load_operations_profile(OPERATIONS[3]), repository_root=tmp_path
        )


def test_v4_stage_order_cutoff_and_exact_universe_handoff(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path, symbols=("BBB", "AAA"))
    stages = tuple(name for name, _ in operational_stage_definitions(plan.operations_profile))
    assert stages.index("current_research") < stages.index("opportunity_discovery")
    assert stages.index("opportunity_discovery") < stages.index("opportunity_change")
    observed: dict[str, object] = {}

    def fake_build(session: Session, **kwargs):
        del session
        observed.update(kwargs)
        return SimpleNamespace(
            run_id=UUID(int=1),
            run_key_sha256="a" * 64,
            selected_snapshot_set_checksum_sha256="b" * 64,
            requested_securities=2,
            rankable_securities=1,
            unranked_securities=1,
            created_run=True,
            already_completed=False,
        )

    monkeypatch.setattr(opportunity_monitoring, "build_opportunity_discovery", fake_build)
    result, succeeded = OpportunityMonitoringOrchestrator(ROOT).execute_discovery(session, plan)
    assert succeeded
    assert observed["symbols"] == ("BBB", "AAA")
    assert observed["discovery_cutoff"] == plan.inputs.knowledge_cutoff
    assert result["requested_symbol_checksum_sha256"] == plan.symbol_set_checksum_sha256
    assert result["opportunity_discovery_run_id"] == str(UUID(int=1))


def test_first_monitoring_run_completes_change_stage_without_fake_l_run(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    configuration = _configuration(session)
    current = _discovery_run(
        session, configuration, token="current", cutoff=plan.inputs.knowledge_cutoff
    )
    operational = _operational_run(
        session, plan, discovery_run_id=current.id, status="running"
    )
    result, succeeded = OpportunityMonitoringOrchestrator(ROOT).execute_change(session, plan)
    assert succeeded
    assert result["status"] == "no_compatible_baseline"
    assert result["baseline_discovery_run_id"] is None
    assert result["opportunity_change_run_id"] is None
    assert session.scalar(select(func.count()).select_from(OpportunityChangeRun)) == 0
    stage = next(item for item in operational.stages if item.stage_name == "opportunity_change")
    assert stage.result_summary_json["baseline_selection_frozen"] is True


def test_latest_compatible_monitored_baseline_ignores_manual_and_incompatible_runs(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(session)
    day1_plan = _plan(tmp_path, cycle_at=CYCLE_AT - timedelta(days=2), symbols=("AAA",))
    day2_plan = _plan(tmp_path, cycle_at=CYCLE_AT - timedelta(days=1), symbols=("AAA", "CCC"))
    current_plan = _plan(tmp_path, symbols=("AAA", "BBB"))
    day1 = _discovery_run(
        session, configuration, token="day1", cutoff=day1_plan.inputs.knowledge_cutoff
    )
    incompatible = _discovery_run(
        session,
        configuration,
        token="incompatible",
        cutoff=day2_plan.inputs.knowledge_cutoff,
        compatible=False,
    )
    manual = _discovery_run(
        session,
        configuration,
        token="manual",
        cutoff=CYCLE_AT - timedelta(hours=6),
    )
    current = _discovery_run(
        session, configuration, token="current", cutoff=current_plan.inputs.knowledge_cutoff
    )
    _operational_run(session, day1_plan, discovery_run_id=day1.id, status="completed")
    _operational_run(
        session, day2_plan, discovery_run_id=incompatible.id, status="completed"
    )
    _operational_run(session, current_plan, discovery_run_id=current.id, status="running")
    observed: dict[str, object] = {}

    def fake_change(session: Session, **kwargs):
        del session
        observed.update(kwargs)
        return SimpleNamespace(
            run_id=UUID(int=2),
            run_key_sha256="d" * 64,
            compared_identities=3,
            changed_identities=2,
            created_run=True,
            already_completed=False,
        )

    monkeypatch.setattr(opportunity_monitoring, "build_opportunity_change", fake_change)
    result, succeeded = OpportunityMonitoringOrchestrator(ROOT).execute_change(
        session, current_plan
    )
    assert succeeded
    assert observed["baseline_run_id"] == day1.id
    assert observed["baseline_run_id"] != manual.id
    assert observed["current_run_id"] == current.id
    assert result["opportunity_change_run_id"] == str(UUID(int=2))


def test_latest_prior_monitored_baseline_and_frozen_retry_identity(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(session)
    day1_plan = _plan(tmp_path, cycle_at=CYCLE_AT - timedelta(days=3), symbols=("AAA",))
    day2_plan = _plan(tmp_path, cycle_at=CYCLE_AT - timedelta(days=1), symbols=("BBB",))
    current_plan = _plan(tmp_path, symbols=("AAA", "BBB"))
    day1 = _discovery_run(
        session, configuration, token="day1", cutoff=day1_plan.inputs.knowledge_cutoff
    )
    day2 = _discovery_run(
        session, configuration, token="day2", cutoff=day2_plan.inputs.knowledge_cutoff
    )
    current = _discovery_run(
        session, configuration, token="current", cutoff=current_plan.inputs.knowledge_cutoff
    )
    _operational_run(session, day1_plan, discovery_run_id=day1.id, status="completed")
    _operational_run(session, day2_plan, discovery_run_id=day2.id, status="completed")
    operational = _operational_run(
        session, current_plan, discovery_run_id=current.id, status="running"
    )
    change_stage = next(
        item for item in operational.stages if item.stage_name == "opportunity_change"
    )
    change_stage.result_summary_json = {
        "status": "baseline_frozen",
        "baseline_selection_frozen": True,
        "baseline_discovery_run_id": str(day1.id),
        "current_discovery_run_id": str(current.id),
    }
    session.flush()
    observed: dict[str, object] = {}

    def fake_change(session: Session, **kwargs):
        del session
        observed.update(kwargs)
        return SimpleNamespace(
            run_id=UUID(int=3),
            run_key_sha256="e" * 64,
            compared_identities=2,
            changed_identities=1,
            created_run=False,
            already_completed=True,
        )

    monkeypatch.setattr(opportunity_monitoring, "build_opportunity_change", fake_change)
    OpportunityMonitoringOrchestrator(ROOT).execute_change(session, current_plan)
    assert observed["baseline_run_id"] == day1.id
    assert observed["baseline_run_id"] != day2.id


def test_change_failure_persists_baseline_before_retry(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _configuration(session)
    day1_plan = _plan(tmp_path, cycle_at=CYCLE_AT - timedelta(days=3), symbols=("AAA",))
    current_plan = _plan(tmp_path, symbols=("AAA",))
    day1 = _discovery_run(
        session, configuration, token="day1", cutoff=day1_plan.inputs.knowledge_cutoff
    )
    current = _discovery_run(
        session, configuration, token="current", cutoff=current_plan.inputs.knowledge_cutoff
    )
    _operational_run(session, day1_plan, discovery_run_id=day1.id, status="completed")
    operational = _operational_run(
        session, current_plan, discovery_run_id=current.id, status="running"
    )

    def fail_change(session: Session, **kwargs):
        del session, kwargs
        raise RuntimeError("fictional change failure")

    monkeypatch.setattr(opportunity_monitoring, "build_opportunity_change", fail_change)
    with pytest.raises(RuntimeError, match="fictional change failure"):
        OpportunityMonitoringOrchestrator(ROOT).execute_change(session, current_plan)
    stage = next(
        item for item in operational.stages if item.stage_name == "opportunity_change"
    )
    assert stage.result_summary_json["baseline_discovery_run_id"] == str(day1.id)
    assert stage.result_summary_json["baseline_selection_frozen"] is True

    middle_plan = _plan(
        tmp_path, cycle_at=CYCLE_AT - timedelta(days=1), symbols=("AAA",)
    )
    middle = _discovery_run(
        session, configuration, token="middle", cutoff=middle_plan.inputs.knowledge_cutoff
    )
    _operational_run(session, middle_plan, discovery_run_id=middle.id, status="completed")
    observed: dict[str, object] = {}

    def succeed_change(session: Session, **kwargs):
        del session
        observed.update(kwargs)
        return SimpleNamespace(
            run_id=UUID(int=4),
            run_key_sha256="f" * 64,
            compared_identities=1,
            changed_identities=1,
            created_run=True,
            already_completed=False,
        )

    monkeypatch.setattr(opportunity_monitoring, "build_opportunity_change", succeed_change)
    OpportunityMonitoringOrchestrator(ROOT).execute_change(session, current_plan)
    assert observed["baseline_run_id"] == day1.id
    assert observed["baseline_run_id"] != middle.id


class _CycleExecutor:
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
                    {
                        "status": "completed",
                        "symbol": symbol,
                        "snapshot_id": None,
                        "snapshot_status": "partial_component_set",
                        "final_score": None,
                    }
                    for symbol in plan.inputs.symbols
                ],
            }, True
        return {"status": "completed", "stage": stage}, True


def test_k_failure_blocks_l_and_l_resume_skips_completed_k(
    session: Session, tmp_path: Path
) -> None:
    k_plan = _plan(tmp_path, symbols=("AAA",))
    k_failure = _CycleExecutor(failing="opportunity_discovery")
    summary, code = ProductionCycleService(session, k_failure, clock=_clock()).run(
        k_plan, owner_token="k-owner"
    )
    assert code == 1 and summary["status"] == "failed"
    assert "opportunity_change" not in k_failure.calls

    l_plan = _plan(tmp_path, cycle_at=CYCLE_AT + timedelta(days=1), symbols=("AAA",))
    l_failure = _CycleExecutor(failing="opportunity_change")
    failed, code = ProductionCycleService(session, l_failure, clock=_clock()).run(
        l_plan, owner_token="l-owner"
    )
    assert code == 1
    run_id = UUID(str(failed["operational_run_id"]))
    resumed_executor = _CycleExecutor()
    resumed, resumed_code = ProductionCycleService(
        session, resumed_executor, clock=_clock(CYCLE_AT + timedelta(days=2))
    ).resume(run_id, l_plan, owner_token="resume-owner")
    assert resumed_code == 0 and resumed["status"] == "completed"
    assert resumed_executor.calls == ["opportunity_change"]
    assert l_failure.calls.count("opportunity_discovery") == 1


def test_doctor_v4_reports_policy_readiness_and_informational_no_baseline(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES ('20261003_0020')")
    )
    monkeypatch.setattr(production_operations, "production_preflight", lambda *args: None)
    result = doctor(session, plan=plan)
    monitoring = result["opportunity_monitoring"]
    assert isinstance(monitoring, dict)
    assert monitoring["opportunity_discovery"]["status"] == "ready"  # type: ignore[index]
    assert monitoring["opportunity_change"]["status"] == "ready"  # type: ignore[index]
    assert monitoring["prior_baseline"] == {  # type: ignore[index]
        "status": "no_compatible_baseline",
        "informational": True,
    }
    assert result["research"]["status"] == "ready"  # type: ignore[index]


def test_monitoring_module_only_orchestrates_accepted_k_l_apis() -> None:
    path = ROOT / "packages/data/inflector_data/opportunity_monitoring.py"
    source = path.read_text(encoding="utf-8")
    assert "build_opportunity_discovery" in source
    assert "build_opportunity_change" in source
    for forbidden in (
        "final_score",
        "dense_rank",
        "score_delta",
        "rank_delta",
        "component_contribution_delta",
        "backtest_outcomes",
        "backtest_analysis",
        "BacktestOutcome",
    ):
        assert forbidden not in source


def test_production_m_adds_no_migration() -> None:
    versions = tuple((ROOT / "migrations/versions").glob("*.py"))
    assert len(versions) == 20
    assert any("20261003_0020_opportunity_change_detection" in item.name for item in versions)
    assert not any("0021" in item.name for item in versions)
