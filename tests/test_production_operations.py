"""Production E operational profile, ledger, lease, resume, and burn-in tests."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from inflector_data.operations_profile import load_operations_profile
from inflector_data.production_operations import (
    OPERATIONAL_STAGE_DEFINITIONS,
    CycleInputs,
    CyclePlan,
    ProductionCycleService,
    doctor,
    plan_cycle,
    snapshot_change_report,
    symbol_set_checksum,
)
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    Company,
    ModelVersion,
    OperationalRun,
    ScoreSnapshot,
    ScoringConfiguration,
)
from inflector_database.operations_repository import (
    OperationsLeaseError,
    OperationsRepository,
    OperationsStateError,
)

ROOT = Path(__file__).parents[1]
OPERATIONS_PROFILE = ROOT / "config/operations/production_operations_v1.json"
RESEARCH_PROFILE = ROOT / "config/research/production_research_v1.json"
CYCLE_AT = datetime(2026, 10, 2, 14, 30, tzinfo=UTC)


def _plan(
    tmp_path: Path, *, cycle_at: datetime = CYCLE_AT, symbols_text: str = "AAA\nBBB\n"
) -> CyclePlan:
    symbols_token = "-".join(symbols_text.splitlines())
    symbols_file = tmp_path / f"symbols-{cycle_at.timestamp()}-{symbols_token}.txt"
    symbols_file.write_text(symbols_text, encoding="utf-8")
    raw_root = tmp_path / "raw"
    gdelt_root = tmp_path / "gdelt"
    raw_root.mkdir(exist_ok=True)
    gdelt_root.mkdir(exist_ok=True)
    operations = load_operations_profile(OPERATIONS_PROFILE)
    symbols = tuple(
        dict.fromkeys(line.strip().upper() for line in symbols_text.splitlines() if line.strip())
    )
    return plan_cycle(
        CycleInputs(
            operations_profile_path=OPERATIONS_PROFILE,
            research_profile_path=RESEARCH_PROFILE,
            model_family="fictional_operations_v1",
            symbols_file=symbols_file,
            symbols=symbols,
            fiscal_year=2025,
            fiscal_quarter=4,
            cycle_at=cycle_at,
            raw_root=raw_root,
            nse_license_class="fictional-official-source-terms",
            gdelt_raw_root=gdelt_root,
            gdelt_license_class="fictional-public-api-terms",
            model_semantic_version="1.0.0",
            git_sha="a" * 40,
            model_effective_from=datetime(2026, 10, 1, tzinfo=UTC),
        ),
        operations,
        load_research_profile(RESEARCH_PROFILE),
    )


class _Executor:
    def __init__(
        self,
        *,
        failing_stage: str | None = None,
        failing_symbol: str | None = None,
        upstream_symbol_failure: str | None = None,
    ) -> None:
        self.failing_stage = failing_stage
        self.failing_symbol = failing_symbol
        self.upstream_symbol_failure = upstream_symbol_failure
        self.calls: list[str] = []

    def execute(
        self, stage: str, *, session: Session, plan: CyclePlan
    ) -> tuple[dict[str, object], bool]:
        del session
        self.calls.append(stage)
        if stage == self.failing_stage:
            return {"status": "failed", "error": "controlled interruption"}, False
        if stage == "financials" and self.upstream_symbol_failure is not None:
            return {
                "status": "completed_with_symbol_failures",
                "symbol_operational_failures": [self.upstream_symbol_failure],
            }, True
        if stage == "news_attention":
            return {"status": "attention_unavailable"}, True
        if stage == "current_research":
            return {
                "status": "completed",
                "results": [
                    (
                        {"status": "failed", "symbol": symbol, "error": "controlled symbol failure"}
                        if symbol == self.failing_symbol
                        else {
                            "status": "completed",
                            "symbol": symbol,
                            "snapshot_id": None,
                            "snapshot_status": "partial_component_set",
                            "final_score": None,
                            "available_component_codes": ["business_quality"],
                            "missing_component_codes": ["valuation"],
                        }
                    )
                    for symbol in plan.inputs.symbols
                ],
            }, True
        return {"status": "completed", "stage": stage}, True


def _clock(start: datetime = CYCLE_AT):
    current = start

    def tick() -> datetime:
        nonlocal current
        current += timedelta(seconds=1)
        return current

    return tick


def test_operations_profile_is_explicit_deterministic_and_execution_only(tmp_path: Path) -> None:
    first = load_operations_profile(OPERATIONS_PROFILE)
    value = json.loads(OPERATIONS_PROFILE.read_text(encoding="utf-8"))
    reordered = tmp_path / "reordered.json"
    reordered.write_text(
        json.dumps(dict(reversed(tuple(value.items()))), indent=3), encoding="utf-8"
    )
    second = load_operations_profile(reordered)

    assert first.operations_profile_code == "nse_daily_operations_v1"
    assert first.timezone == "Asia/Kolkata"
    assert first.scheduled_local_time == "20:00"
    assert first.market_history_calendar_lookback_days <= 150
    assert first.maximum_symbols_per_cycle <= 100
    assert first.run_lease_seconds == 7200
    assert first.checksum_sha256 == second.checksum_sha256
    assert not {
        "benchmark_code",
        "financial_core_metrics",
        "source_reliability",
        "component_weights",
    }.intersection(value)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("timezone", "Not/AZone"),
        ("scheduled_local_time", "25:00"),
        ("maximum_symbols_per_cycle", 101),
        ("market_history_calendar_lookback_days", 151),
        ("run_lease_seconds", 0),
    ),
)
def test_invalid_operations_profile_fails_closed(
    tmp_path: Path, field: str, invalid: object
) -> None:
    value = json.loads(OPERATIONS_PROFILE.read_text(encoding="utf-8"))
    value[field] = invalid
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_operations_profile(path)


def test_run_identity_is_deterministic_ordered_and_cycle_specific(tmp_path: Path) -> None:
    first = _plan(tmp_path, symbols_text="AAA\nBBB\nAAA\n")
    same = _plan(tmp_path, symbols_text="AAA\nBBB\n")
    later = _plan(tmp_path, cycle_at=CYCLE_AT + timedelta(days=1))
    reordered = _plan(tmp_path, symbols_text="BBB\nAAA\n")
    changed_classification = plan_cycle(
        replace(first.inputs, nse_license_class="different-reviewed-classification"),
        first.operations_profile,
        first.research_profile,
    )

    assert first.inputs.symbols == ("AAA", "BBB")
    assert first.run_key_sha256 == same.run_key_sha256
    assert first.symbol_set_checksum_sha256 == same.symbol_set_checksum_sha256
    assert later.run_key_sha256 != first.run_key_sha256
    assert reordered.symbol_set_checksum_sha256 != first.symbol_set_checksum_sha256
    assert reordered.run_key_sha256 != first.run_key_sha256
    assert changed_classification.run_key_sha256 != first.run_key_sha256
    assert symbol_set_checksum(("AAA", "BBB")) == first.symbol_set_checksum_sha256
    assert "database_url" not in first.persisted_inputs


def test_doctor_is_read_only_and_reports_initialization_requirement(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    session.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
        {"version": "20261002_0016"},
    )
    before = session.scalar(select(func.count()).select_from(OperationalRun))

    result = doctor(session, plan=plan)

    assert result["status"] == "passed"
    assert result["provider_bindings_status"] == "initialization_required"
    assert session.scalar(select(func.count()).select_from(OperationalRun)) == before == 0
    assert not (plan.inputs.raw_root / ".inflector_write_probe").exists()


def test_lease_concurrency_expiry_and_explicit_recovery(session: Session, tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    repository = OperationsRepository(session)
    run = repository.create_run(
        run_key_sha256=plan.run_key_sha256,
        operations_profile_code=plan.operations_profile.operations_profile_code,
        operations_profile_checksum_sha256=plan.operations_profile.checksum_sha256,
        research_profile_code=plan.research_profile.research_profile_code,
        research_profile_checksum_sha256=plan.research_profile.checksum_sha256,
        model_family=plan.inputs.model_family,
        fiscal_year=plan.inputs.fiscal_year,
        fiscal_quarter=plan.inputs.fiscal_quarter,
        cycle_at=plan.inputs.cycle_at,
        knowledge_cutoff=plan.inputs.knowledge_cutoff,
        symbol_set_checksum_sha256=plan.symbol_set_checksum_sha256,
        ordered_symbols=plan.inputs.symbols,
        inputs=plan.persisted_inputs,
        planned_at=CYCLE_AT,
        stages=OPERATIONAL_STAGE_DEFINITIONS,
    )
    repository.acquire_lease(
        run_id=run.id, owner_token="owner-one", acquired_at=CYCLE_AT, lease_seconds=60
    )
    session.commit()

    with pytest.raises(OperationsLeaseError):
        repository.acquire_lease(
            run_id=run.id, owner_token="owner-two", acquired_at=CYCLE_AT, lease_seconds=60
        )
    with pytest.raises(OperationsLeaseError):
        repository.recover_stale_lease(
            run_id=run.id,
            owner_token="owner-two",
            recovered_at=CYCLE_AT + timedelta(seconds=59),
            lease_seconds=60,
        )
    recovered = repository.recover_stale_lease(
        run_id=run.id,
        owner_token="owner-two",
        recovered_at=CYCLE_AT + timedelta(seconds=60),
        lease_seconds=60,
    )
    assert recovered.lease_owner_token == "owner-two"
    assert repository.lease_state(recovered, CYCLE_AT + timedelta(seconds=61)) == "active"


def test_cycle_partial_success_idempotency_and_symbol_isolation(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path, symbols_text="AAA\nBBB\nCCC\n")
    executor = _Executor(failing_symbol="BBB")
    service = ProductionCycleService(session, executor, clock=_clock())

    first, first_code = service.run(plan, owner_token="owner-one")
    repeated, repeated_code = service.run(plan, owner_token="owner-two")

    assert first["status"] == "completed_with_symbol_failures"
    assert first_code == 2
    symbols = cast(list[dict[str, object]], first["symbols"])
    assert [item["symbol"] for item in symbols] == ["AAA", "BBB", "CCC"]
    assert [item["status"] for item in symbols] == ["completed", "failed", "completed"]
    assert symbols[0]["result"]["snapshot_status"] == "partial_component_set"  # type: ignore[index]
    assert symbols[0]["result"]["final_score"] is None  # type: ignore[index]
    assert repeated_code == 2
    assert repeated["already_completed"] is True
    assert repeated["operational_run_id"] == first["operational_run_id"]
    assert executor.calls.count("current_research") == 1


def test_upstream_symbol_failure_does_not_block_later_symbols(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path, symbols_text="AAA\nBBB\nCCC\n")
    executor = _Executor(upstream_symbol_failure="BBB")
    summary, exit_code = ProductionCycleService(session, executor, clock=_clock()).run(
        plan, owner_token="owner"
    )

    assert exit_code == 2
    assert summary["status"] == "completed_with_symbol_failures"
    assert executor.calls[-1] == "current_research"
    symbols = cast(list[dict[str, object]], summary["symbols"])
    assert [item["status"] for item in symbols] == ["completed", "failed", "completed"]
    assert symbols[1]["error_code"] == "upstream_symbol_stage_failed"


def test_required_stage_failure_blocks_then_explicit_resume_reuses_completed_stages(
    session: Session, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    interrupted = _Executor(failing_stage="financials")
    first_service = ProductionCycleService(session, interrupted, clock=_clock())
    first, code = first_service.run(plan, owner_token="owner-one")
    assert code == 1
    assert first["status"] == "failed"
    assert interrupted.calls == ["preflight", "universe", "market_history", "financials"]

    resumed_executor = _Executor()
    resumed_service = ProductionCycleService(
        session, resumed_executor, clock=_clock(CYCLE_AT + timedelta(hours=3))
    )
    resumed, resumed_code = resumed_service.resume(
        UUID(cast(str, first["operational_run_id"])), plan, owner_token="owner-two"
    )
    assert resumed_code == 0
    assert resumed["status"] == "completed"
    assert resumed_executor.calls[0] == "financials"
    assert "preflight" not in resumed_executor.calls
    assert "universe" not in resumed_executor.calls
    assert "market_history" not in resumed_executor.calls
    stages = cast(list[dict[str, object]], resumed["stages"])
    financial = next(item for item in stages if item["stage"] == "financials")
    assert financial["attempt_count"] == 2


def test_conflicting_resume_inputs_fail_closed(session: Session, tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    repository = OperationsRepository(session)
    run = repository.create_run(
        run_key_sha256=plan.run_key_sha256,
        operations_profile_code=plan.operations_profile.operations_profile_code,
        operations_profile_checksum_sha256=plan.operations_profile.checksum_sha256,
        research_profile_code=plan.research_profile.research_profile_code,
        research_profile_checksum_sha256=plan.research_profile.checksum_sha256,
        model_family=plan.inputs.model_family,
        fiscal_year=plan.inputs.fiscal_year,
        fiscal_quarter=plan.inputs.fiscal_quarter,
        cycle_at=plan.inputs.cycle_at,
        knowledge_cutoff=plan.inputs.knowledge_cutoff,
        symbol_set_checksum_sha256=plan.symbol_set_checksum_sha256,
        ordered_symbols=plan.inputs.symbols,
        inputs=plan.persisted_inputs,
        planned_at=CYCLE_AT,
        stages=OPERATIONAL_STAGE_DEFINITIONS,
    )
    conflicting = dict(plan.persisted_inputs)
    conflicting["fiscal_quarter"] = 3
    with pytest.raises(OperationsStateError, match="conflict"):
        repository.assert_immutable_inputs(run, conflicting)

    repository.acquire_lease(
        run_id=run.id, owner_token="owner", acquired_at=CYCLE_AT, lease_seconds=60
    )
    repository.finish_run(run, status="completed", at=CYCLE_AT, summary={})
    with pytest.raises(OperationsStateError, match="illegal"):
        repository.finish_run(run, status="running", at=CYCLE_AT, summary={})


def _snapshot(
    session: Session,
    *,
    company: Company,
    model: ModelVersion,
    configuration: ScoringConfiguration,
    cutoff: datetime,
    fingerprint: str,
    confidence: str,
    available: list[str],
    missing: list[str],
    final_score: str | None,
) -> ScoreSnapshot:
    record = ScoreSnapshot(
        company_id=company.id,
        model_version_id=model.id,
        scoring_configuration_id=configuration.id,
        configuration_checksum_sha256=configuration.checksum_sha256,
        as_of_date=cutoff.date(),
        knowledge_cutoff=cutoff,
        ending_fiscal_year=2025,
        ending_fiscal_quarter=4,
        selected_provider_dataset_id=None,
        selected_filing_scope=None,
        selected_security_id=None,
        snapshot_status="final_score_available" if final_score else "partial_component_set",
        eligibility_eligible=True,
        eligibility_inputs_json={},
        eligibility_reasons_json=[],
        eligibility_warnings_json=[],
        financial_core_coverage=Decimal("1"),
        confidence=Decimal(confidence),
        confidence_inputs_json={},
        confidence_details_json={},
        top_level_component_weight_coverage=Decimal("1" if final_score else "0.95"),
        available_component_codes_json=available,
        missing_component_codes_json=missing,
        context_resolution_json={},
        input_manifest_json={},
        fingerprint_payload_json={},
        snapshot_fingerprint_sha256=fingerprint,
        final_score=Decimal(final_score) if final_score else None,
        algorithm_version="score_snapshot_v5",
    )
    session.add(record)
    session.flush()
    return record


def test_change_projection_initial_unchanged_and_factual_change(session: Session) -> None:
    company = Company(
        legal_name="Fictional Operations Limited",
        display_name="Fictional Operations",
        sector="Industrials",
        industry="Testing",
    )
    model = ModelVersion(
        model_family="operations_change_v1",
        semantic_version="1",
        git_sha="a" * 40,
        status="active",
        activated_at=CYCLE_AT,
    )
    configuration = ScoringConfiguration(
        model_version=model,
        configuration_name="operations",
        configuration_version="1",
        status="active",
        effective_from=CYCLE_AT,
        configuration_json={},
        checksum_sha256="c" * 64,
    )
    session.add_all((company, model))
    session.flush()
    first = _snapshot(
        session,
        company=company,
        model=model,
        configuration=configuration,
        cutoff=CYCLE_AT,
        fingerprint="1" * 64,
        confidence="0.8",
        available=["business_quality"],
        missing=["valuation"],
        final_score=None,
    )
    initial = snapshot_change_report(session, first.id)
    assert initial["status"] == "initial_no_baseline"

    second = _snapshot(
        session,
        company=company,
        model=model,
        configuration=configuration,
        cutoff=CYCLE_AT + timedelta(days=1),
        fingerprint="2" * 64,
        confidence="0.8",
        available=["business_quality"],
        missing=["valuation"],
        final_score=None,
    )
    unchanged = snapshot_change_report(session, second.id, snapshot_created=False)
    assert unchanged["status"] == "unchanged"
    assert unchanged["snapshot_reused"] is True

    third = _snapshot(
        session,
        company=company,
        model=model,
        configuration=configuration,
        cutoff=CYCLE_AT + timedelta(days=2),
        fingerprint="3" * 64,
        confidence="0.75",
        available=["business_quality", "valuation"],
        missing=[],
        final_score="70.675",
    )
    _snapshot(
        session,
        company=company,
        model=model,
        configuration=configuration,
        cutoff=CYCLE_AT + timedelta(days=3),
        fingerprint="4" * 64,
        confidence="0.1",
        available=[],
        missing=["business_quality", "valuation"],
        final_score=None,
    )
    changed = snapshot_change_report(session, third.id)
    assert changed["status"] == "changed"
    assert changed["before"]["confidence"] == "0.8"  # type: ignore[index]
    assert changed["after"]["confidence"] == "0.75"  # type: ignore[index]
    assert changed["before"]["final_score"] is None  # type: ignore[index]
    assert changed["after"]["final_score"] == "70.675"  # type: ignore[index]
    assert not any(
        word in json.dumps(changed).lower()
        for word in ("bullish", "bearish", "attractive", "buy", "sell")
    )


def test_production_e_migration_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "operations-migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "20260929_0015")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "operational_runs" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, "20261002_0016")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert {
            "operational_runs",
            "operational_run_stages",
            "operational_run_symbols",
        }.issubset(inspector.get_table_names())
        assert any(
            constraint["column_names"] == ["run_key_sha256"]
            for constraint in inspector.get_unique_constraints("operational_runs")
        )
        assert {
            key["referred_table"] for key in inspector.get_foreign_keys("operational_run_stages")
        } == {"operational_runs"}
        assert {
            key["referred_table"] for key in inspector.get_foreign_keys("operational_run_symbols")
        } == {
            "operational_runs",
            "score_snapshots",
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260929_0015")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "operational_runs" not in inspect(engine).get_table_names()
        assert "score_snapshots" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_scheduler_wrapper_materializes_one_cycle_time_and_propagates_exit() -> None:
    source = (ROOT / "scripts/run_inflector_scheduled.ps1").read_text(encoding="utf-8")
    assert source.count("[DateTimeOffset]::UtcNow") == 1
    assert "Asia/Kolkata" in source
    assert "exit $exitCode" in source
    assert "INFLECTOR_PRODUCTION_DATABASE_URL" in source
    assert "$env:INFLECTOR_PRODUCTION_DATABASE_URL" in source
    assert "Write-Host $env:INFLECTOR_PRODUCTION_DATABASE_URL" not in source


def test_task_registration_dry_run_creates_no_task() -> None:
    script = ROOT / "scripts/register_inflector_scheduled_task.ps1"
    command_line = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RepositoryPath",
        str(ROOT),
        "-PythonExecutable",
        str(ROOT / ".venv/Scripts/python.exe"),
        "-OperationsProfile",
        str(OPERATIONS_PROFILE),
        "-ResearchProfile",
        str(RESEARCH_PROFILE),
        "-ModelFamily",
        "fictional_operations_v1",
        "-SymbolsFile",
        str(ROOT / "README.md"),
        "-FiscalYear",
        "2025",
        "-FiscalQuarter",
        "4",
        "-ModelSemanticVersion",
        "1.0.0",
        "-GitSha",
        "a" * 40,
        "-ModelEffectiveFrom",
        "2026-10-01T00:00:00Z",
        "-TaskName",
        "Inflector-Fictional-Dry-Run",
        "-DryRun",
    ]
    result = subprocess.run(command_line, capture_output=True, check=True, text=True)
    preview = json.loads(result.stdout)
    assert preview["task_name"] == "Inflector-Fictional-Dry-Run"
    assert preview["one_instance"] is True
    assert preview["start_when_available"] is True
    assert preview["secrets_in_command_line"] is False
    assert "Register-ScheduledTask" not in result.stdout
