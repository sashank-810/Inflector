"""Production L deterministic K-to-K research-state comparison contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from inflector_data.opportunity_change import (
    IncompatibleDiscoveryRunsError,
    build_opportunity_change,
    compare_discovery_items,
)
from inflector_data.opportunity_change_policy import (
    canonical_json_sha256,
    load_opportunity_change_policy,
)
from inflector_data.opportunity_change_summary import summarize_opportunity_change
from inflector_database.models import (
    Company,
    ModelVersion,
    OpportunityDiscoveryItem,
    OpportunityDiscoveryRun,
    ScoreComponent,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
)

ROOT = Path(__file__).parents[1]
POLICY = ROOT / "config/opportunity/production_opportunity_change_v1.json"
K_POLICY = ROOT / "config/opportunity/production_opportunity_discovery_v1.json"
BASELINE_CUTOFF = datetime(2026, 10, 1, 18, tzinfo=UTC)
CURRENT_CUTOFF = datetime(2026, 10, 3, 18, tzinfo=UTC)
K_CHECKSUM = "f90e463d3c8940cf9f93210e4d0806bdf4d535340a7d4140ba7d3e517d2e99d2"


def _sha(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _configuration(session: Session) -> ScoringConfiguration:
    model = ModelVersion(
        model_family="production-opportunity",
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


def _security(session: Session, symbol: str) -> Security:
    company = Company(
        legal_name=f"{symbol} Limited",
        display_name=f"{symbol} Limited",
        sector="Industrials",
        industry="Industrial Products",
    )
    session.add(company)
    session.flush()
    security = Security(
        company_id=company.id,
        isin=f"INE{symbol[:3].ljust(3, 'X')}000001",
        security_type="equity",
        status="active",
    )
    session.add(security)
    session.flush()
    return security


def _snapshot(
    session: Session,
    configuration: ScoringConfiguration,
    security: Security,
    *,
    token: str,
    cutoff: datetime,
    score: Decimal | None,
    eligible: bool = True,
    confidence: Decimal = Decimal("0.8"),
    coverage: Decimal = Decimal("1"),
    components: dict[str, tuple[Decimal, Decimal | None]] | None = None,
) -> ScoreSnapshot:
    values = components or {"financial_inflection": (Decimal("60"), Decimal("15"))}
    snapshot = ScoreSnapshot(
        company_id=security.company_id,
        model_version_id=configuration.model_version_id,
        scoring_configuration_id=configuration.id,
        configuration_checksum_sha256=configuration.checksum_sha256,
        as_of_date=cutoff.date(),
        knowledge_cutoff=cutoff,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        selected_provider_dataset_id=None,
        selected_filing_scope="consolidated",
        selected_security_id=security.id,
        snapshot_status="final_score_available" if score is not None else "partial_component_set",
        eligibility_eligible=eligible,
        eligibility_inputs_json={},
        eligibility_reasons_json=[],
        eligibility_warnings_json=[],
        financial_core_coverage=Decimal("1"),
        confidence=confidence,
        confidence_inputs_json={},
        confidence_details_json={},
        top_level_component_weight_coverage=coverage,
        available_component_codes_json=list(values),
        missing_component_codes_json=[],
        context_resolution_json={},
        input_manifest_json={},
        fingerprint_payload_json={"token": token},
        snapshot_fingerprint_sha256=_sha(token),
        final_score=score,
        algorithm_version="score_snapshot_v5",
    )
    session.add(snapshot)
    session.flush()
    for code, (component_score, contribution) in values.items():
        session.add(
            ScoreComponent(
                score_snapshot_id=snapshot.id,
                component_code=code,
                score=component_score,
                unit="score_0_100",
                configured_top_level_weight=Decimal("0.25"),
                subfactor_weight_coverage=Decimal("1"),
                final_contribution=contribution,
                available_at=cutoff,
                algorithm_version=f"{code}_v1",
                missing_subfactors_json=[],
                warnings_json=[],
                detail_json={},
            )
        )
    session.flush()
    session.refresh(snapshot)
    return snapshot


def _run(
    session: Session,
    configuration: ScoringConfiguration,
    *,
    token: str,
    cutoff: datetime,
    semantic_checksum: str | None = None,
) -> OpportunityDiscoveryRun:
    run = OpportunityDiscoveryRun(
        run_key_sha256=_sha(f"run:{token}"),
        discovery_policy_code="production_opportunity_discovery_v1",
        discovery_policy_checksum_sha256=semantic_checksum or K_CHECKSUM,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256="d" * 64,
        financial_primitive_policy_checksum_sha256="e" * 64,
        financial_endpoint_policy_checksum_sha256="f" * 64,
        model_family="production-opportunity",
        discovery_cutoff=cutoff,
        universe_mode="explicit_symbols_file",
        ordered_symbols_json=[],
        symbol_set_checksum_sha256=_sha(f"symbols:{token}"),
        selected_snapshot_set_checksum_sha256=_sha(f"snapshots:{token}"),
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


def _item(
    session: Session,
    run: OpportunityDiscoveryRun,
    security: Security | None,
    *,
    symbol: str,
    snapshot: ScoreSnapshot | None,
    rankable: bool,
    reason: str | None = None,
    rank: int | None = None,
    freshness: str = "fresh",
    display_order: int | None = None,
) -> OpportunityDiscoveryItem:
    item = OpportunityDiscoveryItem(
        opportunity_discovery_run_id=run.id,
        company_id=security.company_id if security else None,
        security_id=security.id if security else None,
        score_snapshot_id=snapshot.id if snapshot else None,
        symbol=symbol,
        rankable=rankable,
        unranked_reason=reason,
        score_rank=rank,
        display_order=display_order if display_order is not None else rank,
        snapshot_age_days=1 if snapshot else None,
        freshness_state=freshness,
        selected_snapshot_fingerprint_sha256=(
            snapshot.snapshot_fingerprint_sha256 if snapshot else None
        ),
        detail_json={},
    )
    session.add(item)
    session.flush()
    session.refresh(item)
    return item


def _policy():
    return load_opportunity_change_policy(POLICY, repository_root=ROOT)


def test_change_policy_is_versioned_checksum_bound_and_deterministic(tmp_path: Path) -> None:
    policy = _policy()
    assert policy.code == "production_opportunity_change_v1"
    assert policy.discovery_policy_checksum_sha256 == K_CHECKSUM
    assert canonical_json_sha256(json.loads(K_POLICY.read_text())) == K_CHECKSUM
    reordered = dict(reversed(tuple(json.loads(POLICY.read_text()).items())))
    copy = tmp_path / "policy.json"
    copy.write_text(json.dumps(reordered), encoding="utf-8")
    assert load_opportunity_change_policy(copy, repository_root=ROOT).checksum_sha256 == (
        policy.checksum_sha256
    )
    reordered["rank_change_semantics"] = "different"
    copy.write_text(json.dumps(reordered), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        load_opportunity_change_policy(copy, repository_root=ROOT)


def test_score_rank_component_and_confidence_changes_use_persisted_values(
    session: Session,
) -> None:
    config = _configuration(session)
    security = _security(session, "AAA")
    baseline_run = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current_run = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)
    before = _snapshot(
        session,
        config,
        security,
        token="before",
        cutoff=BASELINE_CUTOFF,
        score=Decimal("60"),
        confidence=Decimal("0.7"),
        coverage=Decimal("0.75"),
        components={
            "financial_inflection": (Decimal("40"), Decimal("10")),
            "business_quality": (Decimal("50"), Decimal("7.5")),
            "valuation": (Decimal("30"), Decimal("3")),
        },
    )
    after = _snapshot(
        session,
        config,
        security,
        token="after",
        cutoff=CURRENT_CUTOFF,
        score=Decimal("72"),
        confidence=Decimal("0.8"),
        coverage=Decimal("0.9"),
        components={
            "financial_inflection": (Decimal("55"), Decimal("13.75")),
            "business_quality": (Decimal("50"), Decimal("7.5")),
            "market_structure": (Decimal("60"), Decimal("3")),
        },
    )
    baseline_item = _item(
        session, baseline_run, security, symbol="AAA", snapshot=before, rankable=True, rank=4
    )
    current_item = _item(
        session, current_run, security, symbol="AAA", snapshot=after, rankable=True, rank=2
    )
    result = compare_discovery_items(_policy(), [baseline_item], [current_item])[0]
    assert result.score_delta == Decimal("12")
    assert result.rank_delta == 2
    assert result.confidence_delta == Decimal("0.1")
    assert result.components_gained_json == ["market_structure"]
    assert result.components_lost_json == ["valuation"]
    assert {
        "snapshot_changed",
        "remained_rankable",
        "score_increased",
        "rank_moved_up",
        "coverage_changed",
        "components_gained",
        "components_lost",
        "component_scores_changed",
        "component_contributions_changed",
        "confidence_changed",
    } <= set(result.change_codes_json)
    detail = result.component_change_detail_json["financial_inflection"]
    assert isinstance(detail, dict)
    assert detail["component_score_delta"] == "15"
    assert detail["component_contribution_delta"] == "3.75"


def test_score_and_rank_are_independent_and_display_order_is_ignored(session: Session) -> None:
    config = _configuration(session)
    security = _security(session, "AAA")
    baseline_run = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current_run = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)
    before = _snapshot(
        session, config, security, token="before", cutoff=BASELINE_CUTOFF, score=Decimal("80")
    )
    after = _snapshot(
        session, config, security, token="after", cutoff=CURRENT_CUTOFF, score=Decimal("80")
    )
    baseline_item = _item(
        session,
        baseline_run,
        security,
        symbol="AAA",
        snapshot=before,
        rankable=True,
        rank=4,
        display_order=9,
    )
    current_item = _item(
        session,
        current_run,
        security,
        symbol="AAA",
        snapshot=after,
        rankable=True,
        rank=2,
        display_order=1,
    )
    result = compare_discovery_items(_policy(), [baseline_item], [current_item])[0]
    assert result.score_delta == Decimal("0")
    assert result.rank_delta == 2
    assert "score_unchanged" in result.change_codes_json
    assert "rank_moved_up" in result.change_codes_json
    after.final_score = Decimal("55")
    result = compare_discovery_items(_policy(), [baseline_item], [current_item])[0]
    assert result.score_delta == Decimal("-25")
    assert "score_decreased" in result.change_codes_json
    current_item.score_rank = 4
    after.final_score = Decimal("85")
    result = compare_discovery_items(_policy(), [baseline_item], [current_item])[0]
    assert result.score_delta == Decimal("5")
    assert result.rank_delta == 0
    assert "score_increased" in result.change_codes_json
    assert "rank_unchanged" in result.change_codes_json


def test_partial_complete_stale_eligibility_and_reason_transitions(session: Session) -> None:
    config = _configuration(session)
    policy = _policy()
    baseline_run = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current_run = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)

    complete_security = _security(session, "AAA")
    partial = _snapshot(
        session, config, complete_security, token="partial", cutoff=BASELINE_CUTOFF, score=None
    )
    complete = _snapshot(
        session,
        config,
        complete_security,
        token="complete",
        cutoff=CURRENT_CUTOFF,
        score=Decimal("70"),
    )
    a_before = _item(
        session,
        baseline_run,
        complete_security,
        symbol="AAA",
        snapshot=partial,
        rankable=False,
        reason="partial_score",
    )
    a_after = _item(
        session,
        current_run,
        complete_security,
        symbol="AAA",
        snapshot=complete,
        rankable=True,
        rank=1,
    )
    result = compare_discovery_items(policy, [a_before], [a_after])[0]
    assert {"became_rankable", "coverage_completed"} <= set(result.change_codes_json)
    assert result.score_delta is None

    stale_security = _security(session, "BBB")
    fresh = _snapshot(
        session, config, stale_security, token="fresh", cutoff=BASELINE_CUTOFF, score=Decimal("70")
    )
    stale = _snapshot(
        session,
        config,
        stale_security,
        token="stale",
        cutoff=CURRENT_CUTOFF,
        score=Decimal("70"),
        eligible=False,
    )
    b_before = _item(
        session, baseline_run, stale_security, symbol="BBB", snapshot=fresh, rankable=True, rank=1
    )
    b_after = _item(
        session,
        current_run,
        stale_security,
        symbol="BBB",
        snapshot=stale,
        rankable=False,
        reason="stale_snapshot",
        freshness="stale",
    )
    result = compare_discovery_items(policy, [b_before], [b_after])[0]
    assert {"lost_rankability", "became_stale", "became_ineligible"} <= set(
        result.change_codes_json
    )
    assert result.score_delta is None

    reason_security = _security(session, "CCC")
    before = _item(
        session,
        baseline_run,
        reason_security,
        symbol="CCC",
        snapshot=None,
        rankable=False,
        reason="partial_score",
    )
    after = _item(
        session,
        current_run,
        reason_security,
        symbol="CCC",
        snapshot=None,
        rankable=False,
        reason="v5_ineligible",
    )
    result = compare_discovery_items(policy, [before], [after])[0]
    assert {"remained_unranked", "unranked_reason_changed"} <= set(
        result.change_codes_json
    )


def test_complete_to_partial_and_recovered_from_stale(session: Session) -> None:
    config = _configuration(session)
    security = _security(session, "AAA")
    baseline_run = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current_run = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)
    complete = _snapshot(
        session, config, security, token="complete", cutoff=BASELINE_CUTOFF, score=Decimal("70")
    )
    partial = _snapshot(
        session, config, security, token="partial", cutoff=CURRENT_CUTOFF, score=None
    )
    before = _item(
        session,
        baseline_run,
        security,
        symbol="AAA",
        snapshot=complete,
        rankable=True,
        rank=1,
        freshness="stale",
    )
    after = _item(
        session,
        current_run,
        security,
        symbol="AAA",
        snapshot=partial,
        rankable=False,
        reason="partial_score",
        freshness="fresh",
    )
    result = compare_discovery_items(_policy(), [before], [after])[0]
    assert {"lost_rankability", "coverage_regressed", "recovered_from_stale"} <= set(
        result.change_codes_json
    )
    assert result.score_delta is None


def test_coverage_transitions_use_snapshot_state_when_stale_reason_has_priority(
    session: Session,
) -> None:
    config = _configuration(session)
    policy = _policy()
    baseline_run = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current_run = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)

    completed_security = _security(session, "AAA")
    stale_partial = _snapshot(
        session,
        config,
        completed_security,
        token="stale-partial-before",
        cutoff=BASELINE_CUTOFF,
        score=None,
    )
    fresh_complete = _snapshot(
        session,
        config,
        completed_security,
        token="fresh-complete-after",
        cutoff=CURRENT_CUTOFF,
        score=Decimal("70"),
    )
    before = _item(
        session,
        baseline_run,
        completed_security,
        symbol="AAA",
        snapshot=stale_partial,
        rankable=False,
        reason="stale_snapshot",
        freshness="stale",
    )
    after = _item(
        session,
        current_run,
        completed_security,
        symbol="AAA",
        snapshot=fresh_complete,
        rankable=True,
        rank=1,
        freshness="fresh",
    )
    completed = compare_discovery_items(policy, [before], [after])[0]
    assert {"became_rankable", "coverage_completed", "recovered_from_stale"} <= set(
        completed.change_codes_json
    )
    assert completed.score_delta is None

    regressed_security = _security(session, "BBB")
    complete_before = _snapshot(
        session,
        config,
        regressed_security,
        token="complete-before",
        cutoff=BASELINE_CUTOFF,
        score=Decimal("75"),
    )
    stale_partial_after = _snapshot(
        session,
        config,
        regressed_security,
        token="stale-partial-after",
        cutoff=CURRENT_CUTOFF,
        score=None,
    )
    before = _item(
        session,
        baseline_run,
        regressed_security,
        symbol="BBB",
        snapshot=complete_before,
        rankable=True,
        rank=1,
        freshness="fresh",
    )
    after = _item(
        session,
        current_run,
        regressed_security,
        symbol="BBB",
        snapshot=stale_partial_after,
        rankable=False,
        reason="stale_snapshot",
        freshness="stale",
    )
    regressed = compare_discovery_items(policy, [before], [after])[0]
    assert {"lost_rankability", "coverage_regressed", "became_stale"} <= set(
        regressed.change_codes_json
    )
    assert regressed.score_delta is None


def test_identity_symbol_change_and_universe_membership_are_distinct(session: Session) -> None:
    config = _configuration(session)
    baseline_run = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current_run = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)
    renamed = _security(session, "OLD")
    snapshot = _snapshot(
        session, config, renamed, token="same", cutoff=BASELINE_CUTOFF, score=Decimal("80")
    )
    before = _item(
        session, baseline_run, renamed, symbol="OLD", snapshot=snapshot, rankable=True, rank=1
    )
    after = _item(
        session, current_run, renamed, symbol="NEW", snapshot=snapshot, rankable=True, rank=1
    )
    left = _item(
        session,
        baseline_run,
        None,
        symbol="LEFT",
        snapshot=None,
        rankable=False,
        reason="identity_unavailable",
    )
    entered = _item(
        session,
        current_run,
        None,
        symbol="ENTER",
        snapshot=None,
        rankable=False,
        reason="identity_unavailable",
    )
    resolved_security = _security(session, "RES")
    unresolved = _item(
        session,
        baseline_run,
        None,
        symbol="RES",
        snapshot=None,
        rankable=False,
        reason="identity_unavailable",
    )
    resolved = _item(
        session,
        current_run,
        resolved_security,
        symbol="RES",
        snapshot=None,
        rankable=False,
        reason="no_visible_snapshot",
    )
    results = compare_discovery_items(
        _policy(), [before, left, unresolved], [after, entered, resolved]
    )
    by_key = {item.identity_key: item for item in results}
    renamed_result = by_key[f"security:{renamed.id}"]
    assert "symbol_changed" in renamed_result.change_codes_json
    assert "snapshot_unchanged" in renamed_result.change_codes_json
    assert len([item for item in results if item.security_id == renamed.id]) == 1
    assert by_key["symbol:ENTER"].change_codes_json == ["entered_compared_universe"]
    assert by_key["symbol:LEFT"].change_codes_json == ["left_compared_universe"]
    assert by_key["symbol:RES"].identity_basis == "symbol"
    assert "unranked_reason_changed" in by_key["symbol:RES"].change_codes_json
    assert "entered_compared_universe" not in by_key["symbol:RES"].change_codes_json


def test_incompatible_or_non_temporal_runs_fail_before_items(session: Session) -> None:
    config = _configuration(session)
    baseline = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    incompatible = _run(
        session,
        config,
        token="c",
        cutoff=CURRENT_CUTOFF,
        semantic_checksum="9" * 64,
    )
    with pytest.raises(IncompatibleDiscoveryRunsError):
        build_opportunity_change(
            session,
            policy=_policy(),
            baseline_run_id=baseline.id,
            current_run_id=incompatible.id,
        )
    baseline.status = "planned"
    with pytest.raises(ValueError, match="completed"):
        build_opportunity_change(
            session,
            policy=_policy(),
            baseline_run_id=baseline.id,
            current_run_id=incompatible.id,
        )
    baseline.status = "completed"
    reversed_run = _run(
        session, config, token="reversed", cutoff=BASELINE_CUTOFF - timedelta(days=1)
    )
    with pytest.raises(ValueError, match="precede"):
        build_opportunity_change(
            session,
            policy=_policy(),
            baseline_run_id=baseline.id,
            current_run_id=reversed_run.id,
        )


def test_change_run_is_idempotent_frozen_and_export_filters_are_derivative(
    session: Session, tmp_path: Path
) -> None:
    config = _configuration(session)
    security = _security(session, "AAA")
    baseline = _run(session, config, token="b", cutoff=BASELINE_CUTOFF)
    current = _run(session, config, token="c", cutoff=CURRENT_CUTOFF)
    before_snapshot = _snapshot(
        session, config, security, token="before", cutoff=BASELINE_CUTOFF, score=Decimal("60")
    )
    after_snapshot = _snapshot(
        session, config, security, token="after", cutoff=CURRENT_CUTOFF, score=Decimal("72")
    )
    _item(
        session,
        baseline,
        security,
        symbol="AAA",
        snapshot=before_snapshot,
        rankable=True,
        rank=1,
    )
    _item(
        session,
        current,
        security,
        symbol="AAA",
        snapshot=after_snapshot,
        rankable=True,
        rank=1,
    )
    policy = _policy()
    first = build_opportunity_change(
        session, policy=policy, baseline_run_id=baseline.id, current_run_id=current.id
    )
    repeated = build_opportunity_change(
        session, policy=policy, baseline_run_id=baseline.id, current_run_id=current.id
    )
    assert repeated.run_id == first.run_id
    assert repeated.run_key_sha256 == first.run_key_sha256
    assert repeated.already_completed
    changed_policy = replace(policy, checksum_sha256="9" * 64)
    policy_changed = build_opportunity_change(
        session,
        policy=changed_policy,
        baseline_run_id=baseline.id,
        current_run_id=current.id,
    )
    assert policy_changed.run_key_sha256 != first.run_key_sha256
    export = tmp_path / "changes.csv"
    summary = summarize_opportunity_change(
        session,
        run_id=first.run_id,
        policy=policy,
        changed_only=True,
        change_code="score_increased",
        limit=1,
        export_csv=export,
    )
    assert len(summary.items) == 1
    codes = summary.items[0]["change_codes"]
    assert isinstance(codes, list)
    assert "score_increased" in codes
    assert export.read_text(encoding="utf-8").count("\n") == 2
    later = _run(
        session, config, token="future-k", cutoff=CURRENT_CUTOFF + timedelta(days=1)
    )
    assert later.id not in (baseline.id, current.id)
    frozen = summarize_opportunity_change(session, run_id=first.run_id, policy=policy)
    assert frozen.items == summary.items


def test_l_has_no_research_scoring_or_backtest_dependency() -> None:
    files = (
        ROOT / "packages/data/inflector_data/opportunity_change.py",
        ROOT / "packages/data/inflector_data/opportunity_change_summary.py",
        ROOT / "packages/database/inflector_database/opportunity_change_repository.py",
    )
    forbidden = (
        "production_research",
        "research_cli",
        "financial_endpoint",
        "score_orchestration",
        "backtest_outcomes",
        "backtest_analysis",
        "BacktestOutcome",
        "inflector_core",
    )
    import_text = "\n".join(
        line
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(("from ", "import "))
    )
    assert not any(value in import_text for value in forbidden)


def test_migration_0020_adds_only_change_tables(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert {"opportunity_change_runs", "opportunity_change_items"} <= tables
    assert {"opportunity_discovery_runs", "backtest_runs", "score_snapshots"} <= tables
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            "20261003_0021"
        )
    engine.dispose()
    command.downgrade(config, "20261003_0019")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert not {"opportunity_change_runs", "opportunity_change_items"} & tables
    assert {"opportunity_discovery_runs", "backtest_runs", "score_snapshots"} <= tables
    engine.dispose()
