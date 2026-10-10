"""Production R frozen-prediction and factual-label evaluation contracts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import Session

import inflector_data.multibagger_evaluation as evaluation_module
from inflector_core.score_audit import canonical_audit_value
from inflector_data.historical_evaluation_policy import load_multibagger_evaluation_policy
from inflector_data.multibagger_evaluation import (
    EvaluationCohortBundle,
    EvaluationIntegrityError,
    _cohort_metrics,
    _confusion_class,
    _f1,
    _ratio,
    _selection_count,
    build_multibagger_evaluation,
    load_evaluation_manifest,
)
from inflector_database.backtest_repository import BacktestObservationWrite, BacktestRepository
from inflector_database.historical_evaluation_repository import (
    HistoricalEvaluationRepository,
    HistoricalUniverseMemberWrite,
    MultibaggerLabelWrite,
)
from inflector_database.models import (
    BacktestObservation,
    BacktestRun,
    Company,
    DataProvider,
    ExchangeListing,
    ModelVersion,
    MultibaggerEvaluationRun,
    MultibaggerLabelRun,
    MultibaggerOutcomeLabel,
    ProviderDataset,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
)
from inflector_database.score_repository import (
    V5_COMPONENT_ALGORITHM_VERSIONS,
    V5_COMPONENT_ORDER,
    V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
    ScoreComponentWrite,
    ScoreSnapshotRepository,
    ScoreSnapshotWrite,
)

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config/backtest/production_multibagger_evaluation_v1.json"
WHEN = datetime(2020, 1, 31, 23, 59, tzinfo=UTC)
OUTCOME_CUTOFF = datetime(2026, 1, 31, tzinfo=UTC)


def _identity(session: Session, symbol: str) -> tuple[Company, Security, ExchangeListing]:
    company = Company(
        legal_name=f"{symbol} Legal {uuid4()}",
        display_name=symbol,
        sector="Fixtures",
        industry="Fixtures",
        created_at=WHEN,
        updated_at=WHEN,
    )
    session.add(company)
    session.flush()
    security = Security(company_id=company.id, isin=f"INE{str(uuid4().int)[:9]}")
    session.add(security)
    session.flush()
    listing = ExchangeListing(
        security_id=security.id,
        exchange="NSE",
        symbol=symbol,
        valid_from=date(2010, 1, 1),
        valid_to=None,
        status="active",
        created_at=WHEN,
        updated_at=WHEN,
    )
    session.add(listing)
    session.flush()
    return company, security, listing


def _foundation(session: Session) -> tuple[ProviderDataset, ScoringConfiguration, ModelVersion]:
    provider = DataProvider(
        code=f"fixture-{uuid4()}", provider_type="fixture", licence_name="fixture", enabled=True
    )
    session.add(provider)
    session.flush()
    dataset = ProviderDataset(
        provider_id=provider.id,
        code=f"fixture-{uuid4()}",
        licence_class="fixture",
        redistributable=False,
    )
    model = ModelVersion(
        model_family="opportunity_score_v5", semantic_version="5", git_sha="a" * 40, status="active"
    )
    session.add_all((dataset, model))
    session.flush()
    configuration = ScoringConfiguration(
        model_version_id=model.id,
        configuration_name="fixture",
        configuration_version="1",
        status="active",
        effective_from=WHEN,
        effective_to=None,
        configuration_json={},
        checksum_sha256="b" * 64,
    )
    session.add(configuration)
    session.flush()
    return dataset, configuration, model


def _snapshot(
    session: Session,
    *,
    company: Company,
    security: Security,
    configuration: ScoringConfiguration,
    model: ModelVersion,
    score: Decimal | None,
    status: str = "final_score_available",
    eligible: bool = True,
) -> ScoreSnapshot:
    assert score is not None and status == "final_score_available" and eligible
    weight = Decimal("0.125")
    components = tuple(
        ScoreComponentWrite(
            component_code=code,
            score=score,
            unit="score_0_100",
            configured_top_level_weight=weight,
            subfactor_weight_coverage=Decimal("1"),
            final_contribution=score * weight,
            available_at=WHEN,
            algorithm_version=V5_COMPONENT_ALGORITHM_VERSIONS[code],
            missing_subfactors_json=[],
            warnings_json=[],
            detail_json={},
            explanations=(),
        )
        for code in V5_COMPONENT_ORDER
    )
    component_state = canonical_audit_value(
        [
            {
                "component_code": item.component_code,
                "score": item.score,
                "configured_top_level_weight": item.configured_top_level_weight,
                "final_contribution": item.final_contribution,
            }
            for item in components
        ]
    )
    payload = {
        "algorithm_version": "score_snapshot_v5",
        "fixture_security_id": str(security.id),
        "opportunity_score_aggregation_version": V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
        "final_score_state": {
            "aggregation_version": V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
            "required_positive_weight_component_codes": list(V5_COMPONENT_ORDER),
            "components": component_state,
            "final_score": canonical_audit_value(score),
        },
    }
    snapshot = (
        ScoreSnapshotRepository(session)
        .persist_snapshot(
            ScoreSnapshotWrite(
                company_id=company.id,
                model_version_id=model.id,
                scoring_configuration_id=configuration.id,
                configuration_checksum_sha256=configuration.checksum_sha256,
                as_of_date=WHEN.date(),
                knowledge_cutoff=WHEN,
                ending_fiscal_year=2020,
                ending_fiscal_quarter=3,
                selected_provider_dataset_id=None,
                selected_filing_scope=None,
                selected_security_id=security.id,
                snapshot_status=status,
                eligibility_eligible=eligible,
                eligibility_inputs_json={},
                eligibility_reasons_json=[],
                eligibility_warnings_json=[],
                financial_core_coverage=Decimal("1"),
                confidence=Decimal("1"),
                confidence_inputs_json={},
                confidence_details_json={},
                top_level_component_weight_coverage=Decimal("1"),
                available_component_codes_json=list(V5_COMPONENT_ORDER),
                missing_component_codes_json=[],
                context_resolution_json={},
                input_manifest_json={},
                fingerprint_payload_json=payload,
                final_score=score,
                algorithm_version="score_snapshot_v5",
                components=components,
            )
        )
        .record
    )
    session.flush()
    return snapshot


def _run(
    session: Session,
    *,
    configuration: ScoringConfiguration,
    members: list[tuple[Company, Security, ExchangeListing]],
    scores: list[Decimal | None],
) -> tuple[UUID, list[UUID], list[ScoreSnapshot | None]]:
    repository = BacktestRepository(session)
    run, _ = repository.create_run(
        run_key_sha256=uuid4().hex + uuid4().hex,
        backtest_policy_code="production_backtest_v1",
        backtest_policy_checksum_sha256="e1f1f886f686045f7f2774be355674a726b155db1a32c9080999c32becd35984",
        availability_manifest_code="fixture",
        availability_manifest_checksum_sha256="c" * 64,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256="f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1",
        financial_primitive_policy_checksum_sha256="d" * 64,
        financial_endpoint_policy_checksum_sha256="e" * 64,
        source_state_checksum_sha256="f" * 64,
        model_family="opportunity_score_v5",
        cutoff_start=WHEN,
        cutoff_end=WHEN,
        cutoff_cadence="calendar_month_end_utc",
        universe_policy="observed_pit_universe",
        benchmark_code="NIFTY 50",
        return_horizons=(21, 63, 126, 252),
        ordered_symbols=tuple(item[2].symbol for item in members),
        symbol_set_checksum_sha256="a" * 64,
        inputs_json={},
    )
    model = session.get(ModelVersion, configuration.model_version_id)
    assert model is not None
    observations: list[UUID] = []
    snapshots: list[ScoreSnapshot | None] = []
    for (company, security, listing), score in zip(members, scores, strict=True):
        snapshot = (
            _snapshot(
                session,
                company=company,
                security=security,
                configuration=configuration,
                model=model,
                score=score,
            )
            if score is not None
            else None
        )
        observation, _ = repository.add_observation(
            run,
            BacktestObservationWrite(
                company_id=company.id,
                security_id=security.id,
                score_snapshot_id=snapshot.id if snapshot else None,
                symbol=listing.symbol,
                knowledge_cutoff=WHEN,
                observation_status="snapshot_frozen" if snapshot else "issuer_unavailable",
                selected_fiscal_year=None,
                selected_fiscal_quarter=None,
                selected_filing_scope=None,
                selected_period_end=None,
                snapshot_status=snapshot.snapshot_status if snapshot else None,
                snapshot_fingerprint_sha256=(
                    snapshot.snapshot_fingerprint_sha256 if snapshot else None
                ),
                research_state_projection_version="fixture",
                research_state_sha256="1" * 64,
                research_state_changed=False,
                detail_json={},
            ),
        )
        observations.append(observation.id)
        snapshots.append(snapshot)
    repository.set_status(run, "snapshots_built")
    repository.set_status(run, "outcomes_built")
    repository.set_status(run, "completed", completed_at=OUTCOME_CUTOFF)
    session.flush()
    return run.id, observations, snapshots


def _universe(
    session: Session,
    *,
    dataset: ProviderDataset,
    members: list[tuple[Company, Security, ExchangeListing]],
) -> UUID:
    repository = HistoricalEvaluationRepository(session)
    run, _ = repository.create_universe_run(
        run_key_sha256=uuid4().hex + uuid4().hex,
        universe_policy_code="production_historical_universe_v1",
        universe_policy_checksum_sha256="ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982",
        cutoff=WHEN,
        source_provider_dataset_id=dataset.id,
        source_state_checksum_sha256="9" * 64,
        inputs_json={},
    )
    for company, security, listing in members:
        repository.add_universe_member(
            run,
            HistoricalUniverseMemberWrite(
                historical_symbol=listing.symbol,
                historical_isin=security.isin,
                exchange="NSE",
                series="EQ",
                membership_date=WHEN.date(),
                company_id=company.id,
                security_id=security.id,
                exchange_listing_id=listing.id,
                membership_status="eligible",
                reason_code=None,
                source_record_id=uuid4(),
                member_fingerprint_sha256=uuid4().hex + uuid4().hex,
                provenance_json={},
            ),
        )
    repository.complete_universe_run(
        run, member_count=len(members), summary_json={}, completed_at=OUTCOME_CUTOFF
    )
    return run.id


def _labels(
    session: Session,
    *,
    backtest_run_id: UUID,
    observations: list[UUID],
    classifications: list[str],
    dataset: ProviderDataset,
    outcome_cutoff: datetime = OUTCOME_CUTOFF,
) -> UUID:
    repository = HistoricalEvaluationRepository(session)
    backtest = session.get(BacktestRun, backtest_run_id)
    assert backtest is not None
    run, _ = repository.create_label_run(
        run_key_sha256=uuid4().hex + uuid4().hex,
        label_policy_code="production_multibagger_outcomes_v1",
        label_policy_checksum_sha256="3c901049e37dfa1764d70feb7e8fdfa8762b3c4733ea283f8b6d99c73475332f",
        source_backtest_run_id=backtest_run_id,
        source_backtest_run_key_sha256=backtest.run_key_sha256,
        outcome_data_cutoff=outcome_cutoff,
        market_provider_dataset_id=dataset.id,
        corporate_action_provider_dataset_id=dataset.id,
        source_state_checksum_sha256="3" * 64,
        ordered_contracts_json=[
            {"code": "MB_2X_2Y", "threshold_multiple": "2", "horizon_calendar_years": 2},
            {"code": "MB_3X_3Y", "threshold_multiple": "3", "horizon_calendar_years": 3},
            {"code": "MB_5X_5Y", "threshold_multiple": "5", "horizon_calendar_years": 5},
        ],
        algorithm_versions_json={},
        inputs_json={},
    )
    for observation_id, classification in zip(observations, classifications, strict=True):
        for code, threshold, years in (
            ("MB_2X_2Y", "2", 2),
            ("MB_3X_3Y", "3", 3),
            ("MB_5X_5Y", "5", 5),
        ):
            unavailable = "fixture_unavailable" if classification == "unavailable" else None
            repository.add_label(
                run,
                MultibaggerLabelWrite(
                    backtest_observation_id=observation_id,
                    contract_code=code,
                    classification=classification,
                    unavailable_reason=unavailable,
                    threshold_multiple=Decimal(threshold),
                    horizon_calendar_years=years,
                    entry_trading_date=date(2020, 2, 1),
                    adjusted_entry_close=Decimal("100"),
                    horizon_end_date=date(2022 + years - 2, 2, 1),
                    first_threshold_hit_trading_date=(
                        date(2021, 1, 1) if classification == "positive" else None
                    ),
                    trading_observations_available=10,
                    peak_adjusted_close=Decimal("200"),
                    peak_price_multiple=Decimal("2"),
                    maximum_forward_price_return=Decimal("1"),
                    endpoint_adjusted_close=Decimal("150"),
                    endpoint_return=Decimal("0.5"),
                    calendar_days_to_threshold=(365 if classification == "positive" else None),
                    trading_observations_to_threshold=(5 if classification == "positive" else None),
                    maximum_drawdown=Decimal("-0.2"),
                    listing_valid_to=None,
                    label_matured_at=(
                        outcome_cutoff if classification in {"positive", "negative"} else None
                    ),
                    outcome_data_cutoff=outcome_cutoff,
                    provenance_json={"fixture": True},
                    label_fingerprint_sha256=uuid4().hex + uuid4().hex,
                ),
            )
    repository.complete_label_run(run, summary_json={}, completed_at=outcome_cutoff)
    return run.id


def _complete_bundle(session: Session) -> tuple[EvaluationCohortBundle, UUID]:
    dataset, configuration, _ = _foundation(session)
    members = [_identity(session, "ALPHA"), _identity(session, "BETA"), _identity(session, "GAMMA")]
    universe = _universe(session, dataset=dataset, members=members)
    backtest, observations, _ = _run(
        session,
        configuration=configuration,
        members=members,
        scores=[Decimal("90"), Decimal("80"), None],
    )
    labels = _labels(
        session,
        backtest_run_id=backtest,
        observations=observations,
        classifications=["positive", "negative", "positive"],
        dataset=dataset,
    )
    return EvaluationCohortBundle(universe, (backtest,), (labels,), "fixture_verified"), backtest


def test_evaluation_freezes_predictions_before_label_join_and_reports_two_views(
    session: Session,
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    result = build_multibagger_evaluation(
        session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
    )
    assert result.prediction_count == 3
    assert result.confusion_count == 3 * 3 * 6 * 2
    metrics = cast(dict[str, dict[str, object]], result.summary["metrics"])
    end_to_end = metrics["MB_2X_2Y|TOP_5|end_to_end"]
    rankable_only = metrics["MB_2X_2Y|TOP_5|rankable_only"]
    assert end_to_end["tp"] == 1
    assert end_to_end["fp"] == 1
    assert end_to_end["fn"] == 1
    assert end_to_end["selected_count"] == 2
    assert end_to_end["selected_mature_count"] == 2
    assert end_to_end["mature_ground_truth_count"] == 3
    cohort_weighted = cast(dict[str, object], end_to_end["cohort_weighted"])
    assert cohort_weighted["macro_precision_contributing_cohort_count"] == 1
    diagnostics = cast(dict[str, object], result.summary["grouped_diagnostics"])
    outcome_diagnostics = cast(dict[str, object], diagnostics["MB_2X_2Y"])
    selection_diagnostics = cast(dict[str, object], outcome_diagnostics["TOP_5"])
    view_diagnostics = cast(dict[str, object], selection_diagnostics["end_to_end"])
    assert "calendar_year" in view_diagnostics
    assert rankable_only["excluded_rankability_count"] == 1
    assert result.created_run is True
    rerun = build_multibagger_evaluation(
        session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
    )
    assert rerun.created_run is False
    assert rerun.run_id == result.run_id


def test_label_rows_are_not_loaded_before_prediction_selection_freeze(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    session.expire_all()
    outcome_queries: list[str] = []
    phase_sql: list[str] = []

    def observe_sql(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        if (
            "multibagger_prediction_rows" in normalized
            or "multibagger_outcome_labels" in normalized
        ):
            phase_sql.append(normalized)
        if "multibagger_outcome_labels" in normalized:
            outcome_queries.append(statement)

    original_rank = evaluation_module._rank_predictions

    def rank_after_metadata(
        rank_session: Session,
        *,
        observations: dict[UUID, BacktestObservation],
        backtests: tuple[BacktestRun, ...],
    ) -> tuple[evaluation_module.PredictionState, ...]:
        assert outcome_queries == []
        return original_rank(
            rank_session,
            observations=observations,
            backtests=backtests,
        )

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", observe_sql)
    monkeypatch.setattr(evaluation_module, "_rank_predictions", rank_after_metadata)
    try:
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
    assert outcome_queries
    prediction_insert = next(
        index
        for index, statement in enumerate(phase_sql)
        if statement.lstrip().startswith("insert into multibagger_prediction_rows")
    )
    first_label_query = next(
        index
        for index, statement in enumerate(phase_sql)
        if "multibagger_outcome_labels" in statement
    )
    assert prediction_insert < first_label_query


@pytest.mark.parametrize(
    "corruption",
    (
        "no_snapshot_with_fingerprint",
        "no_snapshot_with_status",
        "no_snapshot_marked_frozen",
        "snapshot_marked_issuer_unavailable",
        "snapshot_marked_research_failed",
    ),
)
def test_backtest_observation_state_contradictions_fail_closed(
    session: Session, corruption: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    no_snapshot = session.scalar(
        select(BacktestObservation).where(BacktestObservation.score_snapshot_id.is_(None))
    )
    with_snapshot = session.scalar(
        select(BacktestObservation).where(BacktestObservation.score_snapshot_id.is_not(None))
    )
    assert no_snapshot is not None and with_snapshot is not None
    if corruption == "no_snapshot_with_fingerprint":
        no_snapshot.snapshot_fingerprint_sha256 = "f" * 64
    elif corruption == "no_snapshot_with_status":
        no_snapshot.snapshot_status = "partial_component_set"
    elif corruption == "no_snapshot_marked_frozen":
        no_snapshot.observation_status = "snapshot_frozen"
    elif corruption == "snapshot_marked_issuer_unavailable":
        with_snapshot.observation_status = "issuer_unavailable"
    else:
        with_snapshot.observation_status = "research_failed"
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match="contradictory persisted state"):
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )


@pytest.mark.parametrize("legitimate_status", ("issuer_unavailable", "research_failed"))
def test_legitimate_no_snapshot_observations_remain_unrankable(
    session: Session, legitimate_status: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    no_snapshot = session.scalar(
        select(BacktestObservation).where(BacktestObservation.score_snapshot_id.is_(None))
    )
    assert no_snapshot is not None
    no_snapshot.observation_status = legitimate_status
    result = build_multibagger_evaluation(
        session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
    )
    assert result.prediction_count == 3


def test_incomplete_universe_shards_fail_closed(session: Session) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    from inflector_database.models import BacktestObservation

    # Fixture corruption models an omitted shard member; it must not become unrankable.
    observation = session.scalar(select(BacktestObservation))
    assert observation is not None
    session.delete(observation)
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match="backtest shards"):
        build_multibagger_evaluation(
            session,
            policy=policy,
            bundles=(bundle,),
            completed_at=OUTCOME_CUTOFF,
        )


def test_selection_and_confusion_contracts_are_exact_decimal_and_not_outcome_tuned() -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    percentage = next(item for item in policy.selection_contracts if item.code == "TOP_1_PERCENT")
    assert _selection_count(percentage, 26) == 1
    assert _selection_count(percentage, 0) == 0
    assert (
        _confusion_class(rankable=False, selected=False, actual_label="positive", view="end_to_end")
        == "fn"
    )
    assert (
        _confusion_class(
            rankable=False, selected=False, actual_label="negative", view="rankable_only"
        )
        == "excluded_rankability_for_rankable_only"
    )


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        ("TOP_5", 5),
        ("TOP_10", 10),
        ("TOP_20", 20),
        ("TOP_1_PERCENT", 1),
        ("TOP_5_PERCENT", 2),
        ("TOP_10_PERCENT", 3),
    ),
)
def test_all_six_fixed_selection_contracts(code: str, expected: int) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    contract = next(item for item in policy.selection_contracts if item.code == code)
    assert _selection_count(contract, 26) == expected


@pytest.mark.parametrize(
    ("selected", "actual", "expected"),
    (
        (True, "positive", "tp"),
        (True, "negative", "fp"),
        (False, "positive", "fn"),
        (False, "negative", "tn"),
        (False, "unmatured", "excluded_unmatured"),
        (False, "unavailable", "excluded_unavailable"),
    ),
)
def test_confusion_contract_matrix(selected: bool, actual: str, expected: str) -> None:
    assert (
        _confusion_class(
            rankable=True,
            selected=selected,
            actual_label=actual,
            view="end_to_end",
        )
        == expected
    )


def test_decimal_metric_formulas_and_zero_denominators() -> None:
    assert _ratio(1, 2) == Decimal("0.5")
    assert _f1(1, 2, 2) == Decimal("0.5")
    assert _ratio(0, 0) is None
    assert _f1(0, 0, 0) is None


def test_label_missing_or_extra_is_not_a_prediction_failure(session: Session) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    # Delete is only a fixture corruption to prove the builder fails rather than imputing TN/FN.
    from inflector_database.models import MultibaggerOutcomeLabel

    victim = session.scalar(select(MultibaggerOutcomeLabel))
    assert victim is not None
    session.delete(victim)
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match="exactly one label"):
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )


@pytest.mark.parametrize("classification", ("unmatured", "unavailable"))
def test_rankable_only_counts_exclude_unrankable_nonmature_rows(
    session: Session, classification: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    no_snapshot = session.scalar(
        select(BacktestObservation).where(BacktestObservation.score_snapshot_id.is_(None))
    )
    assert no_snapshot is not None
    for label in session.scalars(
        select(MultibaggerOutcomeLabel).where(
            MultibaggerOutcomeLabel.backtest_observation_id == no_snapshot.id
        )
    ):
        label.classification = classification
        label.label_matured_at = None
        label.unavailable_reason = (
            "incomplete_outcome_window" if classification == "unavailable" else None
        )
    session.flush()
    result = build_multibagger_evaluation(
        session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
    )
    metrics = cast(dict[str, dict[str, object]], result.summary["metrics"])
    end_to_end = metrics["MB_2X_2Y|TOP_5|end_to_end"]
    rankable_only = metrics["MB_2X_2Y|TOP_5|rankable_only"]
    assert (
        cast(int, end_to_end["mature_ground_truth_count"])
        + cast(int, end_to_end["unmatured_count"])
        + cast(int, end_to_end["unavailable_count"])
        == 3
    )
    assert rankable_only["total_rows_in_view"] == 2
    assert rankable_only["mature_ground_truth_count"] == 2
    assert rankable_only["unmatured_count"] == 0
    assert rankable_only["unavailable_count"] == 0
    assert rankable_only["excluded_rankability_count"] == 1


@pytest.mark.parametrize("excluded_class", ("excluded_unmatured", "excluded_unavailable"))
def test_cohort_hit_rate_uses_only_evaluable_cohorts(excluded_class: str) -> None:
    first_id = uuid4()
    second_id = uuid4()
    first = SimpleNamespace(
        prediction=SimpleNamespace(cohort=SimpleNamespace(id=first_id)),
        confusion_class="tp",
    )
    excluded = SimpleNamespace(
        prediction=SimpleNamespace(cohort=SimpleNamespace(id=second_id)),
        confusion_class=excluded_class,
    )
    metrics = _cohort_metrics(all_items=[first, excluded], view_items=[first, excluded])
    assert metrics["cohort_count_total"] == 2
    assert metrics["evaluable_cohort_count"] == 1
    assert metrics["cohorts_with_hit"] == 1
    assert metrics["cohort_hit_rate"] == "1"


def test_cohort_hit_rate_handles_rankability_exclusion_and_zero_evaluable() -> None:
    cohort_id = uuid4()
    excluded = SimpleNamespace(
        prediction=SimpleNamespace(cohort=SimpleNamespace(id=cohort_id)),
        confusion_class="excluded_rankability_for_rankable_only",
    )
    metrics = _cohort_metrics(all_items=[excluded], view_items=[])
    assert metrics["cohort_count_total"] == 1
    assert metrics["evaluable_cohort_count"] == 0
    assert metrics["cohorts_with_hit"] == 0
    assert metrics["cohort_hit_rate"] is None


def test_multiple_j_shards_exactly_recompose_one_historical_cohort(session: Session) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    dataset, configuration, _ = _foundation(session)
    members = [_identity(session, symbol) for symbol in ("ALPHA", "BETA", "GAMMA")]
    universe = _universe(session, dataset=dataset, members=members)
    first_run, first_observations, _ = _run(
        session,
        configuration=configuration,
        members=members[:2],
        scores=[Decimal("90"), Decimal("80")],
    )
    second_run, second_observations, _ = _run(
        session,
        configuration=configuration,
        members=members[2:],
        scores=[Decimal("70")],
    )
    first_labels = _labels(
        session,
        backtest_run_id=first_run,
        observations=first_observations,
        classifications=["positive", "negative"],
        dataset=dataset,
    )
    second_labels = _labels(
        session,
        backtest_run_id=second_run,
        observations=second_observations,
        classifications=["positive"],
        dataset=dataset,
    )
    result = build_multibagger_evaluation(
        session,
        policy=policy,
        bundles=(
            EvaluationCohortBundle(
                universe,
                (first_run, second_run),
                (first_labels, second_labels),
                "fixture_verified",
            ),
        ),
        completed_at=OUTCOME_CUTOFF,
    )
    assert result.prediction_count == 3
    reordered = build_multibagger_evaluation(
        session,
        policy=policy,
        bundles=(
            EvaluationCohortBundle(
                universe,
                (second_run, first_run),
                (second_labels, first_labels),
                "fixture_verified",
            ),
        ),
        completed_at=OUTCOME_CUTOFF,
    )
    assert reordered.run_id == result.run_id
    assert reordered.created_run is False


@pytest.mark.parametrize(
    ("corruption", "message"),
    (
        ("scoring_configuration", "mix frozen model semantics"),
        ("model_family", "mix frozen model semantics"),
        ("j_policy", "backtest policy binding mismatch"),
        ("cohort_cutoff", "cannot contain the cohort cutoff"),
    ),
)
def test_mixed_backtest_shard_metadata_fails_closed(
    session: Session, corruption: str, message: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    dataset, configuration, _ = _foundation(session)
    members = [_identity(session, symbol) for symbol in ("ALPHA", "BETA")]
    universe = _universe(session, dataset=dataset, members=members)
    first_run, first_observations, _ = _run(
        session,
        configuration=configuration,
        members=members[:1],
        scores=[Decimal("90")],
    )
    second_run, second_observations, _ = _run(
        session,
        configuration=configuration,
        members=members[1:],
        scores=[Decimal("80")],
    )
    first_labels = _labels(
        session,
        backtest_run_id=first_run,
        observations=first_observations,
        classifications=["positive"],
        dataset=dataset,
    )
    second_labels = _labels(
        session,
        backtest_run_id=second_run,
        observations=second_observations,
        classifications=["negative"],
        dataset=dataset,
    )
    corrupt_run = session.get(BacktestRun, second_run)
    assert corrupt_run is not None
    if corruption == "scoring_configuration":
        corrupt_run.scoring_configuration_checksum_sha256 = "0" * 64
    elif corruption == "model_family":
        corrupt_run.model_family = "other_model_family"
    elif corruption == "j_policy":
        corrupt_run.backtest_policy_checksum_sha256 = "0" * 64
    else:
        corrupt_run.cutoff_start = WHEN + timedelta(days=1)
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match=message):
        build_multibagger_evaluation(
            session,
            policy=policy,
            bundles=(
                EvaluationCohortBundle(
                    universe,
                    (first_run, second_run),
                    (first_labels, second_labels),
                    "fixture_verified",
                ),
            ),
            completed_at=OUTCOME_CUTOFF,
        )


@pytest.mark.parametrize(
    ("corruption", "message"),
    (
        ("duplicate", "duplicate security observation"),
        ("extra", "non-universe security"),
    ),
)
def test_duplicate_or_extra_backtest_observations_fail_exact_recomposition(
    session: Session, corruption: str, message: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    dataset, configuration, _ = _foundation(session)
    cohort_members = [_identity(session, symbol) for symbol in ("ALPHA", "BETA")]
    universe = _universe(session, dataset=dataset, members=cohort_members)
    first_members = cohort_members if corruption == "extra" else cohort_members[:1]
    second_members = (
        [_identity(session, "FOREIGN")]
        if corruption == "extra"
        else [cohort_members[0], cohort_members[1]]
    )
    first_run, first_observations, _ = _run(
        session,
        configuration=configuration,
        members=first_members,
        scores=[Decimal("90")] * len(first_members),
    )
    second_run, second_observations, _ = _run(
        session,
        configuration=configuration,
        members=second_members,
        scores=[Decimal("80")] * len(second_members),
    )
    first_labels = _labels(
        session,
        backtest_run_id=first_run,
        observations=first_observations,
        classifications=["positive"] * len(first_observations),
        dataset=dataset,
    )
    second_labels = _labels(
        session,
        backtest_run_id=second_run,
        observations=second_observations,
        classifications=["negative"] * len(second_observations),
        dataset=dataset,
    )
    with pytest.raises(EvaluationIntegrityError, match=message):
        build_multibagger_evaluation(
            session,
            policy=policy,
            bundles=(
                EvaluationCohortBundle(
                    universe,
                    (first_run, second_run),
                    (first_labels, second_labels),
                    "fixture_verified",
                ),
            ),
            completed_at=OUTCOME_CUTOFF,
        )


def test_migration_0024_adds_only_evaluation_tables_and_downgrades(tmp_path: Path) -> None:
    database = tmp_path / "r-migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "20261004_0023")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "20261005_0024")
    after = set(inspect(engine).get_table_names())
    assert after - before == {
        "multibagger_evaluation_runs",
        "multibagger_evaluation_cohorts",
        "multibagger_prediction_rows",
        "multibagger_confusion_rows",
    }
    command.downgrade(config, "20261004_0023")
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()


def test_production_research_and_delivery_modules_do_not_depend_on_r_outcomes() -> None:
    protected = (
        "packages/data/inflector_data/production_research.py",
        "packages/data/inflector_data/score_orchestration.py",
        "packages/data/inflector_data/historical_dataset.py",
        "packages/data/inflector_data/opportunity_discovery.py",
        "packages/data/inflector_data/opportunity_change.py",
        "packages/data/inflector_data/research_notifications.py",
        "packages/data/inflector_data/research_notification_delivery.py",
    )
    for relative in protected:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "multibagger_evaluation" not in source
        assert "MultibaggerEvaluation" not in source
        assert "MultibaggerConfusion" not in source


def test_real_q_contract_projection_and_manifest_calendar_claim_are_strict(
    session: Session, tmp_path: Path
) -> None:
    bundle, _ = _complete_bundle(session)
    from inflector_database.models import MultibaggerLabelRun

    run = session.scalar(select(MultibaggerLabelRun))
    assert run is not None
    assert run.ordered_contracts_json == [
        {"code": "MB_2X_2Y", "threshold_multiple": "2", "horizon_calendar_years": 2},
        {"code": "MB_3X_3Y", "threshold_multiple": "3", "horizon_calendar_years": 3},
        {"code": "MB_5X_5Y", "threshold_multiple": "5", "horizon_calendar_years": 5},
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "historical_universe_run_id": str(bundle.historical_universe_run_id),
                    "backtest_run_ids": [str(item) for item in bundle.backtest_run_ids],
                    "label_run_ids": [str(item) for item in bundle.label_run_ids],
                    "calendar_integrity_status": "verified",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cannot self-certify"):
        load_evaluation_manifest(manifest)


@pytest.mark.parametrize(
    ("corruption", "message"),
    (
        ("source_run_key", "run key binding"),
        ("outcome_cutoff", "outcome cutoff"),
        ("unsupported_contract", "unsupported contract set"),
    ),
)
def test_q_label_run_metadata_corruption_fails_closed(
    session: Session, corruption: str, message: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    run = session.get(MultibaggerLabelRun, bundle.label_run_ids[0])
    assert run is not None
    if corruption == "source_run_key":
        run.source_backtest_run_key_sha256 = "0" * 64
    elif corruption == "outcome_cutoff":
        run.outcome_data_cutoff = OUTCOME_CUTOFF + timedelta(days=1)
    else:
        run.ordered_contracts_json = [
            *run.ordered_contracts_json,
            {"code": "MB_UNKNOWN", "threshold_multiple": "9", "horizon_calendar_years": 9},
        ]
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match=message):
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )


def test_later_q_cutoff_creates_append_only_evaluation_with_identical_predictions(
    session: Session,
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, backtest = _complete_bundle(session)
    first = build_multibagger_evaluation(
        session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
    )
    observations = [
        item.id
        for item in session.scalars(
            select(BacktestObservation).where(BacktestObservation.backtest_run_id == backtest)
        )
    ]
    dataset = session.scalar(select(ProviderDataset))
    assert dataset is not None
    later = OUTCOME_CUTOFF.replace(year=2027)
    second_labels = _labels(
        session,
        backtest_run_id=backtest,
        observations=observations,
        classifications=["negative", "positive", "negative"],
        dataset=dataset,
        outcome_cutoff=later,
    )
    second = build_multibagger_evaluation(
        session,
        policy=policy,
        bundles=(
            EvaluationCohortBundle(
                bundle.historical_universe_run_id,
                bundle.backtest_run_ids,
                (second_labels,),
                "fixture_verified",
            ),
        ),
        completed_at=later,
    )
    assert second.run_id != first.run_id
    assert second.created_run is True
    first_run = session.get(MultibaggerEvaluationRun, first.run_id)
    second_run = session.get(MultibaggerEvaluationRun, second.run_id)
    assert first_run is not None and second_run is not None
    first_fingerprints = sorted(
        item.prediction_fingerprint_sha256
        for cohort in first_run.cohorts
        for item in cohort.predictions
    )
    second_fingerprints = sorted(
        item.prediction_fingerprint_sha256
        for cohort in second_run.cohorts
        for item in cohort.predictions
    )
    assert first_fingerprints == second_fingerprints
    first_projection = sorted(
        (
            item.security_id,
            item.dense_score_rank,
            item.display_order,
            item.final_score,
        )
        for cohort in first_run.cohorts
        for item in cohort.predictions
    )
    second_projection = sorted(
        (
            item.security_id,
            item.dense_score_rank,
            item.display_order,
            item.final_score,
        )
        for cohort in second_run.cohorts
        for item in cohort.predictions
    )
    assert first_projection == second_projection
    assert (
        first_run.cohorts[0].summary_json["prediction_selection_checksum_sha256"]
        == second_run.cohorts[0].summary_json["prediction_selection_checksum_sha256"]
    )


def test_grouped_diagnostics_keep_outcome_contracts_separate(session: Session) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    alpha = session.scalar(select(BacktestObservation).where(BacktestObservation.symbol == "ALPHA"))
    assert alpha is not None
    three_x = session.scalar(
        select(MultibaggerOutcomeLabel).where(
            MultibaggerOutcomeLabel.backtest_observation_id == alpha.id,
            MultibaggerOutcomeLabel.contract_code == "MB_3X_3Y",
        )
    )
    assert three_x is not None
    three_x.classification = "negative"
    session.flush()
    result = build_multibagger_evaluation(
        session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
    )
    grouped = cast(dict[str, object], result.summary["grouped_diagnostics"])
    two_x = cast(dict[str, object], grouped["MB_2X_2Y"])
    three_x_groups = cast(dict[str, object], grouped["MB_3X_3Y"])
    two_x_top = cast(dict[str, object], two_x["TOP_5"])
    three_x_top = cast(dict[str, object], three_x_groups["TOP_5"])
    two_x_view = cast(dict[str, object], two_x_top["end_to_end"])
    three_x_view = cast(dict[str, object], three_x_top["end_to_end"])
    two_x_year = cast(dict[str, dict[str, object]], two_x_view["calendar_year"])["2020"]
    three_x_year = cast(dict[str, dict[str, object]], three_x_view["calendar_year"])["2020"]
    assert two_x_year["tp"] == 1
    assert two_x_year["fp"] == 1
    assert three_x_year["tp"] == 0
    assert three_x_year["fp"] == 2


def test_equal_scores_use_symbol_tie_order_and_audit_selection_boundary(
    session: Session,
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    dataset, configuration, _ = _foundation(session)
    members = [_identity(session, symbol) for symbol in ("BETA", "ALPHA", "GAMMA")]
    universe = _universe(session, dataset=dataset, members=members)
    backtest, observations, _ = _run(
        session,
        configuration=configuration,
        members=members,
        scores=[Decimal("90"), Decimal("90"), Decimal("80")],
    )
    labels = _labels(
        session,
        backtest_run_id=backtest,
        observations=observations,
        classifications=["positive", "negative", "negative"],
        dataset=dataset,
    )
    result = build_multibagger_evaluation(
        session,
        policy=policy,
        bundles=(
            EvaluationCohortBundle(
                universe, (backtest,), (labels,), "fixture_verified"
            ),
        ),
        completed_at=OUTCOME_CUTOFF,
    )
    run = session.get(MultibaggerEvaluationRun, result.run_id)
    assert run is not None
    predictions = sorted(run.cohorts[0].predictions, key=lambda item: item.display_order or 99)
    assert [item.historical_symbol for item in predictions] == ["ALPHA", "BETA", "GAMMA"]
    assert [item.dense_score_rank for item in predictions] == [1, 1, 2]
    boundary_ties = cast(
        dict[str, bool], run.cohorts[0].summary_json["selection_boundary_ties"]
    )
    assert boundary_ties["TOP_1_PERCENT"] is True


def test_corrupt_snapshot_fails_closed_instead_of_becoming_unrankable(session: Session) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    snapshot = session.scalar(select(ScoreSnapshot).where(ScoreSnapshot.final_score.is_not(None)))
    assert snapshot is not None
    snapshot.final_score = None
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match="snapshot integrity"):
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )


@pytest.mark.parametrize(
    "corruption",
    ("security_binding", "fingerprint_binding", "configuration_binding", "status_binding"),
)
def test_snapshot_observation_bindings_fail_closed(
    session: Session, corruption: str
) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, _ = _complete_bundle(session)
    observation = session.scalar(
        select(BacktestObservation).where(BacktestObservation.score_snapshot_id.is_not(None))
    )
    assert observation is not None
    snapshot = session.get(ScoreSnapshot, observation.score_snapshot_id)
    assert snapshot is not None
    if corruption == "security_binding":
        other = session.scalar(select(Security).where(Security.id != observation.security_id))
        assert other is not None
        snapshot.selected_security_id = other.id
    elif corruption == "fingerprint_binding":
        observation.snapshot_fingerprint_sha256 = "0" * 64
    elif corruption == "configuration_binding":
        snapshot.configuration_checksum_sha256 = "0" * 64
    else:
        observation.snapshot_status = "partial_component_set"
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match="snapshot"):
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )


def test_foreign_or_overlapping_q_labels_fail_before_cohort_join(session: Session) -> None:
    policy = load_multibagger_evaluation_policy(POLICY_PATH, repository_root=ROOT)
    bundle, backtest = _complete_bundle(session)
    dataset = session.scalar(select(ProviderDataset))
    configuration = session.scalar(select(ScoringConfiguration))
    assert dataset is not None and configuration is not None
    # A second supplied Q run for the same J run is invalid; no dict overwrite
    # may conceal it.
    observations = [
        item.id
        for item in session.scalars(
            select(BacktestObservation).where(BacktestObservation.backtest_run_id == backtest)
        )
    ]
    extra_labels = _labels(
        session,
        backtest_run_id=backtest,
        observations=observations,
        classifications=["positive", "negative", "positive"],
        dataset=dataset,
    )
    with pytest.raises(EvaluationIntegrityError, match="overlapping"):
        build_multibagger_evaluation(
            session,
            policy=policy,
            bundles=(
                EvaluationCohortBundle(
                    bundle.historical_universe_run_id,
                    bundle.backtest_run_ids,
                    (bundle.label_run_ids[0], extra_labels),
                    "fixture_verified",
                ),
            ),
            completed_at=OUTCOME_CUTOFF,
        )
    foreign_member = _identity(session, "FOREIGN")
    foreign_backtest, foreign_observations, _ = _run(
        session,
        configuration=configuration,
        members=[foreign_member],
        scores=[Decimal("70")],
    )
    from inflector_database.models import MultibaggerOutcomeLabel

    foreign = session.scalar(select(MultibaggerOutcomeLabel))
    assert foreign is not None
    foreign.backtest_observation_id = foreign_observations[0]
    session.flush()
    with pytest.raises(EvaluationIntegrityError, match="foreign observation"):
        build_multibagger_evaluation(
            session, policy=policy, bundles=(bundle,), completed_at=OUTCOME_CUTOFF
        )
