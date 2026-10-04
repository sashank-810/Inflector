"""Production H automatic PIT financial-endpoint selection tests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from inflector_data import research_cli
from inflector_data.financial_endpoint import (
    FinancialEndpointResolver,
    load_financial_endpoint_policy,
)
from inflector_data.operations_profile import load_operations_profile
from inflector_data.production_operations import (
    CycleInputs,
    CyclePlan,
    ProductionCycleService,
    plan_cycle,
)
from inflector_data.research_cli import _parser
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    Company,
    DataProvider,
    FinancialFact,
    FinancialFiling,
    FinancialMetricDefinition,
    FiscalPeriod,
    IngestionRun,
    ProviderDataset,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config/financial/production_financial_endpoint_policy_v1.json"
RESEARCH = [
    ROOT / f"config/research/production_research_v{version}.json" for version in range(1, 5)
]
OPERATIONS = [
    ROOT / f"config/operations/production_operations_v{version}.json" for version in range(1, 4)
]
CUTOFF = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _base(session):
    provider = DataProvider(
        code="nse_official",
        provider_type="https",
        licence_name="fictional-reviewed-terms",
        enabled=True,
    )
    dataset = ProviderDataset(
        provider_id=provider.id,
        code="nse_integrated_financials_xbrl",
        licence_class="fictional-reviewed-terms",
        redistributable=False,
    )
    session.add(provider)
    session.flush()
    dataset.provider_id = provider.id
    session.add(dataset)
    session.flush()
    run = IngestionRun(
        provider_dataset_id=dataset.id,
        status="completed",
        started_at=CUTOFF,
        finished_at=CUTOFF,
        records_received=0,
        records_accepted=0,
        records_quarantined=0,
        records_duplicated=0,
    )
    metric = FinancialMetricDefinition(
        code="operating_revenue",
        statement_kind="income",
        unit_category="monetary",
        semantic_type="duration",
    )
    session.add_all((run, metric))
    session.flush()
    return dataset, run, metric


def _company(session, name: str) -> Company:
    company = Company(
        legal_name=name,
        display_name=name,
        sector="Fictional sector",
        industry="Fictional industry",
    )
    session.add(company)
    session.flush()
    return company


def _fact(
    session,
    *,
    dataset,
    run,
    metric,
    company: Company,
    scope: str,
    fiscal_year: int,
    fiscal_quarter: int,
    period_end: date,
    available_at: datetime,
    suffix: str,
    value: str = "1",
) -> None:
    period_start = period_end - timedelta(days=89)
    period = FiscalPeriod(
        company_id=company.id,
        period_kind="quarter",
        period_start=period_start,
        period_end=period_end,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        is_ytd=False,
    )
    filing = FinancialFiling(
        company_id=company.id,
        provider_dataset_id=dataset.id,
        external_filing_id=f"filing-{company.id}-{suffix}",
        filing_type="integrated_financial_results_xbrl",
        filing_scope=scope,
        is_restatement="restatement" in suffix,
        available_at=available_at,
        revision_at=available_at if "restatement" in suffix else None,
    )
    source = SourceRecord(
        ingestion_run_id=run.id,
        provider_dataset_id=dataset.id,
        external_record_id=f"source-{company.id}-{suffix}",
        source_uri=f"https://nsearchives.nseindia.com/corporate/xbrl/{suffix}.xml",
        raw_object_key=f"fictional/{suffix}.xml",
        raw_payload_reference=f"xbrl:RevenueFromOperations:context:{suffix}",
        raw_content_sha256=(suffix.encode().hex() + "0" * 64)[:64],
        content_sha256=(suffix.encode().hex() + "1" * 64)[:64],
        retrieved_at=available_at,
        available_at=available_at,
        parse_status="parsed",
        validation_status="accepted",
    )
    session.add_all((period, filing, source))
    session.flush()
    session.add(
        FinancialFact(
            filing_id=filing.id,
            fiscal_period_id=period.id,
            metric_definition_id=metric.id,
            source_record_id=source.id,
            reported_value=value,
            reported_unit="INR",
            reported_scale="ones",
            reported_currency="INR",
            normalized_value=value,
            normalized_unit="INR",
            available_at=available_at,
            revision_at=filing.revision_at,
        )
    )
    session.flush()


def _resolver(session) -> FinancialEndpointResolver:
    return FinancialEndpointResolver(session, load_financial_endpoint_policy(POLICY_PATH))


def test_endpoint_policy_and_profile_checksums_are_versioned_and_stable(tmp_path: Path) -> None:
    policy = load_financial_endpoint_policy(POLICY_PATH)
    assert policy.financial_endpoint_policy_code == "nse_latest_pit_financial_endpoint_v1"
    assert (
        policy.checksum_sha256 == "8d101bbec5c58c6939cb0650aa7ed428d9aa2b34ea01913aa48a2b5ce84661d5"
    )
    reordered = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(dict(reversed(tuple(reordered.items())))), encoding="utf-8")
    assert load_financial_endpoint_policy(path).checksum_sha256 == policy.checksum_sha256
    reordered["completeness_handling"] = "prefer_complete"
    path.write_text(json.dumps(reordered), encoding="utf-8")
    with pytest.raises(ValueError, match="completeness_handling"):
        load_financial_endpoint_policy(path)

    research = [load_research_profile(path) for path in RESEARCH]
    assert [item.checksum_sha256 for item in research[:3]] == [
        "182d139eaf6a6603b3fa817cdc590676454d2e4051e49a495a32591747f51f80",
        "ed14c7c3aa73278793da16d2d6a1de23ea19274f98bd91b64908fedbee9677b8",
        "a1510f311babe3edcab242b228f79fc6353e34a399bc8145aec6764909eb4676",
    ]
    assert (
        research[3].checksum_sha256
        == "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
    )
    assert research[3].financial_primitive_policy_checksum_sha256 == (
        research[2].financial_primitive_policy_checksum_sha256
    )
    operations = [load_operations_profile(path) for path in OPERATIONS]
    assert [item.checksum_sha256 for item in operations] == [
        "ffefef5ba249ef93eb8ac7db26297a579ed6d59ad02204ad263a7bc354d39d4c",
        "129392617256d4808b03bacf138ef54d020329fcece9d6738c28d28169716f23",
        "fc2db825f7397f4b6aee76155c45ba01e3db082d15b0418badee9c47b8c50976",
    ]
    assert operations[2].automatic_financial_endpoint is True


def test_latest_visible_endpoint_ignores_completeness_and_future_rows(session) -> None:
    dataset, run, metric = _base(session)
    company = _company(session, "Fictional Latest Limited")
    _fact(
        session,
        dataset=dataset,
        run=run,
        metric=metric,
        company=company,
        scope="consolidated",
        fiscal_year=2026,
        fiscal_quarter=1,
        period_end=date(2026, 6, 30),
        available_at=CUTOFF - timedelta(days=40),
        suffix="q1-complete-metric-1",
    )
    for code, statement, semantic in (
        ("pat", "income", "duration"),
        ("cash_flow_from_operations", "cash_flow", "duration"),
        ("total_equity", "balance_sheet", "instant"),
    ):
        additional_metric = FinancialMetricDefinition(
            code=code,
            statement_kind=statement,
            unit_category="monetary",
            semantic_type=semantic,
        )
        session.add(additional_metric)
        session.flush()
        _fact(
            session,
            dataset=dataset,
            run=run,
            metric=additional_metric,
            company=company,
            scope="consolidated",
            fiscal_year=2026,
            fiscal_quarter=1,
            period_end=date(2026, 6, 30),
            available_at=CUTOFF - timedelta(days=40),
            suffix=f"q1-complete-{code}",
        )
    _fact(
        session,
        dataset=dataset,
        run=run,
        metric=metric,
        company=company,
        scope="consolidated",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 9, 30),
        available_at=CUTOFF - timedelta(days=1),
        suffix="q2-partial-only-one-metric",
    )
    _fact(
        session,
        dataset=dataset,
        run=run,
        metric=metric,
        company=company,
        scope="consolidated",
        fiscal_year=2026,
        fiscal_quarter=3,
        period_end=date(2026, 12, 31),
        available_at=CUTOFF - timedelta(hours=1),
        suffix="future-q3",
    )
    result = _resolver(session).resolve(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        filing_scope_priority=("consolidated", "standalone"),
        knowledge_cutoff=CUTOFF,
    )
    assert (result.status, result.fiscal_year, result.fiscal_quarter) == ("selected", 2026, 2)
    assert result.excluded_future_candidates == 1


def test_per_company_cutoff_scope_revision_no_endpoint_and_ambiguity(session) -> None:
    dataset, run, metric = _base(session)
    endpoint_cutoff = datetime(2028, 1, 1, tzinfo=UTC)
    endpoints = (("AAA", 2027, 2), ("BBB", 2027, 1), ("CCC", 2026, 4))
    for name, year, quarter in endpoints:
        company = _company(session, name)
        end = {1: date(2027, 6, 30), 2: date(2027, 9, 30), 4: date(2027, 3, 31)}[quarter]
        if name == "CCC":
            end = date(2027, 3, 31)
        _fact(
            session,
            dataset=dataset,
            run=run,
            metric=metric,
            company=company,
            scope="consolidated",
            fiscal_year=year,
            fiscal_quarter=quarter,
            period_end=end,
            available_at=CUTOFF - timedelta(days=1),
            suffix=f"{name}-endpoint",
        )
        result = _resolver(session).resolve(
            provider_dataset_id=dataset.id,
            company_id=company.id,
            filing_scope_priority=("consolidated", "standalone"),
            knowledge_cutoff=endpoint_cutoff,
        )
        assert (result.fiscal_year, result.fiscal_quarter) == (year, quarter)

    scope_company = _company(session, "SCOPE")
    _fact(
        session,
        dataset=dataset,
        run=run,
        metric=metric,
        company=scope_company,
        scope="consolidated",
        fiscal_year=2025,
        fiscal_quarter=4,
        period_end=date(2026, 3, 31),
        available_at=CUTOFF - timedelta(days=2),
        suffix="scope-consolidated",
    )
    _fact(
        session,
        dataset=dataset,
        run=run,
        metric=metric,
        company=scope_company,
        scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=1,
        period_end=date(2026, 6, 30),
        available_at=CUTOFF - timedelta(days=1),
        suffix="scope-standalone-newer",
    )
    selected_scope = _resolver(session).resolve(
        provider_dataset_id=dataset.id,
        company_id=scope_company.id,
        filing_scope_priority=("consolidated", "standalone"),
        knowledge_cutoff=CUTOFF,
    )
    assert (
        selected_scope.filing_scope,
        selected_scope.fiscal_year,
        selected_scope.fiscal_quarter,
    ) == ("consolidated", 2025, 4)

    invisible = _company(session, "INVISIBLE")
    _fact(
        session,
        dataset=dataset,
        run=run,
        metric=metric,
        company=invisible,
        scope="consolidated",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 9, 30),
        available_at=CUTOFF + timedelta(seconds=1),
        suffix="post-cutoff",
    )
    assert (
        _resolver(session)
        .resolve(
            provider_dataset_id=dataset.id,
            company_id=invisible.id,
            filing_scope_priority=("consolidated", "standalone"),
            knowledge_cutoff=CUTOFF,
        )
        .status
        == "no_pit_visible_financial_endpoint"
    )

    ambiguous = _company(session, "AMBIGUOUS")
    for end, suffix in ((date(2026, 9, 29), "ambiguous-a"), (date(2026, 9, 30), "ambiguous-b")):
        _fact(
            session,
            dataset=dataset,
            run=run,
            metric=metric,
            company=ambiguous,
            scope="consolidated",
            fiscal_year=2026,
            fiscal_quarter=2,
            period_end=end,
            available_at=CUTOFF - timedelta(days=1),
            suffix=suffix,
        )
    assert (
        _resolver(session)
        .resolve(
            provider_dataset_id=dataset.id,
            company_id=ambiguous.id,
            filing_scope_priority=("consolidated", "standalone"),
            knowledge_cutoff=CUTOFF,
        )
        .status
        == "ambiguous_financial_endpoint"
    )


def test_same_endpoint_revision_visibility_does_not_change_endpoint(session) -> None:
    dataset, run, metric = _base(session)
    company = _company(session, "Fictional Restatement Limited")
    original_at = CUTOFF - timedelta(days=10)
    restated_at = CUTOFF + timedelta(days=10)
    for available, suffix, value in (
        (original_at, "original", "10"),
        (restated_at, "restatement", "12"),
    ):
        _fact(
            session,
            dataset=dataset,
            run=run,
            metric=metric,
            company=company,
            scope="consolidated",
            fiscal_year=2026,
            fiscal_quarter=2,
            period_end=date(2026, 9, 30),
            available_at=available,
            suffix=suffix,
            value=value,
        )
    before = _resolver(session).resolve(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        filing_scope_priority=("consolidated", "standalone"),
        knowledge_cutoff=CUTOFF,
    )
    after = _resolver(session).resolve(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        filing_scope_priority=("consolidated", "standalone"),
        knowledge_cutoff=restated_at,
    )
    assert (before.fiscal_year, before.fiscal_quarter) == (2026, 2)
    assert (after.fiscal_year, after.fiscal_quarter) == (2026, 2)
    assert before.filing_external_ids != after.filing_external_ids


def test_auto_operations_identity_and_cli_are_explicit(tmp_path: Path) -> None:
    symbols = tmp_path / "symbols.txt"
    symbols.write_text("AAA\nBBB\n", encoding="utf-8")
    raw = tmp_path / "raw"
    gdelt = tmp_path / "gdelt"
    raw.mkdir()
    gdelt.mkdir()
    inputs = CycleInputs(
        operations_profile_path=OPERATIONS[2],
        research_profile_path=RESEARCH[3],
        model_family="fictional_auto_v1",
        symbols_file=symbols,
        symbols=("AAA", "BBB"),
        fiscal_year=None,
        fiscal_quarter=None,
        cycle_at=CUTOFF,
        raw_root=raw,
        nse_license_class="fictional-reviewed-terms",
        gdelt_raw_root=gdelt,
        gdelt_license_class="fictional-gdelt-terms",
        model_semantic_version="1",
        git_sha="a" * 40,
        model_effective_from=CUTOFF,
    )
    plan = plan_cycle(
        inputs,
        load_operations_profile(OPERATIONS[2]),
        load_research_profile(RESEARCH[3]),
    )
    same = plan_cycle(
        inputs,
        load_operations_profile(OPERATIONS[2]),
        load_research_profile(RESEARCH[3]),
    )
    assert plan.run_key_sha256 == same.run_key_sha256
    assert plan.persisted_inputs["financial_endpoint_mode"] == "automatic"
    assert plan.persisted_inputs["fiscal_year"] is None
    endpoint_checksum = plan.research_profile.financial_endpoint_policy_checksum_sha256
    assert endpoint_checksum is not None
    assert endpoint_checksum in json.dumps(plan.persisted_inputs)
    with pytest.raises(ValueError, match="endpoint mode"):
        plan_cycle(
            replace(inputs, fiscal_year=2026, fiscal_quarter=2),
            load_operations_profile(OPERATIONS[2]),
            load_research_profile(RESEARCH[3]),
        )


def test_operations_v3_accepts_no_endpoint_and_reuses_completed_cycle(
    session, tmp_path: Path
) -> None:
    symbols = tmp_path / "symbols-v3.txt"
    symbols.write_text("AAA\nBBB\n", encoding="utf-8")
    raw = tmp_path / "raw-v3"
    gdelt = tmp_path / "gdelt-v3"
    raw.mkdir()
    gdelt.mkdir()
    plan = plan_cycle(
        CycleInputs(
            operations_profile_path=OPERATIONS[2],
            research_profile_path=RESEARCH[3],
            model_family="fictional_auto_operations_v1",
            symbols_file=symbols,
            symbols=("AAA", "BBB"),
            fiscal_year=None,
            fiscal_quarter=None,
            cycle_at=CUTOFF,
            raw_root=raw,
            nse_license_class="fictional-reviewed-terms",
            gdelt_raw_root=gdelt,
            gdelt_license_class="fictional-gdelt-terms",
            model_semantic_version="1",
            git_sha="b" * 40,
            model_effective_from=CUTOFF,
        ),
        load_operations_profile(OPERATIONS[2]),
        load_research_profile(RESEARCH[3]),
    )

    class Executor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute(
            self, stage: str, *, session: Session, plan: CyclePlan
        ) -> tuple[dict[str, object], bool]:
            del session
            self.calls.append(stage)
            if stage == "current_research":
                return {
                    "status": "completed",
                    "results": [
                        {
                            "status": "completed",
                            "symbol": "AAA",
                            "financial_endpoint_status": ("no_pit_visible_financial_endpoint"),
                            "snapshot_id": None,
                            "final_score": None,
                        },
                        {
                            "status": "completed",
                            "symbol": "BBB",
                            "financial_endpoint_status": "selected",
                            "selected_fiscal_year": 2026,
                            "selected_fiscal_quarter": 2,
                            "snapshot_id": None,
                            "snapshot_status": "partial_component_set",
                            "final_score": None,
                        },
                    ],
                }, True
            return {"status": "completed"}, True

    executor = Executor()
    tick = iter(CUTOFF + timedelta(seconds=value) for value in range(100))
    service = ProductionCycleService(session, executor, clock=lambda: next(tick))
    first, first_code = service.run(plan, owner_token="owner-one")
    repeated, repeated_code = service.run(plan, owner_token="owner-two")

    assert first_code == repeated_code == 0
    assert first["status"] == "completed"
    assert first["fiscal_year"] is None and first["fiscal_quarter"] is None
    assert first["financial_endpoint_mode"] == "automatic"
    assert executor.calls.index("financials") < executor.calls.index("current_research")
    assert repeated["already_completed"] is True
    assert executor.calls.count("current_research") == 1


def test_auto_mode_delegates_selected_endpoint_to_existing_manual_path(monkeypatch) -> None:
    policy = load_financial_endpoint_policy(POLICY_PATH)
    company_id = uuid4()
    endpoint = research_cli.FinancialEndpointResult(
        company_id=company_id,
        status="selected",
        filing_scope="consolidated",
        fiscal_year=2026,
        fiscal_quarter=2,
        period_end=date(2026, 9, 30),
        knowledge_cutoff=CUTOFF,
        available_at=CUTOFF - timedelta(days=1),
        filing_external_ids=("fictional-filing",),
        source_record_ids=(uuid4(),),
        excluded_future_candidates=0,
        policy_code=policy.financial_endpoint_policy_code,
        policy_checksum_sha256=policy.checksum_sha256,
    )
    profile = SimpleNamespace(financial_scope_priority=("consolidated", "standalone"))
    identity = SimpleNamespace(
        security=SimpleNamespace(
            company_id=company_id,
            isin="INE0AUTO0019",
            company=SimpleNamespace(legal_name="Fictional Auto Limited"),
        )
    )

    class Assembler:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def resolve_symbol(self, symbol: str):
            assert symbol == "AUTO"
            return identity

    class Resolver:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def resolve(self, **kwargs):
            assert kwargs["knowledge_cutoff"] == CUTOFF
            return endpoint

    observed: dict[str, object] = {}

    def explicit_path(session, *, args, symbol, repository_root, endpoint_result=None):
        del session, repository_root
        observed.update(
            fiscal_year=args.fiscal_year,
            fiscal_quarter=args.fiscal_quarter,
            symbol=symbol,
            endpoint_result=endpoint_result,
        )
        return {"status": "completed", "symbol": symbol}

    monkeypatch.setattr(research_cli, "load_research_profile", lambda path: profile)
    monkeypatch.setattr(
        research_cli, "load_profile_financial_endpoint_policy", lambda profile, root: policy
    )
    monkeypatch.setattr(
        research_cli, "load_profile_financial_primitive_policy", lambda profile, root: None
    )
    monkeypatch.setattr(research_cli, "ProductionResearchAssembler", Assembler)
    monkeypatch.setattr(
        research_cli, "resolve_profile_datasets", lambda session, profile: {"financial": uuid4()}
    )
    monkeypatch.setattr(research_cli, "FinancialEndpointResolver", Resolver)
    monkeypatch.setattr(research_cli, "_run_symbol", explicit_path)

    result = research_cli._run_symbol_auto(
        cast(Session, object()),
        args=argparse.Namespace(research_profile=RESEARCH[3], knowledge_cutoff=CUTOFF),
        symbol="AUTO",
        repository_root=ROOT,
    )
    assert result["status"] == "completed"
    assert observed == {
        "fiscal_year": 2026,
        "fiscal_quarter": 2,
        "symbol": "AUTO",
        "endpoint_result": endpoint,
    }

    auto = _parser().parse_args(
        [
            "run-current-auto",
            "--database-url",
            "sqlite://",
            "--model-family",
            "m",
            "--research-profile",
            str(RESEARCH[3]),
            "--symbol",
            "AAA",
            "--knowledge-cutoff",
            CUTOFF.isoformat(),
        ]
    )
    assert auto.command == "run-current-auto"
    assert not hasattr(auto, "fiscal_year")
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "run-current",
                "--database-url",
                "sqlite://",
                "--model-family",
                "m",
                "--research-profile",
                str(RESEARCH[2]),
                "--symbol",
                "AAA",
                "--knowledge-cutoff",
                CUTOFF.isoformat(),
            ]
        )


def test_operations_v3_task_dry_run_requires_no_global_fiscal_endpoint(
    powershell_executable: str,
) -> None:
    script = ROOT / "scripts/register_inflector_scheduled_task.ps1"
    command_line = [
        powershell_executable,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-RepositoryPath",
        str(ROOT),
        "-PythonExecutable",
        sys.executable,
        "-OperationsProfile",
        str(OPERATIONS[2]),
        "-ResearchProfile",
        str(RESEARCH[3]),
        "-ModelFamily",
        "fictional_auto_v1",
        "-SymbolsFile",
        str(ROOT / "README.md"),
        "-ModelSemanticVersion",
        "1.0.0",
        "-GitSha",
        "a" * 40,
        "-ModelEffectiveFrom",
        "2026-10-01T00:00:00Z",
        "-TaskName",
        "Inflector-Fictional-Auto-Dry-Run",
        "-DryRun",
    ]
    result = subprocess.run(command_line, capture_output=True, check=True, text=True)
    preview = json.loads(result.stdout)
    assert "-FiscalYear" not in preview["arguments"]
    assert "-FiscalQuarter" not in preview["arguments"]
    assert preview["secrets_in_command_line"] is False
