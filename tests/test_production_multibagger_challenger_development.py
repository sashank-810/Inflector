from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from inspect import signature
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, Table, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from inflector_data.historical_activation_policy import (
    load_historical_evaluation_activation_policy,
    load_multibagger_temporal_split_policy,
)
from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.multibagger_challenger import (
    ChallengerIntegrityError,
    _candidate_score_for_prediction,
    _jsonable,
    _load_outcome_facts_after_score_flush,
    _selection_projection,
    _validate_existing_evaluation_row,
    _validate_existing_metric,
    _validate_existing_score,
    challenger_readiness,
    confusion_class,
    counterfactual_candidate_score,
    create_challenger_experiment,
    evaluate_challenger_split,
    freeze_challenger_for_t,
    parse_candidate_weights,
    rank_candidate_scores,
    register_challenger_candidate,
    seal_challenger_candidate_set,
    select_validation_challenger,
    selection_count,
    validation_gate,
)
from inflector_data.multibagger_challenger_policy import (
    COMPONENT_CODES,
    load_multibagger_challenger_development_policy,
)
from inflector_database.base import Base
from inflector_database.models import (
    BacktestObservation,
    MultibaggerChallengerEvaluationRow,
    MultibaggerChallengerExperimentRun,
    MultibaggerChallengerMetric,
    MultibaggerChallengerScore,
    MultibaggerErrorAnalysisRow,
    MultibaggerErrorAnalysisRun,
    MultibaggerEvaluationRun,
    MultibaggerPredictionRow,
    MultibaggerTemporalSplitCutoff,
    MultibaggerTemporalSplitRun,
    ScoreSnapshot,
    ScoringConfiguration,
)
from inflector_database.score_repository import ScoreSnapshotRepository

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config/backtest/production_multibagger_challenger_development_v1.json"
POLICY_CHECKSUM = "17642f0e05640464b0166bc82740882e6fda5c99e62ca555c19d7be30ebe85e0"
ACTIVATION_PATH = ROOT / "config/backtest/production_historical_evaluation_activation_v1.json"
SPLIT_PATH = ROOT / "config/backtest/production_multibagger_temporal_split_v1.json"


def _weights(**changes: str) -> dict[str, str]:
    value = {
        "balance_sheet": "0.10",
        "business_catalyst": "0.20",
        "business_quality": "0.15",
        "cash_flow_quality": "0.10",
        "financial_inflection": "0.25",
        "low_market_attention": "0.05",
        "market_structure": "0.05",
        "valuation": "0.10",
    }
    value.update(changes)
    return value


def _metric_cells(
    *,
    candidate_f1: str = "0.6",
    champion_f1: str = "0.5",
    candidate_precision: str = "0.6",
    champion_precision: str = "0.5",
    candidate_recall: str = "0.6",
    champion_recall: str = "0.5",
    candidate_rank: str = "7",
    champion_rank: str = "8",
    secondary_f1: str = "0.5",
) -> dict[tuple[str, str, str], tuple[dict[str, object], dict[str, object]]]:
    primary_candidate: dict[str, object] = {
        "f1": candidate_f1,
        "precision": candidate_precision,
        "recall": candidate_recall,
        "positive_median_rank": candidate_rank,
        "rankability_delta": 0,
        "coverage_delta": 0,
    }
    primary_champion: dict[str, object] = {
        "f1": champion_f1,
        "precision": champion_precision,
        "recall": champion_recall,
        "positive_median_rank": champion_rank,
        "rankability_delta": 0,
        "coverage_delta": 0,
    }
    result = {
        ("MB_3X_3Y", "TOP_20", "end_to_end"): (
            primary_candidate,
            primary_champion,
        )
    }
    for outcome in ("MB_2X_2Y", "MB_5X_5Y"):
        result[(outcome, "TOP_20", "end_to_end")] = (
            {"f1": secondary_f1},
            {"f1": "0.5"},
        )
    return result


def _policy():
    return load_multibagger_challenger_development_policy(POLICY_PATH)


def _experiment_record(
    *, experiment_id: UUID = UUID(int=500)
) -> MultibaggerChallengerExperimentRun:
    policy = _policy()
    return MultibaggerChallengerExperimentRun(
        id=experiment_id,
        run_key_sha256="1" * 64,
        policy_code=policy.code,
        policy_checksum_sha256=policy.checksum_sha256,
        activation_policy_checksum_sha256=policy.inherited_checksums[
            "historical_evaluation_activation_policy_checksum_sha256"
        ],
        temporal_split_policy_checksum_sha256=policy.inherited_checksums[
            "temporal_split_policy_checksum_sha256"
        ],
        error_analysis_policy_checksum_sha256=policy.inherited_checksums[
            "error_analysis_policy_checksum_sha256"
        ],
        temporal_split_run_id=UUID(int=501),
        temporal_split_checksum_sha256="2" * 64,
        error_analysis_run_id=UUID(int=502),
        source_evaluation_run_id=UUID(int=503),
        source_evaluation_run_key_sha256="3" * 64,
        champion_scoring_configuration_id=UUID(int=504),
        champion_scoring_checksum_sha256=(
            "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
        ),
        champion_model_family="opportunity_score_v5",
        development_cutoff_ids_json=[str(UUID(int=505))],
        validation_cutoff_ids_json=[str(UUID(int=506))],
        holdout_cutoff_ids_json=[str(UUID(int=507))],
        candidate_algorithm_version="counterfactual_v5_top_level_weight_recomposition_v1",
        primary_objective_json={
            "outcome_contract": "MB_3X_3Y",
            "selection_contract": "TOP_20",
            "evaluation_view": "end_to_end",
            "metric": "f1",
        },
        evidence_class="fixture_only",
        status="draft",
        holdout_outcomes_accessed=False,
        summary_json={},
    )


def _analysis_run() -> MultibaggerErrorAnalysisRun:
    policy = _policy()
    return MultibaggerErrorAnalysisRun(
        id=UUID(int=502),
        run_key_sha256="4" * 64,
        error_policy_code="production_multibagger_error_analysis_v1",
        error_policy_checksum_sha256=policy.inherited_checksums[
            "error_analysis_policy_checksum_sha256"
        ],
        temporal_split_run_id=UUID(int=501),
        temporal_split_checksum_sha256="2" * 64,
        source_evaluation_run_id=UUID(int=503),
        evaluation_policy_checksum_sha256=policy.inherited_checksums[
            "multibagger_evaluation_policy_checksum_sha256"
        ],
        universe_policy_checksum_sha256=policy.inherited_checksums[
            "historical_universe_policy_checksum_sha256"
        ],
        outcome_policy_checksum_sha256=policy.inherited_checksums[
            "multibagger_outcome_policy_checksum_sha256"
        ],
        model_family="opportunity_score_v5",
        scoring_configuration_checksum_sha256=(
            "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
        ),
        holdout_outcomes_accessed=False,
        status="completed",
        summary_json={},
    )


def _analysis_row(run: MultibaggerErrorAnalysisRun) -> MultibaggerErrorAnalysisRow:
    projection = {
        "analysis_run_key_sha256": run.run_key_sha256,
        "source_confusion_row_id": str(UUID(int=510)),
        "split_code": "development",
        "confusion_class": "fn",
        "component_values": {"financial_inflection": "70"},
        "error_signature": "RANKABLE_JUST_BELOW_BOUNDARY",
    }
    return MultibaggerErrorAnalysisRow(
        id=UUID(int=509),
        multibagger_error_analysis_run_id=run.id,
        temporal_split_cutoff_id=UUID(int=511),
        source_confusion_row_id=UUID(int=510),
        prediction_row_id=UUID(int=512),
        split_code="development",
        historical_cutoff=datetime(2015, 1, 31, tzinfo=UTC),
        company_id=UUID(int=513),
        security_id=UUID(int=514),
        historical_symbol="FACT",
        outcome_contract_code="MB_3X_3Y",
        selection_contract_code="TOP_20",
        evaluation_view="end_to_end",
        confusion_class="fn",
        rankable=True,
        unranked_reason=None,
        snapshot_status="final_score_available",
        final_score=Decimal("70"),
        dense_score_rank=21,
        display_order=21,
        selection_boundary_count=20,
        selection_boundary_score=Decimal("71"),
        rank_positions_below_boundary=1,
        score_gap_to_boundary=Decimal("1"),
        threshold_hit_trading_date=None,
        calendar_days_to_threshold=None,
        trading_observations_to_threshold=None,
        endpoint_return=Decimal("1"),
        peak_price_multiple=Decimal("3"),
        maximum_drawdown=Decimal("-0.2"),
        available_component_codes_json=["financial_inflection"],
        component_values_json={"financial_inflection": "70"},
        score_bucket="[60,80)",
        error_signature="RANKABLE_JUST_BELOW_BOUNDARY",
        factual_provenance_json={},
        row_fingerprint_sha256=canonical_json_sha256(projection),
    )


def _evidence(row: MultibaggerErrorAnalysisRow) -> list[dict[str, object]]:
    return [
        {
            "reference_version": "s1_error_analysis_row_v1",
            "error_analysis_run_id": str(row.multibagger_error_analysis_run_id),
            "error_analysis_row_id": str(row.id),
            "split_code": row.split_code,
            "outcome_contract_code": row.outcome_contract_code,
            "selection_contract_code": row.selection_contract_code,
            "evaluation_view": row.evaluation_view,
            "error_signature": row.error_signature,
        }
    ]


def test_s2_policy_is_exact_and_checksum_bound(tmp_path: Path) -> None:
    policy = load_multibagger_challenger_development_policy(POLICY_PATH)
    assert policy.checksum_sha256 == POLICY_CHECKSUM
    assert policy.component_codes == COMPONENT_CODES
    assert (
        policy.primary_outcome_contract,
        policy.primary_selection_contract,
        policy.primary_evaluation_view,
        policy.primary_comparison_metric,
    ) == ("MB_3X_3Y", "TOP_20", "end_to_end", "f1")
    assert policy.maximum_candidates_per_experiment == 8
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    value["champion_scoring_configuration_checksum_sha256"] = "0" * 64
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="binding mismatch"):
        load_multibagger_challenger_development_policy(altered)
    value.pop("automatic_weight_search")
    altered.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fields are not exact"):
        load_multibagger_challenger_development_policy(altered)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"valuation": "-0.01", "balance_sheet": "0.21"}, "between zero and one"),
        ({"valuation": "1.01", "balance_sheet": "-0.01"}, "between zero and one"),
        ({"valuation": "0.11"}, "sum exactly to one"),
    ],
)
def test_candidate_weights_reject_invalid_decimal_vectors(
    mutation: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_candidate_weights(_weights(**mutation))


def test_candidate_weights_require_exact_codes_strings_and_are_deterministic() -> None:
    parsed = parse_candidate_weights(_weights())
    assert tuple(code for code, _ in parsed.ordered) == COMPONENT_CODES
    assert (
        parsed.checksum_sha256
        == parse_candidate_weights(dict(reversed(_weights().items()))).checksum_sha256
    )
    missing = _weights()
    missing.pop("valuation")
    with pytest.raises(ValueError, match="exactly the eight"):
        parse_candidate_weights(missing)
    extra: dict[str, object] = {**_weights(), "extra": "0"}
    with pytest.raises(ValueError, match="exactly the eight"):
        parse_candidate_weights(extra)
    floats: dict[str, object] = dict(_weights())
    floats["valuation"] = 0.1
    with pytest.raises(ValueError, match="Decimal strings"):
        parse_candidate_weights(floats)


def test_counterfactual_score_uses_exact_components_not_contributions() -> None:
    weights = parse_candidate_weights(_weights())
    scores = {code: Decimal(index * 10) for index, code in enumerate(COMPONENT_CODES, 1)}
    expected = sum((weight * scores[code] for code, weight in weights.ordered), Decimal("0"))
    assert counterfactual_candidate_score(weights, scores) == expected
    assert isinstance(expected, Decimal)
    with pytest.raises(ChallengerIntegrityError, match="all eight"):
        counterfactual_candidate_score(weights, {"valuation": Decimal("50")})


def test_ranking_ties_and_all_six_selection_contracts_are_deterministic() -> None:
    rows = (
        (UUID(int=1), "BETA", UUID(int=9), Decimal("90"), True, None),
        (UUID(int=2), "ALPHA", UUID(int=8), Decimal("90.00"), True, None),
        (UUID(int=3), "ALPHA", UUID(int=7), Decimal("90"), True, None),
        (UUID(int=4), "NONE", UUID(int=6), None, False, "research_failed"),
    )
    ranked = {item.prediction_id: item for item in rank_candidate_scores(rows)}
    assert ranked[UUID(int=3)].display_order == 1
    assert ranked[UUID(int=2)].display_order == 2
    assert ranked[UUID(int=1)].display_order == 3
    assert {ranked[UUID(int=value)].dense_score_rank for value in (1, 2, 3)} == {1}
    assert ranked[UUID(int=4)].unranked_reason == "research_failed"
    expected = {
        "TOP_5": 5,
        "TOP_10": 10,
        "TOP_20": 20,
        "TOP_1_PERCENT": 1,
        "TOP_5_PERCENT": 3,
        "TOP_10_PERCENT": 5,
    }
    assert {code: selection_count(code, 50) for code in expected} == expected
    assert all(selection_count(code, 0) == 0 for code in expected)
    assert selection_count("TOP_1_PERCENT", 1) == 1
    flags, ties = _selection_projection(tuple(ranked.values()), rankable_count=3)
    assert set(flags[UUID(int=3)]) == set(expected)
    assert all(flags[UUID(int=3)].values())
    assert all(ties.values()) is False


def test_selection_boundary_tie_is_audited_without_expanding_budget() -> None:
    rows = tuple(
        (UUID(int=index), f"S{index}", UUID(int=index), Decimal("50"), True, None)
        for index in range(1, 7)
    )
    ranked = rank_candidate_scores(rows)
    flags, ties = _selection_projection(ranked, rankable_count=6)
    assert sum(item["TOP_5"] for item in flags.values()) == 5
    assert ties["TOP_5"] is True


@pytest.mark.parametrize(
    ("rankable", "selected", "label", "view", "expected"),
    [
        (True, True, "positive", "end_to_end", "tp"),
        (True, True, "negative", "end_to_end", "fp"),
        (True, False, "positive", "end_to_end", "fn"),
        (True, False, "negative", "end_to_end", "tn"),
        (False, False, "positive", "end_to_end", "fn"),
        (False, False, "negative", "end_to_end", "tn"),
        (False, False, "positive", "rankable_only", "excluded_rankability_for_rankable_only"),
        (True, False, "unmatured", "end_to_end", "excluded_unmatured"),
        (True, False, "unavailable", "end_to_end", "excluded_unavailable"),
    ],
)
def test_confusion_semantics_match_r(
    rankable: bool, selected: bool, label: str, view: str, expected: str
) -> None:
    assert (
        confusion_class(
            rankable=rankable,
            selected=selected,
            actual_label=label,
            evaluation_view=view,
        )
        == expected
    )


def test_validation_gate_is_conservative_and_deterministic() -> None:
    accepted, reasons = validation_gate(_metric_cells())
    assert accepted is True
    assert reasons == ()
    assert validation_gate(_metric_cells(candidate_precision="0.4"))[0] is False
    assert validation_gate(_metric_cells(secondary_f1="0.4"))[0] is False
    assert validation_gate(_metric_cells(candidate_f1="0.5"))[0] is False
    cells = _metric_cells()
    cells[("MB_3X_3Y", "TOP_20", "end_to_end")][0]["f1"] = None
    assert validation_gate(cells) == (False, ("insufficient_validation_metric_support",))


def test_multiple_passing_candidates_choose_one_in_predeclared_order() -> None:
    selected, assessments = select_validation_challenger(
        (
            ("candidate_b", _metric_cells(candidate_f1="0.7")),
            ("candidate_a", _metric_cells(candidate_f1="0.7")),
            ("candidate_bad", _metric_cells(candidate_precision="0.4")),
        )
    )
    assert selected == "candidate_a"
    assert assessments["candidate_a"] == ()
    assert assessments["candidate_bad"] == ("primary_precision_guardrail_failed",)
    selected_none, _ = select_validation_challenger(
        (("candidate_bad", _metric_cells(candidate_f1="0.4")),)
    )
    assert selected_none is None


def test_real_readiness_requires_a_database_not_a_caller_status() -> None:
    policy = _policy()
    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_PATH, repository_root=ROOT
    )
    split_policy = load_multibagger_temporal_split_policy(SPLIT_PATH)
    assert challenger_readiness(
        None,
        policy=policy,
        activation_policy=activation,
        split_policy=split_policy,
        source_evaluation_run_id=UUID(int=1),
        session_calendar_run_id=UUID(int=2),
        temporal_split_run_id=UUID(int=3),
        error_analysis_run_id=UUID(int=4),
        database_available=False,
    ) == {
        "status": "environment_blocked",
        "message": "REAL S2 CHALLENGER SELECTION: ENVIRONMENT-BLOCKED",
        "blockers": ["production_database_unavailable"],
    }


def test_database_backed_readiness_requires_the_coherent_s1_r_champion_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    policy = _policy()
    activation = load_historical_evaluation_activation_policy(
        ACTIVATION_PATH, repository_root=ROOT
    )
    split_policy = load_multibagger_temporal_split_policy(SPLIT_PATH)
    evaluation_id = UUID(int=800)
    calendar_id = UUID(int=801)
    split_id = UUID(int=802)
    analysis_id = UUID(int=803)
    with factory() as session:
        evaluation = MultibaggerEvaluationRun(
            id=evaluation_id,
            run_key_sha256="5" * 64,
            evaluation_policy_code="production_multibagger_evaluation_v1",
            evaluation_policy_checksum_sha256=policy.inherited_checksums[
                "multibagger_evaluation_policy_checksum_sha256"
            ],
            backtest_policy_checksum_sha256=policy.inherited_checksums[
                "backtest_policy_checksum_sha256"
            ],
            universe_policy_checksum_sha256=policy.inherited_checksums[
                "historical_universe_policy_checksum_sha256"
            ],
            outcome_policy_checksum_sha256=policy.inherited_checksums[
                "multibagger_outcome_policy_checksum_sha256"
            ],
            research_profile_checksum_sha256=policy.inherited_checksums[
                "research_profile_checksum_sha256"
            ],
            model_family="opportunity_score_v5",
            scoring_configuration_checksum_sha256=(
                "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
            ),
            ordered_bundle_checksums_json=[],
            selection_contracts_json=[],
            outcome_contracts_json=[],
            algorithm_versions_json={},
            inputs_json={},
            status="completed",
            summary_json={},
        )
        dates = [datetime(2010, month, 1, tzinfo=UTC) for month in range(1, 13)]
        validation_dates = [datetime(2011, month, 1, tzinfo=UTC) for month in range(1, 13)]
        holdout_dates = [datetime(2012, month, 1, tzinfo=UTC) for month in range(1, 13)]
        split = MultibaggerTemporalSplitRun(
            id=split_id,
            run_key_sha256="6" * 64,
            split_policy_code="production_multibagger_temporal_split_v1",
            split_policy_checksum_sha256=policy.inherited_checksums[
                "temporal_split_policy_checksum_sha256"
            ],
            source_evaluation_run_id=evaluation_id,
            source_evaluation_run_key_sha256=evaluation.run_key_sha256,
            session_calendar_run_id=calendar_id,
            ordered_eligible_cutoffs_json=[
                item.isoformat() for item in dates + validation_dates + holdout_dates
            ],
            development_cutoffs_json=[item.isoformat() for item in dates],
            validation_cutoffs_json=[item.isoformat() for item in validation_dates],
            holdout_cutoffs_json=[item.isoformat() for item in holdout_dates],
            split_checksum_sha256="7" * 64,
            outcome_data_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
            status="completed",
            summary_json={},
        )
        analysis = MultibaggerErrorAnalysisRun(
            id=analysis_id,
            run_key_sha256="8" * 64,
            error_policy_code="production_multibagger_error_analysis_v1",
            error_policy_checksum_sha256=policy.inherited_checksums[
                "error_analysis_policy_checksum_sha256"
            ],
            temporal_split_run_id=split_id,
            temporal_split_checksum_sha256=split.split_checksum_sha256,
            source_evaluation_run_id=evaluation_id,
            evaluation_policy_checksum_sha256=evaluation.evaluation_policy_checksum_sha256,
            universe_policy_checksum_sha256=evaluation.universe_policy_checksum_sha256,
            outcome_policy_checksum_sha256=evaluation.outcome_policy_checksum_sha256,
            model_family=evaluation.model_family,
            scoring_configuration_checksum_sha256=(
                evaluation.scoring_configuration_checksum_sha256
            ),
            holdout_outcomes_accessed=False,
            status="completed",
            summary_json={},
        )
        config = ScoringConfiguration(
            id=UUID(int=804),
            model_version_id=UUID(int=805),
            configuration_name="production_opportunity_v5",
            configuration_version="5",
            status="active",
            configuration_json={},
            checksum_sha256=(
                "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
            ),
        )
        rows = []
        for code, values, offset in (
            ("development", dates, 900),
            ("validation", validation_dates, 1000),
            ("holdout", holdout_dates, 1100),
        ):
            rows.extend(
                MultibaggerTemporalSplitCutoff(
                    id=UUID(int=offset + index),
                    multibagger_temporal_split_run_id=split_id,
                    multibagger_evaluation_cohort_id=UUID(int=offset + 100 + index),
                    historical_cutoff=value,
                    split_code=code,
                    assignment_fingerprint_sha256=f"{offset + index:064x}",
                )
                for index, value in enumerate(values)
            )
        session.add_all((evaluation, split, analysis, config, *rows))
        session.flush()
        monkeypatch.setattr(
            "inflector_data.multibagger_challenger.historical_evaluation_readiness",
            lambda *_args, **_kwargs: {"status": "READY", "blockers": []},
        )

        def readiness():
            return challenger_readiness(
                session,
                policy=policy,
                activation_policy=activation,
                split_policy=split_policy,
                source_evaluation_run_id=evaluation_id,
                session_calendar_run_id=calendar_id,
                temporal_split_run_id=split_id,
                error_analysis_run_id=analysis_id,
            )

        assert readiness()["status"] == "ready"
        analysis.status = "failed"
        session.flush()
        assert "accepted_s1_error_analysis_unavailable" in cast(
            list[object], readiness()["blockers"]
        )
        analysis.status = "completed"
        split.source_evaluation_run_id = UUID(int=9999)
        session.flush()
        assert "accepted_s1_temporal_split_unavailable" in cast(
            list[object], readiness()["blockers"]
        )
        split.source_evaluation_run_id = evaluation_id
        session.delete(config)
        session.flush()
        assert "accepted_champion_scoring_configuration_unavailable" in cast(
            list[object], readiness()["blockers"]
        )
    assert "evidence_class" not in signature(create_challenger_experiment).parameters
    engine.dispose()


def test_sealed_candidate_set_rejects_development_and_registration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        run = _analysis_run()
        row = _analysis_row(run)
        experiment = _experiment_record()
        session.add_all((run, row, experiment))
        candidate = register_challenger_candidate(
            session,
            policy=_policy(),
            experiment_id=experiment.id,
            candidate_code="hypothesis_a",
            weights=_weights(),
            hypothesis="Factual development association",
            development_evidence=_evidence(row),
        )
        experiment.status = "development_evaluated"
        checksum = seal_challenger_candidate_set(
            session, policy=_policy(), experiment_id=experiment.id
        )
        assert checksum == experiment.sealed_candidate_set_checksum_sha256
        assert (
            seal_challenger_candidate_set(session, policy=_policy(), experiment_id=experiment.id)
            == checksum
        )
        with pytest.raises(ChallengerIntegrityError, match="forbidden after sealing"):
            evaluate_challenger_split(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                split_code="development",
                evaluated_at=datetime.now(UTC),
            )
        with pytest.raises(ChallengerIntegrityError, match="immutable"):
            register_challenger_candidate(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                candidate_code="hypothesis_b",
                weights=_weights(valuation="0.11", balance_sheet="0.09"),
                hypothesis="Another factual hypothesis",
                development_evidence=_evidence(row),
            )
        candidate.hypothesis = "tampered after seal"
        with pytest.raises(ChallengerIntegrityError, match="definition conflicts"):
            evaluate_challenger_split(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                split_code="validation",
                evaluated_at=datetime.now(UTC),
            )
        with pytest.raises(ChallengerIntegrityError, match="definition conflicts"):
            seal_challenger_candidate_set(session, policy=_policy(), experiment_id=experiment.id)
        candidate.hypothesis = "Factual development association"
        experiment.validation_opened_at = datetime.now(UTC)
        experiment.status = "validation_evaluated"
        with pytest.raises(ChallengerIntegrityError, match="forbidden after sealing"):
            evaluate_challenger_split(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                split_code="development",
                evaluated_at=datetime.now(UTC),
            )
        with pytest.raises(ChallengerIntegrityError, match="immutable"):
            register_challenger_candidate(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                candidate_code="post_validation",
                weights=_weights(valuation="0.11", balance_sheet="0.09"),
                hypothesis="Forbidden post-validation registration",
                development_evidence=_evidence(row),
            )
    engine.dispose()


def test_development_evidence_is_resolved_and_complete_definition_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        run = _analysis_run()
        row = _analysis_row(run)
        experiment = _experiment_record()
        session.add_all((run, row, experiment))
        first = register_challenger_candidate(
            session,
            policy=_policy(),
            experiment_id=experiment.id,
            candidate_code="resolved_fact",
            weights=_weights(),
            hypothesis="  normalized   factual association ",
            development_evidence=_evidence(row),
        )
        same = register_challenger_candidate(
            session,
            policy=_policy(),
            experiment_id=experiment.id,
            candidate_code="resolved_fact",
            weights=_weights(),
            hypothesis="normalized factual association",
            development_evidence=_evidence(row),
        )
        assert same.id == first.id
        assert first.development_evidence_checksum_sha256 == canonical_json_sha256(_evidence(row))
        fabricated = _evidence(row)
        fabricated[0]["error_signature"] = "CALLER_INVENTED"
        with pytest.raises(ChallengerIntegrityError, match="persisted S1 fact"):
            register_challenger_candidate(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                candidate_code="fabricated",
                weights=_weights(valuation="0.11", balance_sheet="0.09"),
                hypothesis="Invented evidence",
                development_evidence=fabricated,
            )
        with pytest.raises(ChallengerIntegrityError, match="existing definition"):
            register_challenger_candidate(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
                candidate_code="resolved_fact",
                weights=_weights(),
                hypothesis="changed immutable hypothesis",
                development_evidence=_evidence(row),
            )
    engine.dispose()


def test_append_only_reuse_validates_score_evaluation_and_metric_projections() -> None:
    score_values: dict[str, object] = {
        "experiment_run_id": UUID(int=600),
        "candidate_id": UUID(int=601),
        "split_code": "development",
        "source_evaluation_cohort_id": UUID(int=602),
        "source_prediction_row_id": UUID(int=603),
        "security_id": UUID(int=604),
        "historical_symbol": "IMMUTABLE",
        "source_score_snapshot_id": UUID(int=605),
        "source_snapshot_fingerprint_sha256": "a" * 64,
        "component_state_checksum_sha256": "b" * 64,
        "candidate_score": Decimal("75"),
        "rankable": True,
        "unranked_reason": None,
        "display_order": 1,
        "dense_score_rank": 1,
        "rankable_count": 1,
        "cohort_size": 1,
        "selection_flags_json": {"TOP_5": True},
        "selection_boundary_ties_json": {"TOP_5": False},
        "score_fingerprint_sha256": "c" * 64,
    }
    score = MultibaggerChallengerScore(**score_values)
    _validate_existing_score(score, score_values)
    changed_score = {**score_values, "source_snapshot_fingerprint_sha256": "d" * 64}
    with pytest.raises(ChallengerIntegrityError, match="candidate score"):
        _validate_existing_score(score, changed_score)
    changed_component = {**score_values, "component_state_checksum_sha256": "e" * 64}
    with pytest.raises(ChallengerIntegrityError, match="candidate score"):
        _validate_existing_score(score, changed_component)
    changed_rank = {**score_values, "display_order": 2}
    with pytest.raises(ChallengerIntegrityError, match="candidate score"):
        _validate_existing_score(score, changed_rank)

    evaluation_values: dict[str, object] = {
        "experiment_run_id": UUID(int=600),
        "candidate_id": UUID(int=601),
        "candidate_score_id": UUID(int=606),
        "source_confusion_row_id": UUID(int=607),
        "split_code": "development",
        "outcome_contract_code": "MB_3X_3Y",
        "selection_contract_code": "TOP_20",
        "evaluation_view": "end_to_end",
        "selected": True,
        "actual_label": "positive",
        "confusion_class": "tp",
        "row_fingerprint_sha256": "f" * 64,
    }
    evaluation = MultibaggerChallengerEvaluationRow(**evaluation_values)
    _validate_existing_evaluation_row(evaluation, evaluation_values)
    with pytest.raises(ChallengerIntegrityError, match="evaluation row"):
        _validate_existing_evaluation_row(
            evaluation, {**evaluation_values, "source_confusion_row_id": UUID(int=608)}
        )

    metric_values: dict[str, object] = {
        "experiment_run_id": UUID(int=600),
        "candidate_id": UUID(int=601),
        "split_code": "development",
        "outcome_contract_code": "MB_3X_3Y",
        "selection_contract_code": "TOP_20",
        "evaluation_view": "end_to_end",
        "candidate_metrics_json": {"f1": "0.6"},
        "champion_metrics_json": {"f1": "0.5"},
        "delta_metrics_json": {"f1": "0.1"},
        "rankability_delta": 0,
        "coverage_delta": 0,
        "metric_fingerprint_sha256": "0" * 64,
    }
    metric = MultibaggerChallengerMetric(**metric_values)
    _validate_existing_metric(metric, metric_values)
    with pytest.raises(ChallengerIntegrityError, match="candidate metric"):
        _validate_existing_metric(
            metric, {**metric_values, "candidate_metrics_json": {"f1": "0.7"}}
        )


@pytest.mark.parametrize("mismatch", ("knowledge_cutoff", "final_score"))
def test_frozen_snapshot_pit_binding_fails_closed(
    mismatch: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cutoff = datetime(2015, 1, 31, tzinfo=UTC)
    snapshot_id = UUID(int=700)
    security_id = UUID(int=701)
    company_id = UUID(int=702)
    observation = BacktestObservation(
        id=UUID(int=703),
        backtest_run_id=UUID(int=704),
        company_id=company_id,
        security_id=security_id,
        score_snapshot_id=snapshot_id,
        symbol="PIT",
        knowledge_cutoff=cutoff,
        observation_status="snapshot_frozen",
        snapshot_status="final_score_available",
        snapshot_fingerprint_sha256="a" * 64,
        research_state_projection_version="v1",
        research_state_sha256="b" * 64,
        research_state_changed=False,
        detail_json={},
    )
    prediction = MultibaggerPredictionRow(
        id=UUID(int=705),
        multibagger_evaluation_cohort_id=UUID(int=706),
        backtest_observation_id=observation.id,
        company_id=company_id,
        security_id=security_id,
        historical_symbol="PIT",
        score_snapshot_id=snapshot_id,
        snapshot_fingerprint_sha256="a" * 64,
        snapshot_status="final_score_available",
        rankable=True,
        unranked_reason=None,
        final_score=Decimal("80"),
        dense_score_rank=1,
        display_order=1,
        available_component_codes_json=list(COMPONENT_CODES),
        prediction_fingerprint_sha256="",
    )
    prediction_projection = {
        "backtest_observation_id": prediction.backtest_observation_id,
        "company_id": prediction.company_id,
        "security_id": prediction.security_id,
        "historical_symbol": prediction.historical_symbol,
        "score_snapshot_id": prediction.score_snapshot_id,
        "snapshot_fingerprint_sha256": prediction.snapshot_fingerprint_sha256,
        "snapshot_status": prediction.snapshot_status,
        "rankable": prediction.rankable,
        "unranked_reason": prediction.unranked_reason,
        "final_score": prediction.final_score,
        "dense_score_rank": prediction.dense_score_rank,
        "display_order": prediction.display_order,
        "available_component_codes_json": prediction.available_component_codes_json,
    }
    prediction.prediction_fingerprint_sha256 = canonical_json_sha256(
        _jsonable(prediction_projection)
    )
    snapshot = ScoreSnapshot(
        id=snapshot_id,
        selected_security_id=security_id,
        algorithm_version="score_snapshot_v5",
        snapshot_fingerprint_sha256="a" * 64,
        scoring_configuration_id=UUID(int=504),
        configuration_checksum_sha256=(
            "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
        ),
        knowledge_cutoff=(
            datetime(2015, 2, 1, tzinfo=UTC) if mismatch == "knowledge_cutoff" else cutoff
        ),
        eligibility_eligible=True,
        final_score=Decimal("81") if mismatch == "final_score" else Decimal("80"),
        snapshot_status="final_score_available",
    )
    snapshot.components = []

    class FakeSession:
        def get(self, model, identity):
            if model is BacktestObservation and identity == observation.id:
                return observation
            if model is ScoreSnapshot and identity == snapshot.id:
                return snapshot
            return None

    monkeypatch.setattr(
        ScoreSnapshotRepository,
        "validate_persisted_snapshot",
        lambda _self, record: record,
    )
    with pytest.raises(ChallengerIntegrityError, match="snapshot binding mismatch"):
        _candidate_score_for_prediction(
            cast(Session, FakeSession()),
            experiment=_experiment_record(),
            prediction=prediction,
            weights=parse_candidate_weights(_weights()),
        )


def test_freeze_rechecks_database_backed_s1_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        experiment = _experiment_record()
        experiment.status = "validation_evaluated"
        experiment.evidence_class = "production_authoritative"
        split = MultibaggerTemporalSplitRun(
            id=experiment.temporal_split_run_id,
            run_key_sha256="9" * 64,
            split_policy_code="production_multibagger_temporal_split_v1",
            split_policy_checksum_sha256=experiment.temporal_split_policy_checksum_sha256,
            source_evaluation_run_id=experiment.source_evaluation_run_id,
            source_evaluation_run_key_sha256=experiment.source_evaluation_run_key_sha256,
            session_calendar_run_id=UUID(int=1200),
            ordered_eligible_cutoffs_json=[],
            development_cutoffs_json=[],
            validation_cutoffs_json=[],
            holdout_cutoffs_json=[],
            split_checksum_sha256=experiment.temporal_split_checksum_sha256,
            outcome_data_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
            status="completed",
            summary_json={},
        )
        session.add_all((experiment, split))
        session.flush()
        calls: list[dict[str, object]] = []

        def blocked_readiness(_session, **kwargs):
            calls.append(kwargs)
            return {
                "status": "data_blocked",
                "message": "REAL S2 CHALLENGER SELECTION: DATA-BLOCKED",
                "blockers": ["accepted_s1_historical_readiness_unavailable"],
            }

        monkeypatch.setattr(
            "inflector_data.multibagger_challenger.challenger_readiness",
            blocked_readiness,
        )
        with pytest.raises(ChallengerIntegrityError, match="cannot freeze"):
            freeze_challenger_for_t(
                session,
                policy=_policy(),
                experiment_id=experiment.id,
            )
        assert calls[0]["source_evaluation_run_id"] == experiment.source_evaluation_run_id
        assert calls[0]["session_calendar_run_id"] == split.session_calendar_run_id
        assert calls[0]["temporal_split_run_id"] == split.id
        assert calls[0]["error_analysis_run_id"] == experiment.error_analysis_run_id
    engine.dispose()


def test_database_constraint_forbids_holdout_evaluation_rows() -> None:
    table = cast(Table, MultibaggerChallengerEvaluationRow.__table__)
    constraints = {
        item.name: str(item.sqltext)
        for item in table.constraints
        if isinstance(item, CheckConstraint)
    }
    assert "holdout" not in constraints["ck_multibagger_challenger_evaluation_split"]
    assert "development" in constraints["ck_multibagger_challenger_evaluation_split"]
    assert "validation" in constraints["ck_multibagger_challenger_evaluation_split"]


def test_validation_score_insert_precedes_first_outcome_sql() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    statements: list[str] = []
    parameters: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_sql(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(str(statement).lower())
        parameters.append(repr(_parameters).lower())

    factory = sessionmaker(bind=engine)
    cohort_id = UUID(int=100)
    holdout_id = UUID(int=200)
    with factory() as session:
        session.add(
            MultibaggerChallengerScore(
                experiment_run_id=UUID(int=101),
                candidate_id=UUID(int=102),
                split_code="validation",
                source_evaluation_cohort_id=cohort_id,
                source_prediction_row_id=UUID(int=103),
                security_id=UUID(int=104),
                historical_symbol="FROZEN",
                source_score_snapshot_id=None,
                source_snapshot_fingerprint_sha256=None,
                component_state_checksum_sha256="a" * 64,
                candidate_score=None,
                rankable=False,
                unranked_reason="research_failed",
                display_order=None,
                dense_score_rank=None,
                rankable_count=0,
                cohort_size=1,
                selection_flags_json={
                    code: False
                    for code in (
                        "TOP_5",
                        "TOP_10",
                        "TOP_20",
                        "TOP_1_PERCENT",
                        "TOP_5_PERCENT",
                        "TOP_10_PERCENT",
                    )
                },
                selection_boundary_ties_json={
                    code: False
                    for code in (
                        "TOP_5",
                        "TOP_10",
                        "TOP_20",
                        "TOP_1_PERCENT",
                        "TOP_5_PERCENT",
                        "TOP_10_PERCENT",
                    )
                },
                score_fingerprint_sha256="b" * 64,
            )
        )
        assert (
            _load_outcome_facts_after_score_flush(
                session,
                cohort_ids=(cohort_id,),
                forbidden_holdout_ids={holdout_id},
            )
            == ()
        )
        with pytest.raises(ChallengerIntegrityError, match="holdout cohort"):
            _load_outcome_facts_after_score_flush(
                session,
                cohort_ids=(holdout_id,),
                forbidden_holdout_ids={holdout_id},
            )
    insert_at = next(
        index
        for index, statement in enumerate(statements)
        if "insert into multibagger_challenger_scores" in statement
    )
    outcome_at = next(
        index
        for index, statement in enumerate(statements)
        if "from multibagger_confusion_rows" in statement
    )
    assert insert_at < outcome_at
    assert cohort_id.hex in parameters[outcome_at]
    assert holdout_id.hex not in parameters[outcome_at]
    engine.dispose()


def test_no_automatic_search_or_holdout_command_exists() -> None:
    service = (
        (ROOT / "packages/data/inflector_data/multibagger_challenger.py")
        .read_text(encoding="utf-8")
        .lower()
    )
    cli = (ROOT / "packages/data/inflector_data/backtest_cli.py").read_text(encoding="utf-8")
    assert "grid_search" not in service
    assert "optuna" not in service
    assert "evaluate-challenger-holdout" not in cli
    assert "production_opportunity_v5" not in service


def test_migration_0026_adds_only_s2_tables_and_downgrades(tmp_path: Path) -> None:
    database = tmp_path / "s2-migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "20261006_0025")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "20261007_0026")
    after = set(inspect(engine).get_table_names())
    assert after - before == {
        "multibagger_challenger_experiment_runs",
        "multibagger_challenger_candidates",
        "multibagger_challenger_scores",
        "multibagger_challenger_evaluation_rows",
        "multibagger_challenger_metrics",
        "multibagger_challenger_freezes",
    }
    candidate_columns = {
        item["name"] for item in inspect(engine).get_columns("multibagger_challenger_candidates")
    }
    assert "development_evidence_checksum_sha256" in candidate_columns
    command.downgrade(config, "20261006_0025")
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()
