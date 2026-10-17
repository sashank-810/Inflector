"""Production T immutable temporal-holdout contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
from inflector_data.multibagger_challenger import confusion_class, selection_count
from inflector_data.multibagger_challenger_policy import (
    SELECTION_CONTRACTS,
    load_multibagger_challenger_development_policy,
)
from inflector_data.multibagger_holdout import (
    FIXTURE_EVIDENCE,
    PRODUCTION_EVIDENCE,
    HoldoutInputGraph,
    HoldoutIntegrityError,
    _expected_decision_values,
    _expected_evaluation_row_values,
    _load_holdout_outcomes_after_score_flush,
    _run_identity,
    _validate_completed_holdout_graph,
    _validate_projection,
    _validate_split_membership,
    _validated_run_input,
    create_fixture_temporal_holdout_run,
    create_temporal_holdout_run,
    evaluate_temporal_holdout,
    freeze_temporal_holdout_scores,
    holdout_decision_gate,
    temporal_holdout_readiness,
)
from inflector_data.multibagger_holdout_policy import (
    INHERITED_CHECKSUMS,
    load_multibagger_temporal_holdout_policy,
)
from inflector_database.base import Base
from inflector_database.models import (
    MultibaggerChallengerExperimentRun,
    MultibaggerConfusionRow,
    MultibaggerEvaluationCohort,
    MultibaggerEvaluationRun,
    MultibaggerHoldoutDecision,
    MultibaggerHoldoutEvaluationRow,
    MultibaggerHoldoutEvaluationRun,
    MultibaggerHoldoutMetric,
    MultibaggerHoldoutScore,
    MultibaggerTemporalSplitCutoff,
    MultibaggerTemporalSplitRun,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config/backtest/production_multibagger_temporal_holdout_v1.json"
POLICY_CHECKSUM = "309547a20c01abcece498c76727abf032d643efde6207dab2667519bc28ab45b"
S2_POLICY_PATH = ROOT / "config/backtest/production_multibagger_challenger_development_v1.json"
ACTIVATION_POLICY_PATH = (
    ROOT / "config/backtest/production_historical_evaluation_activation_v1.json"
)
SPLIT_POLICY_PATH = ROOT / "config/backtest/production_multibagger_temporal_split_v1.json"


def _minimal_holdout_run(
    *,
    run_id: UUID = UUID(int=7000),
    status: str = "scores_frozen",
    evidence_class: str = FIXTURE_EVIDENCE,
    score_checksum: str | None = None,
    opened_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> MultibaggerHoldoutEvaluationRun:
    return MultibaggerHoldoutEvaluationRun(
        id=run_id,
        run_key_sha256="a" * 64,
        policy_code="production_multibagger_temporal_holdout_v1",
        policy_checksum_sha256=POLICY_CHECKSUM,
        source_evaluation_run_id=UUID(int=7001),
        source_evaluation_run_key_sha256="b" * 64,
        temporal_split_run_id=UUID(int=7002),
        temporal_split_checksum_sha256="c" * 64,
        holdout_cohort_ids_json=[str(UUID(int=7003))],
        holdout_membership_checksum_sha256="d" * 64,
        champion_scoring_configuration_id=UUID(int=7004),
        champion_scoring_checksum_sha256=INHERITED_CHECKSUMS[
            "champion_scoring_configuration_checksum_sha256"
        ],
        s2_experiment_run_id=UUID(int=7005),
        s2_experiment_run_key_sha256="e" * 64,
        evidence_class=evidence_class,
        terminal_mode="champion_only",
        challenger_freeze_id=None,
        challenger_freeze_key_sha256=None,
        challenger_candidate_id=None,
        challenger_candidate_key_sha256=None,
        challenger_candidate_checksum_sha256=None,
        primary_objective_json={
            "outcome_contract": "MB_3X_3Y",
            "selection_contract": "TOP_20",
            "evaluation_view": "end_to_end",
            "metric": "f1",
        },
        outcome_data_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
        score_selection_checksum_sha256=score_checksum,
        holdout_opened_at=opened_at,
        status=status,
        summary_json={
            "real_evidence": evidence_class == PRODUCTION_EVIDENCE,
            "production_promotion_performed": False,
        },
        completed_at=completed_at,
    )


def _mechanics_graph(*, experiment_evidence: str = FIXTURE_EVIDENCE) -> HoldoutInputGraph:
    experiment = SimpleNamespace(
        id=UUID(int=7005),
        run_key_sha256="e" * 64,
        evidence_class=experiment_evidence,
        champion_scoring_configuration_id=UUID(int=7004),
    )
    split = SimpleNamespace(
        id=UUID(int=7002),
        run_key_sha256="f" * 64,
        split_checksum_sha256="c" * 64,
        outcome_data_cutoff=datetime(2020, 1, 1, tzinfo=UTC),
    )
    evaluation = SimpleNamespace(id=UUID(int=7001), run_key_sha256="b" * 64)
    return HoldoutInputGraph(
        experiment=cast(MultibaggerChallengerExperimentRun, experiment),
        split=cast(MultibaggerTemporalSplitRun, split),
        evaluation=cast(MultibaggerEvaluationRun, evaluation),
        holdout_cohort_ids=(UUID(int=7003),),
        holdout_membership_checksum_sha256="d" * 64,
        terminal_mode="champion_only",
        freeze=None,
        candidate=None,
    )


def test_t_policy_is_strict_checksum_bound_and_uses_the_accepted_q_universe(
    tmp_path: Path,
) -> None:
    policy = load_multibagger_temporal_holdout_policy(POLICY_PATH)
    assert policy.code == "production_multibagger_temporal_holdout_v1"
    assert policy.checksum_sha256 == POLICY_CHECKSUM
    assert policy.inherited_checksums == INHERITED_CHECKSUMS
    assert (
        policy.inherited_checksums["historical_universe_policy_checksum_sha256"]
        == "ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982"
    )
    assert (
        policy.primary_outcome_contract,
        policy.primary_selection_contract,
        policy.primary_evaluation_view,
        policy.primary_comparison_metric,
    ) == ("MB_3X_3Y", "TOP_20", "end_to_end", "f1")
    assert policy.allowed_split == "holdout"
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    value["challenger_development_policy_checksum_sha256"] = "0" * 64
    corrupted = tmp_path / "corrupted.json"
    corrupted.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="binding mismatch"):
        load_multibagger_temporal_holdout_policy(corrupted)
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    value["unknown"] = True
    corrupted.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="fields are not exact"):
        load_multibagger_temporal_holdout_policy(corrupted)


@pytest.mark.parametrize("checksum_field", tuple(INHERITED_CHECKSUMS))
def test_t_policy_rejects_each_corrupt_inherited_checksum(
    tmp_path: Path, checksum_field: str
) -> None:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    value[checksum_field] = "f" * 64
    corrupted = tmp_path / f"corrupt-{checksum_field}.json"
    corrupted.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="binding mismatch"):
        load_multibagger_temporal_holdout_policy(corrupted)


@pytest.mark.parametrize(
    ("selection_code", "rankable_count", "expected"),
    [
        ("TOP_5", 50, 5),
        ("TOP_10", 50, 10),
        ("TOP_20", 50, 20),
        ("TOP_1_PERCENT", 50, 1),
        ("TOP_5_PERCENT", 50, 3),
        ("TOP_10_PERCENT", 50, 5),
        ("TOP_1_PERCENT", 1, 1),
        ("TOP_20", 0, 0),
    ],
)
def test_t_reproduces_exact_fixed_selection_contracts(
    selection_code: str, rankable_count: int, expected: int
) -> None:
    assert selection_count(selection_code, rankable_count) == expected


@pytest.mark.parametrize(
    ("rankable", "selected", "actual_label", "view", "expected"),
    [
        (True, True, "positive", "end_to_end", "tp"),
        (True, True, "negative", "end_to_end", "fp"),
        (True, False, "positive", "end_to_end", "fn"),
        (True, False, "negative", "end_to_end", "tn"),
        (False, False, "positive", "end_to_end", "fn"),
        (False, False, "negative", "end_to_end", "tn"),
        (
            False,
            False,
            "positive",
            "rankable_only",
            "excluded_rankability_for_rankable_only",
        ),
        (True, True, "unmatured", "end_to_end", "excluded_unmatured"),
        (True, True, "unavailable", "end_to_end", "excluded_unavailable"),
    ],
)
def test_t_preserves_r_confusion_and_exclusion_semantics(
    rankable: bool,
    selected: bool,
    actual_label: str,
    view: str,
    expected: str,
) -> None:
    assert (
        confusion_class(
            rankable=rankable,
            selected=selected,
            actual_label=actual_label,
            evaluation_view=view,
        )
        == expected
    )


def test_t_readiness_is_database_backed_not_a_ready_string() -> None:
    policy = load_multibagger_temporal_holdout_policy(POLICY_PATH)
    result = temporal_holdout_readiness(
        None,
        policy=policy,
        s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
        activation_policy=load_historical_evaluation_activation_policy(
            ACTIVATION_POLICY_PATH, repository_root=ROOT
        ),
        split_policy=load_multibagger_temporal_split_policy(SPLIT_POLICY_PATH),
        experiment_id=UUID(int=1),
        session_calendar_run_id=UUID(int=2),
        database_available=False,
    )
    assert result == {
        "status": "environment_blocked",
        "message": "REAL PRODUCTION T: ENVIRONMENT-BLOCKED",
        "blockers": ["production_database_unavailable"],
    }


def test_fixture_creation_cannot_escalate_a_production_s2_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _mechanics_graph(experiment_evidence=PRODUCTION_EVIDENCE)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._validated_input_graph",
        lambda *_args, **_kwargs: graph,
    )

    def capture_create(_session, **kwargs):
        captured.update(kwargs)
        return _minimal_holdout_run(evidence_class=cast(str, kwargs["evidence_class"]))

    monkeypatch.setattr("inflector_data.multibagger_holdout._create_run", capture_create)
    run = create_fixture_temporal_holdout_run(
        cast(Session, object()),
        policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
        s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
        experiment_id=UUID(int=7005),
        session_calendar_run_id=UUID(int=7010),
    )
    assert captured["evidence_class"] == FIXTURE_EVIDENCE
    assert run.evidence_class == FIXTURE_EVIDENCE


def test_production_creation_requires_ready_and_passes_authority_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _mechanics_graph(experiment_evidence=PRODUCTION_EVIDENCE)
    captured: list[str] = []
    readiness = {"status": "data_blocked", "message": "REAL PRODUCTION T: DATA-BLOCKED"}
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout.temporal_holdout_readiness",
        lambda *_args, **_kwargs: readiness,
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._validated_input_graph",
        lambda *_args, **_kwargs: graph,
    )

    def capture_create(_session, **kwargs):
        captured.append(cast(str, kwargs["evidence_class"]))
        return _minimal_holdout_run(evidence_class=captured[-1])

    monkeypatch.setattr("inflector_data.multibagger_holdout._create_run", capture_create)
    parameters = {
        "policy": load_multibagger_temporal_holdout_policy(POLICY_PATH),
        "s2_policy": load_multibagger_challenger_development_policy(S2_POLICY_PATH),
        "activation_policy": load_historical_evaluation_activation_policy(
            ACTIVATION_POLICY_PATH, repository_root=ROOT
        ),
        "split_policy": load_multibagger_temporal_split_policy(SPLIT_POLICY_PATH),
        "experiment_id": UUID(int=7005),
        "session_calendar_run_id": UUID(int=7010),
    }
    with pytest.raises(HoldoutIntegrityError, match="DATA-BLOCKED"):
        create_temporal_holdout_run(cast(Session, object()), **parameters)
    assert captured == []
    readiness.update({"status": "ready", "message": "REAL PRODUCTION T: READY"})
    run = create_temporal_holdout_run(cast(Session, object()), **parameters)
    assert captured == [PRODUCTION_EVIDENCE]
    assert run.evidence_class == PRODUCTION_EVIDENCE


def test_t_run_identity_binds_immutable_evidence_class() -> None:
    policy = load_multibagger_temporal_holdout_policy(POLICY_PATH)
    graph = _mechanics_graph(experiment_evidence=PRODUCTION_EVIDENCE)
    production = _run_identity(
        policy=policy,
        graph=graph,
        evidence_class=PRODUCTION_EVIDENCE,
    )
    fixture = _run_identity(policy=policy, graph=graph, evidence_class=FIXTURE_EVIDENCE)
    assert production["evidence_class"] == PRODUCTION_EVIDENCE
    assert fixture["evidence_class"] == FIXTURE_EVIDENCE
    assert canonical_json_sha256(production) != canonical_json_sha256(fixture)


def test_summary_json_cannot_grant_production_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _minimal_holdout_run(evidence_class=FIXTURE_EVIDENCE)
    run.summary_json = {"real_evidence": True}
    graph = _mechanics_graph(experiment_evidence=PRODUCTION_EVIDENCE)
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._split_calendar_id",
        lambda *_args: UUID(int=7010),
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout.temporal_holdout_readiness",
        lambda *_args, **_kwargs: pytest.fail("fixture run attempted production readiness"),
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._validated_input_graph",
        lambda *_args, **kwargs: graph
        if kwargs["require_production"] is False
        else pytest.fail("fixture run requested production graph"),
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._validate_run_graph",
        lambda *_args, **_kwargs: None,
    )
    assert (
        _validated_run_input(
            cast(Session, object()),
            run=run,
            policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
            s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
            activation_policy=None,
            split_policy=None,
        )
        is graph
    )


def test_production_run_rechecks_full_temporal_holdout_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _minimal_holdout_run(evidence_class=PRODUCTION_EVIDENCE)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._split_calendar_id",
        lambda *_args: UUID(int=7010),
    )

    def blocked(_session, **kwargs):
        calls.append(kwargs)
        return {"status": "data_blocked", "blockers": ["calendar_unavailable"]}

    monkeypatch.setattr(
        "inflector_data.multibagger_holdout.temporal_holdout_readiness", blocked
    )
    with pytest.raises(HoldoutIntegrityError, match="readiness is no longer satisfied"):
        _validated_run_input(
            cast(Session, object()),
            run=run,
            policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
            s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
            activation_policy=load_historical_evaluation_activation_policy(
                ACTIVATION_POLICY_PATH, repository_root=ROOT
            ),
            split_policy=load_multibagger_temporal_split_policy(SPLIT_POLICY_PATH),
        )
    assert calls[0]["experiment_id"] == run.s2_experiment_run_id
    assert calls[0]["session_calendar_run_id"] == UUID(int=7010)


def test_exact_persisted_holdout_membership_and_split_checksum_are_required() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        evaluation = MultibaggerEvaluationRun(
            id=UUID(int=200),
            run_key_sha256="1" * 64,
            evaluation_policy_code="production_multibagger_evaluation_v1",
            evaluation_policy_checksum_sha256=INHERITED_CHECKSUMS[
                "multibagger_evaluation_policy_checksum_sha256"
            ],
            backtest_policy_checksum_sha256=INHERITED_CHECKSUMS["backtest_policy_checksum_sha256"],
            universe_policy_checksum_sha256=INHERITED_CHECKSUMS[
                "historical_universe_policy_checksum_sha256"
            ],
            outcome_policy_checksum_sha256=INHERITED_CHECKSUMS[
                "multibagger_outcome_policy_checksum_sha256"
            ],
            research_profile_checksum_sha256=INHERITED_CHECKSUMS[
                "research_profile_checksum_sha256"
            ],
            model_family="opportunity_score_v5",
            scoring_configuration_checksum_sha256=INHERITED_CHECKSUMS[
                "champion_scoring_configuration_checksum_sha256"
            ],
            ordered_bundle_checksums_json=[],
            selection_contracts_json=[],
            outcome_contracts_json=[],
            algorithm_versions_json={},
            inputs_json={},
            status="completed",
            summary_json={},
        )
        split_id = UUID(int=201)
        source_dates = (
            ("development", datetime(2010, 1, 31, tzinfo=UTC), UUID(int=210)),
            ("validation", datetime(2011, 1, 31, tzinfo=UTC), UUID(int=211)),
            ("holdout", datetime(2012, 1, 31, tzinfo=UTC), UUID(int=212)),
        )
        cohorts = [
            MultibaggerEvaluationCohort(
                id=cohort_id,
                multibagger_evaluation_run_id=evaluation.id,
                historical_universe_run_id=UUID(int=300 + index),
                historical_cutoff=cutoff,
                universe_run_key_sha256=f"{index + 1:064x}",
                eligible_resolved_count=1,
                unresolved_count=0,
                ambiguous_count=0,
                unsupported_count=0,
                ordered_security_checksum_sha256=f"{index + 4:064x}",
                backtest_run_ids_json=[],
                label_run_ids_json=[],
                outcome_data_cutoff=datetime(2020, 1, 31, tzinfo=UTC),
                bundle_checksum_sha256=f"{index + 7:064x}",
                rankable_count=1,
                unrankable_count=0,
                calendar_integrity_status="verified",
                status="completed",
                summary_json={},
            )
            for index, (_, cutoff, cohort_id) in enumerate(source_dates)
        ]
        projection = [
            {
                "cohort_id": str(cohort.id),
                "cutoff": cutoff.isoformat(),
                "bundle_checksum_sha256": cohort.bundle_checksum_sha256,
                "split_code": code,
            }
            for (code, cutoff, _), cohort in zip(source_dates, cohorts, strict=True)
        ]
        split_checksum = canonical_json_sha256(projection)
        split = MultibaggerTemporalSplitRun(
            id=split_id,
            run_key_sha256="f" * 64,
            split_policy_code="production_multibagger_temporal_split_v1",
            split_policy_checksum_sha256=INHERITED_CHECKSUMS[
                "temporal_split_policy_checksum_sha256"
            ],
            source_evaluation_run_id=evaluation.id,
            source_evaluation_run_key_sha256=evaluation.run_key_sha256,
            session_calendar_run_id=UUID(int=202),
            ordered_eligible_cutoffs_json=[item[1].isoformat() for item in source_dates],
            development_cutoffs_json=[source_dates[0][1].isoformat()],
            validation_cutoffs_json=[source_dates[1][1].isoformat()],
            holdout_cutoffs_json=[source_dates[2][1].isoformat()],
            split_checksum_sha256=split_checksum,
            outcome_data_cutoff=datetime(2020, 1, 31, tzinfo=UTC),
            status="completed",
            summary_json={},
        )
        assignments = [
            MultibaggerTemporalSplitCutoff(
                multibagger_temporal_split_run_id=split.id,
                multibagger_evaluation_cohort_id=cohort.id,
                historical_cutoff=cutoff,
                split_code=code,
                assignment_fingerprint_sha256=canonical_json_sha256(
                    {
                        "split_checksum_sha256": split_checksum,
                        "cohort_id": str(cohort.id),
                        "split_code": code,
                    }
                ),
            )
            for (code, cutoff, _), cohort in zip(source_dates, cohorts, strict=True)
        ]
        session.add_all((evaluation, *cohorts, split, *assignments))
        session.flush()
        sets, holdout, membership_checksum = _validate_split_membership(
            session, split=split, evaluation=evaluation
        )
        assert sets["holdout"] == {UUID(int=212)}
        assert holdout == (UUID(int=212),)
        assert membership_checksum == canonical_json_sha256([str(UUID(int=212))])
        split.split_checksum_sha256 = "0" * 64
        session.flush()
        with pytest.raises(HoldoutIntegrityError, match="assignment_fingerprint"):
            _validate_split_membership(session, split=split, evaluation=evaluation)
    engine.dispose()


def _metric(
    *,
    outcome: str,
    candidate_f1: str | None = "0.6",
    champion_f1: str | None = "0.5",
    precision: str | None = "0.6",
    champion_precision: str | None = "0.5",
    recall: str | None = "0.6",
    champion_recall: str | None = "0.5",
    rank: str | None = "7",
    champion_rank: str | None = "8",
    rankability_delta: int = 0,
    coverage_delta: int = 0,
) -> MultibaggerHoldoutMetric:
    return MultibaggerHoldoutMetric(
        id=UUID(int={"MB_2X_2Y": 1, "MB_3X_3Y": 2, "MB_5X_5Y": 3}[outcome]),
        holdout_run_id=UUID(int=10),
        subject_code="challenger",
        outcome_contract_code=outcome,
        selection_contract_code="TOP_20",
        evaluation_view="end_to_end",
        metrics_json={
            "f1": candidate_f1,
            "precision": precision,
            "recall": recall,
            "positive_median_rank": rank,
        },
        champion_metrics_json={
            "f1": champion_f1,
            "precision": champion_precision,
            "recall": champion_recall,
            "positive_median_rank": champion_rank,
        },
        delta_metrics_json={},
        rankability_delta=rankability_delta,
        coverage_delta=coverage_delta,
        selection_membership_checksum_sha256="a" * 64,
        metric_fingerprint_sha256="b" * 64,
    )


def _gate_cells(**primary_changes: object) -> dict[tuple[str, str, str], MultibaggerHoldoutMetric]:
    primary = _metric(outcome="MB_3X_3Y", **primary_changes)  # type: ignore[arg-type]
    return {
        ("MB_2X_2Y", "TOP_20", "end_to_end"): _metric(outcome="MB_2X_2Y"),
        ("MB_3X_3Y", "TOP_20", "end_to_end"): primary,
        ("MB_5X_5Y", "TOP_20", "end_to_end"): _metric(outcome="MB_5X_5Y"),
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"candidate_f1": "0.5"}, "primary_f1_not_strictly_greater"),
        ({"precision": "0.4"}, "primary_precision_guardrail_failed"),
        ({"recall": "0.4"}, "primary_recall_guardrail_failed"),
        ({"rank": "9"}, "positive_median_rank_guardrail_failed"),
        ({"candidate_f1": None}, "insufficient_holdout_metric_support"),
        ({"rankability_delta": 1}, "rankability_or_coverage_changed"),
        ({"coverage_delta": 1}, "rankability_or_coverage_changed"),
    ],
)
def test_predeclared_holdout_decision_gate_fails_closed(
    changes: dict[str, object], reason: str
) -> None:
    passed, reasons = holdout_decision_gate(_gate_cells(**changes))
    assert passed is False
    assert reason in reasons


def test_predeclared_holdout_decision_gate_requires_both_secondary_guardrails() -> None:
    cells = _gate_cells()
    passed, reasons = holdout_decision_gate(cells)
    assert passed is True
    assert reasons == ()
    cells[("MB_5X_5Y", "TOP_20", "end_to_end")].metrics_json["f1"] = "0.4"
    assert holdout_decision_gate(cells) == (False, ("mb_5x_5y_f1_guardrail_failed",))


@pytest.mark.parametrize(
    ("factory", "label"),
    [
        (
            lambda: MultibaggerHoldoutScore(
                id=UUID(int=1),
                holdout_run_id=UUID(int=2),
                subject_code="champion",
                candidate_id=None,
                source_evaluation_cohort_id=UUID(int=3),
                source_prediction_row_id=UUID(int=4),
                security_id=UUID(int=5),
                historical_symbol="FACT",
                source_score_snapshot_id=UUID(int=6),
                source_prediction_fingerprint_sha256="1" * 64,
                source_snapshot_fingerprint_sha256="2" * 64,
                component_state_checksum_sha256="3" * 64,
                score=Decimal("75"),
                rankable=True,
                unranked_reason=None,
                display_order=1,
                dense_score_rank=1,
                rankable_count=1,
                cohort_size=1,
                selection_flags_json={
                    code: True
                    for code in (
                        "TOP_5",
                        "TOP_10",
                        "TOP_20",
                        "TOP_1_PERCENT",
                        "TOP_5_PERCENT",
                        "TOP_10_PERCENT",
                    )
                },
                selection_boundary_ties_json={},
                score_fingerprint_sha256="4" * 64,
            ),
            "holdout score",
        ),
        (
            lambda: MultibaggerHoldoutEvaluationRow(
                id=UUID(int=7),
                holdout_run_id=UUID(int=2),
                holdout_score_id=UUID(int=1),
                source_confusion_row_id=UUID(int=8),
                outcome_contract_code="MB_3X_3Y",
                selection_contract_code="TOP_20",
                evaluation_view="end_to_end",
                selected=True,
                actual_label="positive",
                confusion_class="tp",
                row_fingerprint_sha256="5" * 64,
            ),
            "holdout evaluation row",
        ),
        (
            lambda: _metric(outcome="MB_3X_3Y"),
            "holdout metric",
        ),
        (
            lambda: MultibaggerHoldoutDecision(
                id=UUID(int=9),
                holdout_run_id=UUID(int=2),
                decision_code="CHALLENGER_PASSES_HOLDOUT",
                decision_rule="conservative_holdout_guardrails_v1",
                gate_json={"passed": True},
                production_promotion_performed=False,
                decision_fingerprint_sha256="6" * 64,
            ),
            "holdout decision",
        ),
    ],
)
def test_append_only_reuse_requires_complete_projection(factory, label: str) -> None:
    row = factory()
    expected = {
        key: value
        for key, value in vars(row).items()
        if key != "_sa_instance_state" and key not in {"id", "created_at"}
    }
    _validate_projection(row, expected, label)
    field = next(iter(expected))
    expected[field] = UUID(int=999) if isinstance(expected[field], UUID) else "tampered"
    with pytest.raises(HoldoutIntegrityError, match="immutable projection"):
        _validate_projection(row, expected, label)


def test_score_flush_precedes_outcome_query_and_query_is_holdout_only() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    statements: list[str] = []
    parameters: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, params, _context, _many) -> None:
        statements.append(statement.lower())
        parameters.append(str(params))

    holdout_id = UUID(int=100)
    development_id = UUID(int=101)
    validation_id = UUID(int=102)
    with sessionmaker(bind=engine)() as session:
        session.add(
            MultibaggerHoldoutScore(
                holdout_run_id=UUID(int=103),
                subject_code="champion",
                candidate_id=None,
                source_evaluation_cohort_id=holdout_id,
                source_prediction_row_id=UUID(int=104),
                security_id=UUID(int=105),
                historical_symbol="HOLDOUT",
                source_score_snapshot_id=None,
                source_prediction_fingerprint_sha256="1" * 64,
                source_snapshot_fingerprint_sha256=None,
                component_state_checksum_sha256="2" * 64,
                score=None,
                rankable=False,
                unranked_reason="issuer_unavailable",
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
                selection_boundary_ties_json={},
                score_fingerprint_sha256="3" * 64,
            )
        )
        assert (
            _load_holdout_outcomes_after_score_flush(
                session,
                holdout_cohort_ids=(holdout_id,),
                forbidden_cohort_ids={development_id, validation_id},
            )
            == ()
        )
    insert_at = next(
        index
        for index, sql in enumerate(statements)
        if "insert into multibagger_holdout_scores" in sql
    )
    outcome_at = next(
        index for index, sql in enumerate(statements) if "from multibagger_confusion_rows" in sql
    )
    assert insert_at < outcome_at
    assert holdout_id.hex in parameters[outcome_at]
    assert development_id.hex not in parameters[outcome_at]
    assert validation_id.hex not in parameters[outcome_at]
    with sessionmaker(bind=engine)() as session:
        with pytest.raises(HoldoutIntegrityError, match="isolated"):
            _load_holdout_outcomes_after_score_flush(
                session,
                holdout_cohort_ids=(holdout_id,),
                forbidden_cohort_ids={holdout_id},
            )
    engine.dispose()


def test_holdout_open_is_durable_before_outcome_and_resume_is_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "durable-holdout.sqlite3"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    run_id = UUID(int=7200)
    cohort_id = UUID(int=7003)
    prediction_id = UUID(int=7201)
    score_fingerprint = "9" * 64
    aggregate = canonical_json_sha256([score_fingerprint])
    opened_at = datetime(2026, 10, 17, 1, 2, 3, tzinfo=UTC)
    with factory() as session:
        session.add(
            _minimal_holdout_run(
                run_id=run_id,
                status="scores_frozen",
                score_checksum=aggregate,
            )
        )
        session.add(
            MultibaggerHoldoutScore(
                id=UUID(int=7202),
                holdout_run_id=run_id,
                subject_code="champion",
                candidate_id=None,
                source_evaluation_cohort_id=cohort_id,
                source_prediction_row_id=prediction_id,
                security_id=UUID(int=7203),
                historical_symbol="DURABLE",
                source_score_snapshot_id=None,
                source_prediction_fingerprint_sha256="1" * 64,
                source_snapshot_fingerprint_sha256=None,
                component_state_checksum_sha256="2" * 64,
                score=None,
                rankable=False,
                unranked_reason="issuer_unavailable",
                display_order=None,
                dense_score_rank=None,
                rankable_count=0,
                cohort_size=1,
                selection_flags_json={code: False for code in SELECTION_CONTRACTS},
                selection_boundary_ties_json={code: False for code in SELECTION_CONTRACTS},
                score_fingerprint_sha256=score_fingerprint,
            )
        )
        session.commit()

    graph = _mechanics_graph()
    prediction = SimpleNamespace(id=prediction_id)
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._validated_run_input",
        lambda *_args, **_kwargs: graph,
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._holdout_predictions",
        lambda *_args, **_kwargs: (prediction,),
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._revalidate_frozen_holdout_scores",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._non_holdout_cohort_ids",
        lambda *_args, **_kwargs: set(),
    )

    first_outcome_observations: list[tuple[str, datetime | None, str | None]] = []

    def fail_after_durable_open(*_args, **_kwargs):
        with factory() as independent:
            persisted = independent.get(MultibaggerHoldoutEvaluationRun, run_id)
            assert persisted is not None
            first_outcome_observations.append(
                (
                    persisted.status,
                    persisted.holdout_opened_at,
                    persisted.score_selection_checksum_sha256,
                )
            )
        raise RuntimeError("forced after first outcome access")

    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._load_holdout_outcomes_after_score_flush",
        fail_after_durable_open,
    )
    with factory() as session:
        with pytest.raises(RuntimeError, match="forced after first outcome"):
            evaluate_temporal_holdout(
                session,
                policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
                s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
                holdout_run_id=run_id,
                opened_at=opened_at,
            )
        session.rollback()
    assert first_outcome_observations == [
        ("holdout_opened", opened_at.replace(tzinfo=None), aggregate)
    ]

    with factory() as session:
        persisted = session.get(MultibaggerHoldoutEvaluationRun, run_id)
        assert persisted is not None
        assert persisted.status == "holdout_opened"
        assert persisted.holdout_opened_at == opened_at.replace(tzinfo=None)
        with pytest.raises(HoldoutIntegrityError, match="immutable after opening"):
            freeze_temporal_holdout_scores(
                session,
                policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
                s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
                holdout_run_id=run_id,
            )
        with pytest.raises(HoldoutIntegrityError, match="timestamp is immutable"):
            evaluate_temporal_holdout(
                session,
                policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
                s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
                holdout_run_id=run_id,
                opened_at=opened_at + timedelta(seconds=1),
            )

    with factory() as session:
        score = session.get(MultibaggerHoldoutScore, UUID(int=7202))
        assert score is not None
        score.score_fingerprint_sha256 = "8" * 64
        session.commit()
    with factory() as session:
        with pytest.raises(HoldoutIntegrityError, match="score set checksum mismatch"):
            evaluate_temporal_holdout(
                session,
                policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
                s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
                holdout_run_id=run_id,
                opened_at=opened_at,
            )
    with factory() as session:
        score = session.get(MultibaggerHoldoutScore, UUID(int=7202))
        assert score is not None
        score.score_fingerprint_sha256 = score_fingerprint
        session.commit()

    facts: list[MultibaggerConfusionRow] = []
    for index, outcome in enumerate(("MB_2X_2Y", "MB_3X_3Y", "MB_5X_5Y")):
        source_projection: dict[str, object] = {
            "multibagger_outcome_label_id": UUID(int=7250 + index),
            "outcome_contract_code": outcome,
            "selection_contract_code": "TOP_5",
            "evaluation_view": "end_to_end",
            "selected": False,
            "actual_label": "positive",
            "confusion_class": "fn",
            "threshold_hit_trading_date": None,
            "endpoint_return": Decimal("1"),
            "peak_price_multiple": Decimal("3"),
            "maximum_drawdown": Decimal("-0.1"),
            "label_provenance_json": {},
        }
        facts.append(
            MultibaggerConfusionRow(
                id=UUID(int=7260 + index),
                multibagger_prediction_row_id=prediction_id,
                **source_projection,
                confusion_fingerprint_sha256=canonical_json_sha256(
                    {
                        key: str(value) if isinstance(value, (UUID, Decimal)) else value
                        for key, value in source_projection.items()
                    }
                ),
            )
        )
    monkeypatch.setattr(
        "inflector_data.multibagger_holdout._load_holdout_outcomes_after_score_flush",
        lambda *_args, **_kwargs: tuple(facts),
    )
    with factory() as session:
        decision = evaluate_temporal_holdout(
            session,
            policy=load_multibagger_temporal_holdout_policy(POLICY_PATH),
            s2_policy=load_multibagger_challenger_development_policy(S2_POLICY_PATH),
            holdout_run_id=run_id,
            opened_at=opened_at,
        )
        assert decision.decision_code == "CHAMPION_RETAINED_NO_CHALLENGER"
    with factory() as session:
        completed = session.get(MultibaggerHoldoutEvaluationRun, run_id)
        assert completed is not None
        assert completed.status == "completed"
        assert completed.holdout_opened_at == opened_at.replace(tzinfo=None)
    engine.dispose()


def test_t_and_s2_database_constraints_preserve_holdout_ownership() -> None:
    run_constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in cast(Table, MultibaggerHoldoutEvaluationRun.__table__).constraints
        if isinstance(constraint, CheckConstraint)
    }
    score_constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in cast(Table, MultibaggerHoldoutScore.__table__).constraints
        if isinstance(constraint, CheckConstraint)
    }
    s2_constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in cast(
            Table, Base.metadata.tables["multibagger_challenger_scores"]
        ).constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "holdout" not in s2_constraints["ck_multibagger_challenger_score_split"]
    assert "rankable" in score_constraints["ck_multibagger_holdout_score_rankability"]
    assert "production_authoritative" in run_constraints["ck_multibagger_holdout_evidence_class"]
    assert "fixture_only" in run_constraints["ck_multibagger_holdout_evidence_class"]


def test_source_r_confusion_tampering_is_rejected() -> None:
    score = MultibaggerHoldoutScore(
        id=UUID(int=7300),
        holdout_run_id=UUID(int=7301),
        subject_code="champion",
        candidate_id=None,
        source_evaluation_cohort_id=UUID(int=7302),
        source_prediction_row_id=UUID(int=7303),
        security_id=UUID(int=7304),
        historical_symbol="SOURCE",
        source_score_snapshot_id=None,
        source_prediction_fingerprint_sha256="1" * 64,
        source_snapshot_fingerprint_sha256=None,
        component_state_checksum_sha256="2" * 64,
        score=None,
        rankable=False,
        unranked_reason="issuer_unavailable",
        display_order=None,
        dense_score_rank=None,
        rankable_count=0,
        cohort_size=1,
        selection_flags_json={code: False for code in SELECTION_CONTRACTS},
        selection_boundary_ties_json={code: False for code in SELECTION_CONTRACTS},
        score_fingerprint_sha256="3" * 64,
    )
    facts: list[MultibaggerConfusionRow] = []
    for index, outcome in enumerate(("MB_2X_2Y", "MB_3X_3Y", "MB_5X_5Y")):
        source_projection: dict[str, object] = {
            "multibagger_outcome_label_id": UUID(int=7305 + index),
            "outcome_contract_code": outcome,
            "selection_contract_code": "TOP_5",
            "evaluation_view": "end_to_end",
            "selected": False,
            "actual_label": "positive",
            "confusion_class": "fn",
            "threshold_hit_trading_date": None,
            "endpoint_return": Decimal("1"),
            "peak_price_multiple": Decimal("2"),
            "maximum_drawdown": Decimal("-0.1"),
            "label_provenance_json": {},
        }
        facts.append(
            MultibaggerConfusionRow(
                id=UUID(int=7310 + index),
                multibagger_prediction_row_id=score.source_prediction_row_id,
                **source_projection,
                confusion_fingerprint_sha256=canonical_json_sha256(
                    {
                        key: str(value) if isinstance(value, (UUID, Decimal)) else value
                        for key, value in source_projection.items()
                    }
                ),
            )
        )
    run = _minimal_holdout_run(run_id=score.holdout_run_id)
    assert len(_expected_evaluation_row_values(run=run, scores=(score,), facts=facts)) == 36
    facts[0].actual_label = "negative"
    with pytest.raises(HoldoutIntegrityError, match="source R confusion fingerprint"):
        _expected_evaluation_row_values(run=run, scores=(score,), facts=facts)


@pytest.mark.parametrize(
    "tamper",
    (
        "evaluation_label",
        "evaluation_fingerprint",
        "missing_evaluation",
        "extra_evaluation",
        "metric_value",
        "metric_fingerprint",
        "missing_metric",
        "decision_gate_self_rehashed",
    ),
)
def test_completed_graph_rejects_derived_fact_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    database = tmp_path / f"terminal-{tamper}.sqlite3"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    opened_at = datetime(2026, 10, 17, tzinfo=UTC)
    graph = _mechanics_graph()
    run = _minimal_holdout_run(
        run_id=UUID(int=7400),
        status="completed",
        score_checksum=canonical_json_sha256(["4" * 64]),
        opened_at=opened_at,
        completed_at=opened_at,
    )
    run.summary_json = {
        "real_evidence": False,
        "decision": "CHAMPION_RETAINED_NO_CHALLENGER",
        "holdout_outcomes_read": True,
        "production_promotion_performed": False,
    }
    score = MultibaggerHoldoutScore(
        id=UUID(int=7401),
        holdout_run_id=run.id,
        subject_code="champion",
        candidate_id=None,
        source_evaluation_cohort_id=UUID(int=7003),
        source_prediction_row_id=UUID(int=7402),
        security_id=UUID(int=7403),
        historical_symbol="TERMINAL",
        source_score_snapshot_id=None,
        source_prediction_fingerprint_sha256="1" * 64,
        source_snapshot_fingerprint_sha256=None,
        component_state_checksum_sha256="2" * 64,
        score=None,
        rankable=False,
        unranked_reason="issuer_unavailable",
        display_order=None,
        dense_score_rank=None,
        rankable_count=0,
        cohort_size=1,
        selection_flags_json={code: False for code in SELECTION_CONTRACTS},
        selection_boundary_ties_json={code: False for code in SELECTION_CONTRACTS},
        score_fingerprint_sha256="4" * 64,
    )
    evaluation_values: dict[str, object] = {
        "holdout_run_id": run.id,
        "holdout_score_id": score.id,
        "source_confusion_row_id": UUID(int=7404),
        "outcome_contract_code": "MB_2X_2Y",
        "selection_contract_code": "TOP_5",
        "evaluation_view": "end_to_end",
        "selected": False,
        "actual_label": "positive",
        "confusion_class": "fn",
        "row_fingerprint_sha256": "5" * 64,
    }
    evaluation_row = MultibaggerHoldoutEvaluationRow(id=UUID(int=7405), **evaluation_values)
    metric_values: dict[str, object] = {
        "holdout_run_id": run.id,
        "subject_code": "champion",
        "outcome_contract_code": "MB_2X_2Y",
        "selection_contract_code": "TOP_5",
        "evaluation_view": "end_to_end",
        "metrics_json": {"tp": 0, "fp": 0, "fn": 1, "tn": 0, "f1": None},
        "champion_metrics_json": None,
        "delta_metrics_json": None,
        "rankability_delta": 0,
        "coverage_delta": 0,
        "selection_membership_checksum_sha256": "6" * 64,
        "metric_fingerprint_sha256": "7" * 64,
    }
    metric = MultibaggerHoldoutMetric(id=UUID(int=7406), **metric_values)
    decision_values = _expected_decision_values(run=run, metrics=(metric,))
    decision = MultibaggerHoldoutDecision(id=UUID(int=7407), **decision_values)
    with factory() as session:
        session.add_all((run, score, evaluation_row, metric, decision))
        session.commit()
        monkeypatch.setattr(
            "inflector_data.multibagger_holdout._holdout_predictions",
            lambda *_args, **_kwargs: (SimpleNamespace(id=score.source_prediction_row_id),),
        )
        monkeypatch.setattr(
            "inflector_data.multibagger_holdout._revalidate_frozen_holdout_scores",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(
            "inflector_data.multibagger_holdout._non_holdout_cohort_ids",
            lambda *_args, **_kwargs: set(),
        )
        monkeypatch.setattr(
            "inflector_data.multibagger_holdout._load_holdout_outcomes_after_score_flush",
            lambda *_args, **_kwargs: (),
        )
        monkeypatch.setattr(
            "inflector_data.multibagger_holdout._expected_evaluation_row_values",
            lambda **_kwargs: (evaluation_values,),
        )
        monkeypatch.setattr(
            "inflector_data.multibagger_holdout._expected_metric_values",
            lambda *_args, **_kwargs: (metric_values,),
        )
        _validate_completed_holdout_graph(session, run=run, graph=graph)

        if tamper == "evaluation_label":
            evaluation_row.actual_label = "negative"
        elif tamper == "evaluation_fingerprint":
            evaluation_row.row_fingerprint_sha256 = "8" * 64
        elif tamper == "missing_evaluation":
            session.delete(evaluation_row)
        elif tamper == "extra_evaluation":
            session.add(
                MultibaggerHoldoutEvaluationRow(
                    id=UUID(int=7410),
                    **{
                        **evaluation_values,
                        "outcome_contract_code": "MB_3X_3Y",
                        "row_fingerprint_sha256": "8" * 64,
                    },
                )
            )
        elif tamper == "metric_value":
            metric.metrics_json = {**metric.metrics_json, "f1": "1"}
        elif tamper == "metric_fingerprint":
            metric.metric_fingerprint_sha256 = "8" * 64
        elif tamper == "missing_metric":
            session.delete(metric)
        else:
            decision.gate_json = {"passed": False, "production_promotion": False}
            decision.decision_fingerprint_sha256 = canonical_json_sha256(
                {
                    "holdout_run_key_sha256": run.run_key_sha256,
                    "decision_code": decision.decision_code,
                    "decision_rule": decision.decision_rule,
                    "gate": decision.gate_json,
                    "production_promotion_performed": False,
                }
            )
        session.flush()
        with pytest.raises(HoldoutIntegrityError, match="conflicts|requires exactly"):
            _validate_completed_holdout_graph(session, run=run, graph=graph)
    engine.dispose()


def test_cli_contains_only_evaluation_holdout_commands() -> None:
    cli = (ROOT / "packages/data/inflector_data/backtest_cli.py").read_text(encoding="utf-8")
    for command_name in (
        "temporal-holdout-readiness",
        "create-temporal-holdout-run",
        "freeze-temporal-holdout-scores",
        "evaluate-temporal-holdout",
        "summarize-temporal-holdout",
        "inspect-temporal-holdout",
    ):
        assert command_name in cli
    for forbidden in (
        "tune-holdout",
        "optimize-holdout",
        "search-holdout",
        "train-holdout",
        "promote-holdout",
        "evaluate-challenger-holdout",
    ):
        assert forbidden not in cli


def test_champion_asset_and_upstream_policies_remain_accepted() -> None:
    expected = {
        "config/scoring/production_opportunity_v5.json": (
            "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
        ),
        "config/backtest/production_multibagger_challenger_development_v1.json": (
            "17642f0e05640464b0166bc82740882e6fda5c99e62ca555c19d7be30ebe85e0"
        ),
    }
    for path, checksum in expected.items():
        value = json.loads((ROOT / path).read_text(encoding="utf-8"))
        assert canonical_json_sha256(value) == checksum


def test_migration_0027_adds_only_t_tables_and_downgrades(tmp_path: Path) -> None:
    database = tmp_path / "t-migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "20261007_0026")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "20261008_0027")
    assert set(inspect(engine).get_table_names()) - before == {
        "multibagger_holdout_evaluation_runs",
        "multibagger_holdout_scores",
        "multibagger_holdout_evaluation_rows",
        "multibagger_holdout_metrics",
        "multibagger_holdout_decisions",
    }
    run_columns = {
        item["name"]
        for item in inspect(engine).get_columns("multibagger_holdout_evaluation_runs")
    }
    assert "evidence_class" in run_columns
    run_checks = {
        item["name"]: item["sqltext"]
        for item in inspect(engine).get_check_constraints(
            "multibagger_holdout_evaluation_runs"
        )
    }
    assert "production_authoritative" in run_checks["ck_multibagger_holdout_evidence_class"]
    assert "fixture_only" in run_checks["ck_multibagger_holdout_evidence_class"]
    command.downgrade(config, "20261007_0026")
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()
