"""Production K deterministic current V5 opportunity discovery contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from inflector_core.score_audit import audit_fingerprint_sha256, canonical_audit_value
from inflector_data.opportunity_discovery import (
    DiscoveryDecision,
    assign_dense_score_ranks,
    build_opportunity_discovery,
    normalize_discovery_symbols,
    select_current_snapshots,
    selected_snapshot_set_checksum,
    symbol_set_checksum,
)
from inflector_data.opportunity_policy import (
    canonical_json_sha256,
    load_opportunity_discovery_policy,
)
from inflector_data.opportunity_summary import summarize_opportunity_discovery
from inflector_data.production_policy import initialize_production_model
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    BacktestOutcome,
    Company,
    DataProvider,
    ExchangeListing,
    ModelVersion,
    ProviderDataset,
    ScoreComponent,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
)
from inflector_database.opportunity_repository import (
    OpportunityDiscoveryIntegrityError,
    OpportunityDiscoveryItemWrite,
    OpportunityDiscoveryRepository,
)
from inflector_database.score_repository import (
    V5_COMPONENT_ALGORITHM_VERSIONS,
    V5_COMPONENT_ORDER,
    V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
)

ROOT = Path(__file__).parents[1]
POLICY = ROOT / "config/opportunity/production_opportunity_discovery_v1.json"
PROFILE = ROOT / "config/research/production_research_v4.json"
CUTOFF = datetime(2026, 10, 3, 18, tzinfo=UTC)
CONFIG_CHECKSUM = "a" * 64
WEIGHTS = {
    "financial_inflection": Decimal("0.25"),
    "business_catalyst": Decimal("0.20"),
    "business_quality": Decimal("0.15"),
    "cash_flow_quality": Decimal("0.10"),
    "balance_sheet": Decimal("0.10"),
    "valuation": Decimal("0.10"),
    "market_structure": Decimal("0.05"),
    "low_market_attention": Decimal("0.05"),
}


def _configuration(session: Session, *, checksum: str = CONFIG_CHECKSUM):
    model = ModelVersion(
        model_family=f"opportunity-{uuid4()}",
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
        checksum_sha256=checksum,
    )
    session.add(configuration)
    session.flush()
    return model, configuration


def _identity(session: Session, symbol: str):
    company = Company(
        legal_name=f"Fictional {symbol} {uuid4()} Limited",
        display_name=f"Fictional {symbol}",
        sector="Industrials",
        industry="Equipment",
    )
    session.add(company)
    session.flush()
    security = Security(
        company_id=company.id,
        isin=f"IN{uuid4().hex[:10].upper()}",
        security_type="equity",
        status="active",
    )
    session.add(security)
    session.flush()
    listing = ExchangeListing(
        security_id=security.id,
        exchange="NSE",
        symbol=symbol,
        valid_from=date(2020, 1, 1),
        status="active",
    )
    session.add(listing)
    session.flush()
    return company, security


def _snapshot(
    session: Session,
    *,
    company: Company,
    security: Security,
    configuration: ScoringConfiguration,
    cutoff: datetime,
    score: Decimal | None,
    confidence: Decimal = Decimal("0.5"),
    eligible: bool = True,
    token: str = "snapshot",
) -> ScoreSnapshot:
    if not eligible:
        status = "ineligible"
        available: list[str] = []
        missing = list(V5_COMPONENT_ORDER)
    elif score is None:
        status = "partial_component_set"
        available = [V5_COMPONENT_ORDER[0]]
        missing = list(V5_COMPONENT_ORDER[1:])
    else:
        status = "final_score_available"
        available = list(V5_COMPONENT_ORDER)
        missing = []
    component_state: list[dict[str, object]] = []
    if status == "final_score_available":
        assert score is not None
        component_state = [
            {
                "component_code": code,
                "score": score,
                "configured_top_level_weight": WEIGHTS[code],
                "final_contribution": score * WEIGHTS[code],
            }
            for code in V5_COMPONENT_ORDER
        ]
    elif status == "partial_component_set":
        component_state = [
            {
                "component_code": V5_COMPONENT_ORDER[0],
                "score": Decimal("99"),
                "configured_top_level_weight": WEIGHTS[V5_COMPONENT_ORDER[0]],
                "final_contribution": None,
            }
        ]
    payload = {
        "algorithm_version": "score_snapshot_v5",
        "fixture_token": token,
        "opportunity_score_aggregation_version": V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
        "final_score_state": {
            "aggregation_version": V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
            "required_positive_weight_component_codes": list(V5_COMPONENT_ORDER),
            "components": canonical_audit_value(component_state),
            "final_score": canonical_audit_value(score),
        },
    }
    snapshot = ScoreSnapshot(
        company_id=company.id,
        model_version_id=configuration.model_version_id,
        scoring_configuration_id=configuration.id,
        configuration_checksum_sha256=configuration.checksum_sha256,
        as_of_date=cutoff.date(),
        knowledge_cutoff=cutoff,
        ending_fiscal_year=2026,
        ending_fiscal_quarter=2,
        selected_filing_scope="consolidated",
        selected_security_id=security.id,
        snapshot_status=status,
        eligibility_eligible=eligible,
        eligibility_inputs_json={},
        eligibility_reasons_json=[] if eligible else ["fixture_ineligible"],
        eligibility_warnings_json=[],
        financial_core_coverage=Decimal("1"),
        confidence=confidence,
        confidence_inputs_json={},
        confidence_details_json={},
        top_level_component_weight_coverage=sum(
            (WEIGHTS[code] for code in available), Decimal("0")
        ),
        available_component_codes_json=available,
        missing_component_codes_json=missing,
        context_resolution_json={},
        input_manifest_json={},
        fingerprint_payload_json=payload,
        snapshot_fingerprint_sha256=audit_fingerprint_sha256(payload),
        final_score=score,
        algorithm_version="score_snapshot_v5",
    )
    session.add(snapshot)
    session.flush()
    for item in component_state:
        code = str(item["component_code"])
        component_score = item["score"]
        component_contribution = item["final_contribution"]
        assert isinstance(component_score, Decimal)
        assert component_contribution is None or isinstance(component_contribution, Decimal)
        session.add(
            ScoreComponent(
                score_snapshot_id=snapshot.id,
                component_code=code,
                score=component_score,
                unit="score_0_100",
                configured_top_level_weight=WEIGHTS[code],
                subfactor_weight_coverage=Decimal("1"),
                final_contribution=component_contribution,
                available_at=cutoff,
                algorithm_version=V5_COMPONENT_ALGORITHM_VERSIONS[code],
                missing_subfactors_json=[],
                warnings_json=[],
                detail_json={},
            )
        )
    session.flush()
    session.refresh(snapshot)
    return snapshot


def _select(session: Session, symbols: tuple[str, ...], configuration: ScoringConfiguration):
    policy = load_opportunity_discovery_policy(POLICY, repository_root=ROOT)
    return select_current_snapshots(
        session,
        symbols=symbols,
        discovery_cutoff=CUTOFF,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        maximum_snapshot_age_days=7,
        unranked_reason_priority=policy.unranked_reason_priority,
    )


def test_discovery_policy_is_bound_versioned_and_deterministic(tmp_path: Path) -> None:
    policy = load_opportunity_discovery_policy(POLICY, repository_root=ROOT)
    assert policy.code == "production_opportunity_discovery_v1"
    assert policy.maximum_snapshot_age_days == 7
    assert policy.ranking_key == "final_score_desc"
    assert policy.display_tie_order == ("symbol_asc", "security_id_asc")
    assert policy.maximum_symbols == 5000
    reordered = dict(reversed(tuple(json.loads(POLICY.read_text()).items())))
    copy = tmp_path / "policy.json"
    copy.write_text(json.dumps(reordered), encoding="utf-8")
    assert (
        load_opportunity_discovery_policy(copy, repository_root=ROOT).checksum_sha256
        == policy.checksum_sha256
    )
    changed = dict(reordered)
    changed["maximum_snapshot_age_days"] = 8
    assert canonical_json_sha256(changed) != policy.checksum_sha256


def test_symbol_normalization_and_identity_are_ordered_and_bounded() -> None:
    symbols = normalize_discovery_symbols([" bbb ", "AAA", "BBB"], maximum=2)
    assert symbols == ("BBB", "AAA")
    assert symbol_set_checksum(symbols) != symbol_set_checksum(tuple(reversed(symbols)))


def test_latest_visible_snapshot_wins_without_best_score_cherry_pick(session: Session) -> None:
    _, configuration = _configuration(session)
    company, security = _identity(session, "AAA")
    old = _snapshot(
        session,
        company=company,
        security=security,
        configuration=configuration,
        cutoff=CUTOFF - timedelta(days=3),
        score=Decimal("90"),
        token="old-high",
    )
    current = _snapshot(
        session,
        company=company,
        security=security,
        configuration=configuration,
        cutoff=CUTOFF - timedelta(days=1),
        score=Decimal("55"),
        token="new-low",
    )
    _snapshot(
        session,
        company=company,
        security=security,
        configuration=configuration,
        cutoff=CUTOFF + timedelta(days=1),
        score=Decimal("99"),
        token="future",
    )
    selected = _select(session, ("AAA",), configuration)[0]
    assert selected.snapshot is not None
    assert selected.snapshot.id == current.id
    assert selected.snapshot.id != old.id
    assert selected.snapshot.final_score == Decimal("55")


def test_freshness_partial_and_ineligible_are_explicit_unranked_states(
    session: Session,
) -> None:
    _, configuration = _configuration(session)
    cases = (
        ("FRESH", 6, Decimal("70"), True, None),
        ("STALE", 8, Decimal("95"), True, "stale_snapshot"),
        ("PART", 1, None, True, "partial_score"),
        ("INEL", 1, None, False, "v5_ineligible"),
    )
    for symbol, age, score, eligible, _ in cases:
        company, security = _identity(session, symbol)
        _snapshot(
            session,
            company=company,
            security=security,
            configuration=configuration,
            cutoff=CUTOFF - timedelta(days=age),
            score=score,
            eligible=eligible,
            token=symbol,
        )
    decisions = {
        item.symbol: item
        for item in _select(session, tuple(x[0] for x in cases), configuration)
    }
    assert decisions["FRESH"].rankable is True
    assert decisions["FRESH"].snapshot_age_days == 6
    assert decisions["STALE"].unranked_reason == "stale_snapshot"
    assert decisions["PART"].unranked_reason == "partial_score"
    assert decisions["PART"].score_rank is None
    assert decisions["INEL"].unranked_reason == "v5_ineligible"


def test_dense_rank_uses_only_final_score_and_display_ties_are_deterministic(
    session: Session,
) -> None:
    _, configuration = _configuration(session)
    for symbol, score, confidence in (
        ("BBB", Decimal("82"), Decimal("0.9")),
        ("AAA", Decimal("82"), Decimal("0.1")),
        ("CCC", Decimal("79"), Decimal("1")),
        ("PART", None, Decimal("1")),
    ):
        company, security = _identity(session, symbol)
        _snapshot(
            session,
            company=company,
            security=security,
            configuration=configuration,
            cutoff=CUTOFF - timedelta(days=1),
            score=score,
            confidence=confidence,
            token=symbol,
        )
    decisions = {
        item.symbol: item
        for item in _select(session, ("BBB", "AAA", "CCC", "PART"), configuration)
    }
    assert decisions["AAA"].score_rank == decisions["BBB"].score_rank == 1
    assert decisions["AAA"].display_order == 1
    assert decisions["BBB"].display_order == 2
    assert decisions["CCC"].score_rank == 2
    assert decisions["PART"].score_rank is None


def test_ambiguity_configuration_mismatch_and_missing_states_fail_closed(
    session: Session,
) -> None:
    _, configuration = _configuration(session)
    _, wrong = _configuration(session, checksum="c" * 64)
    company, security = _identity(session, "AMB")
    for score in (Decimal("70"), Decimal("71")):
        _snapshot(
            session,
            company=company,
            security=security,
            configuration=configuration,
            cutoff=CUTOFF - timedelta(days=1),
            score=score,
            token=f"amb-{score}",
        )
    company, security = _identity(session, "WRONG")
    _snapshot(
        session,
        company=company,
        security=security,
        configuration=wrong,
        cutoff=CUTOFF - timedelta(days=1),
        score=Decimal("100"),
        token="wrong-config",
    )
    _identity(session, "NONE")
    decisions = {
        item.symbol: item
        for item in _select(session, ("AMB", "WRONG", "NONE", "UNKNOWN"), configuration)
    }
    assert decisions["AMB"].unranked_reason == "ambiguous_latest_snapshot"
    assert decisions["WRONG"].unranked_reason == "configuration_mismatch"
    assert decisions["NONE"].unranked_reason == "no_visible_snapshot"
    assert decisions["UNKNOWN"].unranked_reason == "identity_unavailable"


def test_repository_is_idempotent_and_conflicting_item_fails_closed(session: Session) -> None:
    _, configuration = _configuration(session)
    repository = OpportunityDiscoveryRepository(session)
    first, created = repository.create_run(
        run_key_sha256="1" * 64,
        discovery_policy_code="production_opportunity_discovery_v1",
        discovery_policy_checksum_sha256="2" * 64,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256="3" * 64,
        financial_primitive_policy_checksum_sha256="4" * 64,
        financial_endpoint_policy_checksum_sha256="5" * 64,
        model_family="fictional",
        discovery_cutoff=CUTOFF,
        universe_mode="explicit_symbols_file",
        ordered_symbols=("AAA",),
        symbol_set_checksum_sha256="6" * 64,
        selected_snapshot_set_checksum_sha256="7" * 64,
        snapshot_selection_version="latest_visible_bound_score_snapshot_v1",
        ranking_version="v5_final_score_dense_rank_v1",
        inputs_json={"stable": True},
    )
    second, created_again = repository.create_run(
        run_key_sha256="1" * 64,
        discovery_policy_code="production_opportunity_discovery_v1",
        discovery_policy_checksum_sha256="2" * 64,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256="3" * 64,
        financial_primitive_policy_checksum_sha256="4" * 64,
        financial_endpoint_policy_checksum_sha256="5" * 64,
        model_family="fictional",
        discovery_cutoff=CUTOFF,
        universe_mode="explicit_symbols_file",
        ordered_symbols=("AAA",),
        symbol_set_checksum_sha256="6" * 64,
        selected_snapshot_set_checksum_sha256="7" * 64,
        snapshot_selection_version="latest_visible_bound_score_snapshot_v1",
        ranking_version="v5_final_score_dense_rank_v1",
        inputs_json={"stable": True},
    )
    assert created is True and created_again is False and second.id == first.id
    value = OpportunityDiscoveryItemWrite(
        company_id=None,
        security_id=None,
        score_snapshot_id=None,
        symbol="AAA",
        rankable=False,
        unranked_reason="identity_unavailable",
        score_rank=None,
        display_order=None,
        snapshot_age_days=None,
        freshness_state="unavailable",
        selected_snapshot_fingerprint_sha256=None,
        detail_json={},
    )
    item, item_created = repository.add_item(first, value)
    same, duplicate = repository.add_item(first, value)
    assert item_created is True and duplicate is False and same.id == item.id
    with pytest.raises(OpportunityDiscoveryIntegrityError):
        repository.add_item(first, replace(value, unranked_reason="no_visible_snapshot"))
    summary: dict[str, object] = {
        "requested_securities": 1,
        "rankable_securities": 0,
        "unranked_securities": 1,
    }
    repository.complete(first, summary=summary)
    repository.complete(first, summary=summary)
    assert first.status == "completed"


def test_complete_discovery_run_is_idempotent_and_snapshot_set_changes_identity(
    session: Session,
) -> None:
    policy = load_opportunity_discovery_policy(POLICY, repository_root=ROOT)
    profile = load_research_profile(PROFILE)
    providers: dict[str, DataProvider] = {}
    for binding in profile.provider_datasets.values():
        if binding is None or binding.provider_code in providers:
            continue
        provider = DataProvider(
            code=binding.provider_code,
            provider_type="https",
            licence_name="fictional-reviewed",
            enabled=True,
        )
        providers[binding.provider_code] = provider
        session.add(provider)
    session.flush()
    for binding in profile.provider_datasets.values():
        if binding is None:
            continue
        session.add(
            ProviderDataset(
                provider_id=providers[binding.provider_code].id,
                code=binding.dataset_code,
                licence_class="fictional-reviewed",
                redistributable=False,
            )
        )
    session.flush()
    initialized = initialize_production_model(
        session,
        profile=profile,
        repository_root=ROOT,
        model_family="production-k-fictional",
        model_semantic_version="5",
        git_sha="d" * 40,
        effective_from=CUTOFF - timedelta(days=30),
    )
    company, security = _identity(session, "AAA")
    _snapshot(
        session,
        company=company,
        security=security,
        configuration=initialized.scoring_configuration,
        cutoff=CUTOFF - timedelta(days=1),
        score=Decimal("80"),
        token="first-selected",
    )
    first = build_opportunity_discovery(
        session,
        policy=policy,
        research_profile=profile,
        model_family="production-k-fictional",
        symbols=("AAA",),
        discovery_cutoff=CUTOFF,
        repository_root=ROOT,
    )
    repeated = build_opportunity_discovery(
        session,
        policy=policy,
        research_profile=profile,
        model_family="production-k-fictional",
        symbols=("AAA",),
        discovery_cutoff=CUTOFF,
        repository_root=ROOT,
    )
    assert repeated.run_id == first.run_id
    assert repeated.already_completed is True
    _snapshot(
        session,
        company=company,
        security=security,
        configuration=initialized.scoring_configuration,
        cutoff=CUTOFF,
        score=Decimal("70"),
        token="new-selected",
    )
    changed = build_opportunity_discovery(
        session,
        policy=policy,
        research_profile=profile,
        model_family="production-k-fictional",
        symbols=("AAA",),
        discovery_cutoff=CUTOFF,
        repository_root=ROOT,
    )
    assert changed.run_id != first.run_id
    assert changed.run_key_sha256 != first.run_key_sha256
    assert (
        changed.selected_snapshot_set_checksum_sha256
        != first.selected_snapshot_set_checksum_sha256
    )


def test_summary_export_is_deterministic_and_mechanical(tmp_path: Path, session: Session) -> None:
    policy = load_opportunity_discovery_policy(POLICY, repository_root=ROOT)
    _, configuration = _configuration(session)
    company, security = _identity(session, "AAA")
    snapshot = _snapshot(
        session,
        company=company,
        security=security,
        configuration=configuration,
        cutoff=CUTOFF - timedelta(days=1),
        score=Decimal("82"),
        token="export",
    )
    repository = OpportunityDiscoveryRepository(session)
    run, _ = repository.create_run(
        run_key_sha256="8" * 64,
        discovery_policy_code=policy.code,
        discovery_policy_checksum_sha256=policy.checksum_sha256,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256=policy.research_profile_checksum_sha256,
        financial_primitive_policy_checksum_sha256=(
            policy.financial_primitive_policy_checksum_sha256
        ),
        financial_endpoint_policy_checksum_sha256=(
            policy.financial_endpoint_policy_checksum_sha256
        ),
        model_family="fictional",
        discovery_cutoff=CUTOFF,
        universe_mode=policy.universe_mode,
        ordered_symbols=("AAA",),
        symbol_set_checksum_sha256="9" * 64,
        selected_snapshot_set_checksum_sha256="a" * 64,
        snapshot_selection_version=policy.snapshot_selection_version,
        ranking_version=policy.ranking_version,
        inputs_json={},
    )
    repository.add_item(
        run,
        OpportunityDiscoveryItemWrite(
            company_id=company.id,
            security_id=security.id,
            score_snapshot_id=snapshot.id,
            symbol="AAA",
            rankable=True,
            unranked_reason=None,
            score_rank=1,
            display_order=1,
            snapshot_age_days=1,
            freshness_state="fresh",
            selected_snapshot_fingerprint_sha256=snapshot.snapshot_fingerprint_sha256,
            detail_json={},
        ),
    )
    repository.complete(
        run,
        summary={"requested_securities": 1, "rankable_securities": 1, "unranked_securities": 0},
    )
    session.commit()
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    result = summarize_opportunity_discovery(
        session, run_id=run.id, policy=policy, export_csv=first
    )
    summarize_opportunity_discovery(session, run_id=run.id, policy=policy, export_csv=second)
    assert first.read_bytes() == second.read_bytes()
    assert result.items[0]["final_score"] == "82"
    contributions = cast(list[dict[str, object]], result.items[0]["component_contributions"])
    assert contributions[0]["final_contribution"] is not None
    assert "recommendation" not in first.read_text().lower()


def test_ranking_modules_are_isolated_from_backtest_outcomes() -> None:
    for relative in (
        "packages/data/inflector_data/opportunity_discovery.py",
        "packages/data/inflector_data/opportunity_summary.py",
        "packages/data/inflector_data/opportunity_cli.py",
        "packages/database/inflector_database/opportunity_repository.py",
    ):
        text_value = (ROOT / relative).read_text(encoding="utf-8")
        assert "backtest_outcomes" not in text_value
        assert "backtest_analysis" not in text_value
        assert "BacktestOutcome" not in text_value


def test_backtest_outcome_rows_cannot_change_current_snapshot_selection(
    session: Session,
) -> None:
    _, configuration = _configuration(session)
    company, security = _identity(session, "AAA")
    _snapshot(
        session,
        company=company,
        security=security,
        configuration=configuration,
        cutoff=CUTOFF - timedelta(days=1),
        score=Decimal("77"),
        token="isolated",
    )
    before = selected_snapshot_set_checksum(_select(session, ("AAA",), configuration))
    session.add(
        BacktestOutcome(
            backtest_observation_id=uuid4(),
            horizon_observations=252,
            outcome_status="available",
            entry_trading_date=date(2026, 1, 1),
            exit_trading_date=date(2026, 12, 31),
            entry_adjusted_close=Decimal("1"),
            exit_adjusted_close=Decimal("100"),
            security_return=Decimal("99"),
            benchmark_return=Decimal("0"),
            excess_return=Decimal("99"),
            outcome_data_cutoff=CUTOFF,
            provenance_json={"must_not_affect_discovery": True},
        )
    )
    session.flush()
    after = selected_snapshot_set_checksum(_select(session, ("AAA",), configuration))
    assert after == before


def test_dense_rank_does_not_consume_confidence_coverage_or_market_cap() -> None:
    first = DiscoveryDecision(
        "AAA", uuid4(), uuid4(), None, True, None, 1, "fresh"
    )
    second = DiscoveryDecision(
        "BBB", uuid4(), uuid4(), None, True, None, 1, "fresh"
    )
    # Ranking requires immutable snapshots and therefore fails closed rather than
    # inventing an alternate confidence/coverage/market-cap score.
    try:
        assign_dense_score_ranks((first, second))
    except ValueError as error:
        assert "persisted final_score" in str(error)
    else:
        raise AssertionError("rank assignment accepted missing persisted V5 scores")


def test_migration_0019_adds_only_discovery_tables(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert {"opportunity_discovery_runs", "opportunity_discovery_items"} <= tables
    assert {"backtest_runs", "backtest_observations", "backtest_outcomes"} <= tables
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            "20261003_0022"
        )
    engine.dispose()
    command.downgrade(config, "20261003_0018")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert not {"opportunity_discovery_runs", "opportunity_discovery_items"} & tables
    assert {"backtest_runs", "score_snapshots"} <= tables
    engine.dispose()
