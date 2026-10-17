"""Production T one-way evaluation of the untouched S1 temporal holdout."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, raiseload

from inflector_data.historical_activation_policy import (
    HistoricalEvaluationActivationPolicy,
    MultibaggerTemporalSplitPolicy,
)
from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.multibagger_challenger import (
    ALGORITHM_VERSION,
    CHAMPION_CHECKSUM,
    ChallengerIntegrityError,
    _candidate_score_for_prediction,
    _metric_cells,
    _sealed_candidate_set_checksum,
    _selection_projection,
    _validate_prediction_fingerprint,
    _weights_from_candidate,
    challenger_readiness,
    confusion_class,
    rank_candidate_scores,
    select_validation_challenger,
)
from inflector_data.multibagger_challenger_policy import (
    EVALUATION_VIEWS,
    OUTCOME_CONTRACTS,
    SELECTION_CONTRACTS,
    MultibaggerChallengerDevelopmentPolicy,
)
from inflector_data.multibagger_holdout_policy import MultibaggerTemporalHoldoutPolicy
from inflector_database.models import (
    BacktestObservation,
    HistoricalSessionCalendarRun,
    MultibaggerChallengerCandidate,
    MultibaggerChallengerEvaluationRow,
    MultibaggerChallengerExperimentRun,
    MultibaggerChallengerFreeze,
    MultibaggerChallengerMetric,
    MultibaggerChallengerScore,
    MultibaggerConfusionRow,
    MultibaggerErrorAnalysisRun,
    MultibaggerEvaluationCohort,
    MultibaggerEvaluationRun,
    MultibaggerHoldoutDecision,
    MultibaggerHoldoutEvaluationRow,
    MultibaggerHoldoutEvaluationRun,
    MultibaggerHoldoutMetric,
    MultibaggerHoldoutScore,
    MultibaggerPredictionRow,
    MultibaggerTemporalSplitCutoff,
    MultibaggerTemporalSplitRun,
    ScoreSnapshot,
    ScoringConfiguration,
)
from inflector_database.score_repository import ScoreSnapshotIntegrityError, ScoreSnapshotRepository

T_POLICY_CODE = "production_multibagger_temporal_holdout_v1"
T_POLICY_CHECKSUM = "309547a20c01abcece498c76727abf032d643efde6207dab2667519bc28ab45b"
S2_POLICY_CODE = "production_multibagger_challenger_development_v1"
S2_POLICY_CHECKSUM = "17642f0e05640464b0166bc82740882e6fda5c99e62ca555c19d7be30ebe85e0"
PRODUCTION_EVIDENCE = "production_authoritative"
FIXTURE_EVIDENCE = "fixture_only"


class HoldoutIntegrityError(RuntimeError):
    """An immutable Production T or upstream binding is inconsistent."""


@dataclass(frozen=True, slots=True)
class HoldoutInputGraph:
    experiment: MultibaggerChallengerExperimentRun
    split: MultibaggerTemporalSplitRun
    evaluation: MultibaggerEvaluationRun
    holdout_cohort_ids: tuple[UUID, ...]
    holdout_membership_checksum_sha256: str
    terminal_mode: str
    freeze: MultibaggerChallengerFreeze | None
    candidate: MultibaggerChallengerCandidate | None


def temporal_holdout_readiness(
    session: Session | None,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    experiment_id: UUID,
    session_calendar_run_id: UUID,
    database_available: bool = True,
) -> dict[str, object]:
    """Validate one coherent DB-backed S1/R/S2 chain without opening outcomes."""

    if not database_available or session is None:
        return {
            "status": "environment_blocked",
            "message": "REAL PRODUCTION T: ENVIRONMENT-BLOCKED",
            "blockers": ["production_database_unavailable"],
        }
    blockers: list[str] = []
    if policy.code != T_POLICY_CODE or policy.checksum_sha256 != T_POLICY_CHECKSUM:
        blockers.append("accepted_t_policy_unavailable")
    if (
        s2_policy.code != S2_POLICY_CODE
        or s2_policy.checksum_sha256 != S2_POLICY_CHECKSUM
        or policy.inherited_checksums["challenger_development_policy_checksum_sha256"]
        != S2_POLICY_CHECKSUM
    ):
        blockers.append("accepted_s2_policy_unavailable")
    experiment = session.get(MultibaggerChallengerExperimentRun, experiment_id)
    if experiment is None:
        blockers.append("s2_experiment_unavailable")
    else:
        split = session.get(MultibaggerTemporalSplitRun, experiment.temporal_split_run_id)
        if split is None:
            blockers.append("s1_temporal_split_unavailable")
        else:
            s2 = challenger_readiness(
                session,
                policy=s2_policy,
                activation_policy=activation_policy,
                split_policy=split_policy,
                source_evaluation_run_id=experiment.source_evaluation_run_id,
                session_calendar_run_id=session_calendar_run_id,
                temporal_split_run_id=split.id,
                error_analysis_run_id=experiment.error_analysis_run_id,
            )
            if s2.get("status") != "ready":
                blockers.append("accepted_s1_s2_readiness_unavailable")
            try:
                _validated_input_graph(
                    session,
                    policy=policy,
                    s2_policy=s2_policy,
                    experiment_id=experiment_id,
                    session_calendar_run_id=session_calendar_run_id,
                    require_production=True,
                )
            except HoldoutIntegrityError as error:
                blockers.append(str(error))
    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return {
            "status": "data_blocked",
            "message": "REAL PRODUCTION T: DATA-BLOCKED",
            "blockers": blockers,
        }
    return {"status": "ready", "message": "REAL PRODUCTION T: READY", "blockers": []}


def create_temporal_holdout_run(
    session: Session,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    experiment_id: UUID,
    session_calendar_run_id: UUID,
) -> MultibaggerHoldoutEvaluationRun:
    readiness = temporal_holdout_readiness(
        session,
        policy=policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
        experiment_id=experiment_id,
        session_calendar_run_id=session_calendar_run_id,
    )
    if readiness["status"] != "ready":
        raise HoldoutIntegrityError(str(readiness["message"]))
    graph = _validated_input_graph(
        session,
        policy=policy,
        s2_policy=s2_policy,
        experiment_id=experiment_id,
        session_calendar_run_id=session_calendar_run_id,
        require_production=True,
    )
    return _create_run(
        session,
        policy=policy,
        graph=graph,
        evidence_class=PRODUCTION_EVIDENCE,
    )


def create_fixture_temporal_holdout_run(
    session: Session,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    experiment_id: UUID,
    session_calendar_run_id: UUID,
) -> MultibaggerHoldoutEvaluationRun:
    """Explicit mechanics-only path; it can never represent real T authority."""

    graph = _validated_input_graph(
        session,
        policy=policy,
        s2_policy=s2_policy,
        experiment_id=experiment_id,
        session_calendar_run_id=session_calendar_run_id,
        require_production=False,
    )
    return _create_run(
        session,
        policy=policy,
        graph=graph,
        evidence_class=FIXTURE_EVIDENCE,
    )


def _create_run(
    session: Session,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    graph: HoldoutInputGraph,
    evidence_class: str,
) -> MultibaggerHoldoutEvaluationRun:
    if evidence_class not in {PRODUCTION_EVIDENCE, FIXTURE_EVIDENCE}:
        raise HoldoutIntegrityError("unsupported T evidence class")
    experiment = graph.experiment
    freeze = graph.freeze
    candidate = graph.candidate
    identity = _run_identity(policy=policy, graph=graph, evidence_class=evidence_class)
    run_key = canonical_json_sha256(identity)
    existing = session.scalar(
        select(MultibaggerHoldoutEvaluationRun).where(
            MultibaggerHoldoutEvaluationRun.run_key_sha256 == run_key
        )
    )
    values: dict[str, object] = {
        "run_key_sha256": run_key,
        "policy_code": policy.code,
        "policy_checksum_sha256": policy.checksum_sha256,
        "source_evaluation_run_id": graph.evaluation.id,
        "source_evaluation_run_key_sha256": graph.evaluation.run_key_sha256,
        "temporal_split_run_id": graph.split.id,
        "temporal_split_checksum_sha256": graph.split.split_checksum_sha256,
        "holdout_cohort_ids_json": [str(item) for item in graph.holdout_cohort_ids],
        "holdout_membership_checksum_sha256": graph.holdout_membership_checksum_sha256,
        "champion_scoring_configuration_id": experiment.champion_scoring_configuration_id,
        "champion_scoring_checksum_sha256": CHAMPION_CHECKSUM,
        "s2_experiment_run_id": experiment.id,
        "s2_experiment_run_key_sha256": experiment.run_key_sha256,
        "evidence_class": evidence_class,
        "terminal_mode": graph.terminal_mode,
        "challenger_freeze_id": freeze.id if freeze else None,
        "challenger_freeze_key_sha256": freeze.freeze_key_sha256 if freeze else None,
        "challenger_candidate_id": candidate.id if candidate else None,
        "challenger_candidate_key_sha256": candidate.candidate_key_sha256 if candidate else None,
        "challenger_candidate_checksum_sha256": (
            candidate.candidate_checksum_sha256 if candidate else None
        ),
        "primary_objective_json": _primary_objective(policy),
        "outcome_data_cutoff": graph.split.outcome_data_cutoff,
    }
    if existing is not None:
        _validate_projection(existing, values, "holdout run identity")
        return existing
    run = MultibaggerHoldoutEvaluationRun(
        **values,
        status="draft",
        summary_json={"real_evidence": evidence_class == PRODUCTION_EVIDENCE},
    )
    session.add(run)
    session.flush()
    return run


def freeze_temporal_holdout_scores(
    session: Session,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    holdout_run_id: UUID,
    activation_policy: HistoricalEvaluationActivationPolicy | None = None,
    split_policy: MultibaggerTemporalSplitPolicy | None = None,
) -> dict[str, object]:
    """Persist every outcome-free score/rank/selection projection."""

    run = _holdout_run(session, holdout_run_id, policy)
    if run.status not in {"draft", "scores_frozen"} or run.holdout_opened_at is not None:
        raise HoldoutIntegrityError("holdout score state is immutable after opening")
    graph = _validated_run_input(
        session,
        run=run,
        policy=policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
    )
    predictions = _holdout_predictions(session, graph.holdout_cohort_ids)
    candidate = graph.candidate
    subjects = ("champion", "challenger") if candidate is not None else ("champion",)
    expected_rows: list[MultibaggerHoldoutScore] = []
    for subject in subjects:
        candidate_weights = _weights_from_candidate(candidate) if candidate is not None else None
        provisional: dict[UUID, list[tuple[UUID, str, UUID, Decimal | None, bool, str | None]]] = {}
        component_checksums: dict[UUID, str] = {}
        for prediction in predictions:
            champion_score, component_checksum = _champion_score_for_prediction(
                session, experiment=graph.experiment, prediction=prediction
            )
            score = champion_score
            if subject == "challenger":
                if candidate_weights is None:
                    raise HoldoutIntegrityError("challenger definition is unavailable")
                try:
                    score, challenger_checksum = _candidate_score_for_prediction(
                        session,
                        experiment=graph.experiment,
                        prediction=prediction,
                        weights=candidate_weights,
                    )
                except ChallengerIntegrityError as error:
                    raise HoldoutIntegrityError(str(error)) from error
                if challenger_checksum != component_checksum:
                    raise HoldoutIntegrityError("champion and challenger component state disagree")
            component_checksums[prediction.id] = component_checksum
            provisional.setdefault(prediction.multibagger_evaluation_cohort_id, []).append(
                (
                    prediction.id,
                    prediction.historical_symbol,
                    prediction.security_id,
                    score,
                    prediction.rankable,
                    prediction.unranked_reason,
                )
            )
        for cohort_id, raw in provisional.items():
            ranked = rank_candidate_scores(raw)
            if subject == "champion":
                by_id = {item.id: item for item in predictions}
                if any(
                    item.display_order != by_id[item.prediction_id].display_order
                    or item.dense_score_rank != by_id[item.prediction_id].dense_score_rank
                    for item in ranked
                ):
                    raise HoldoutIntegrityError("frozen champion ranking conflicts with R")
            rankable_count = sum(item.rankable for item in ranked)
            flags, ties = _selection_projection(ranked, rankable_count=rankable_count)
            prediction_by_id = {item.id: item for item in predictions}
            for item in ranked:
                prediction = prediction_by_id[item.prediction_id]
                values: dict[str, object] = {
                    "holdout_run_id": run.id,
                    "subject_code": subject,
                    "candidate_id": candidate.id if subject == "challenger" and candidate else None,
                    "source_evaluation_cohort_id": cohort_id,
                    "source_prediction_row_id": prediction.id,
                    "security_id": prediction.security_id,
                    "historical_symbol": prediction.historical_symbol,
                    "source_score_snapshot_id": prediction.score_snapshot_id,
                    "source_prediction_fingerprint_sha256": (
                        prediction.prediction_fingerprint_sha256
                    ),
                    "source_snapshot_fingerprint_sha256": prediction.snapshot_fingerprint_sha256,
                    "component_state_checksum_sha256": component_checksums[prediction.id],
                    "score": item.score,
                    "rankable": item.rankable,
                    "unranked_reason": item.unranked_reason,
                    "display_order": item.display_order,
                    "dense_score_rank": item.dense_score_rank,
                    "rankable_count": rankable_count,
                    "cohort_size": len(ranked),
                    "selection_flags_json": flags[item.prediction_id],
                    "selection_boundary_ties_json": ties,
                }
                fingerprint = canonical_json_sha256(
                    {
                        "holdout_run_key_sha256": run.run_key_sha256,
                        "subject_code": subject,
                        "candidate_key_sha256": candidate.candidate_key_sha256
                        if subject == "challenger" and candidate
                        else None,
                        "source_prediction_fingerprint_sha256": (
                            prediction.prediction_fingerprint_sha256
                        ),
                        "source_snapshot_fingerprint_sha256": (
                            prediction.snapshot_fingerprint_sha256
                        ),
                        "component_state_checksum_sha256": component_checksums[prediction.id],
                        "projection": _jsonable(values),
                    }
                )
                values["score_fingerprint_sha256"] = fingerprint
                existing = session.scalar(
                    select(MultibaggerHoldoutScore).where(
                        MultibaggerHoldoutScore.holdout_run_id == run.id,
                        MultibaggerHoldoutScore.subject_code == subject,
                        MultibaggerHoldoutScore.source_prediction_row_id == prediction.id,
                    )
                )
                if existing is None:
                    existing = MultibaggerHoldoutScore(**values)
                    session.add(existing)
                else:
                    _validate_projection(existing, values, "holdout score")
                expected_rows.append(existing)
    session.flush()
    checksum = canonical_json_sha256(
        sorted(item.score_fingerprint_sha256 for item in expected_rows)
    )
    if run.score_selection_checksum_sha256 not in {None, checksum}:
        raise HoldoutIntegrityError("holdout aggregate score checksum conflicts")
    run.score_selection_checksum_sha256 = checksum
    run.status = "scores_frozen"
    session.commit()
    return {
        "holdout_run_id": str(run.id),
        "subject_count": len(subjects),
        "score_count": len(expected_rows),
        "score_selection_checksum_sha256": checksum,
        "holdout_outcomes_read": False,
    }


def evaluate_temporal_holdout(
    session: Session,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    holdout_run_id: UUID,
    opened_at: datetime,
    activation_policy: HistoricalEvaluationActivationPolicy | None = None,
    split_policy: MultibaggerTemporalSplitPolicy | None = None,
) -> MultibaggerHoldoutDecision:
    """Open holdout once, evaluate persisted scores, and record a factual decision."""

    run = _holdout_run(session, holdout_run_id, policy)
    graph = _validated_run_input(
        session,
        run=run,
        policy=policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
    )
    requested_opened_at = _utc(opened_at)
    if run.status == "completed":
        if (
            run.holdout_opened_at is None
            or _stored_utc(run.holdout_opened_at) != requested_opened_at
        ):
            raise HoldoutIntegrityError("persisted holdout opening timestamp is immutable")
        return _validate_completed_holdout_graph(session, run=run, graph=graph)
    if run.status not in {"scores_frozen", "holdout_opened"}:
        raise HoldoutIntegrityError("holdout may open exactly once after scores are frozen")
    if run.status == "scores_frozen" and run.holdout_opened_at is not None:
        raise HoldoutIntegrityError("scores-frozen run already carries an opening timestamp")
    if run.status == "holdout_opened" and run.holdout_opened_at is None:
        raise HoldoutIntegrityError("opened holdout run lacks its durable opening timestamp")
    predictions = _holdout_predictions(session, graph.holdout_cohort_ids)
    scores = tuple(
        session.scalars(
            select(MultibaggerHoldoutScore).where(MultibaggerHoldoutScore.holdout_run_id == run.id)
        )
    )
    expected_subject_count = 2 if graph.candidate is not None else 1
    if len(scores) != len(predictions) * expected_subject_count:
        raise HoldoutIntegrityError("all holdout scores must exist before outcome access")
    _revalidate_frozen_holdout_scores(
        session, run=run, graph=graph, predictions=predictions, scores=scores
    )
    aggregate = canonical_json_sha256(sorted(item.score_fingerprint_sha256 for item in scores))
    if aggregate != run.score_selection_checksum_sha256:
        raise HoldoutIntegrityError("frozen holdout score set checksum mismatch")
    if run.status == "scores_frozen":
        # This commit is the durable one-way leakage boundary. No outcome-bearing
        # table is queried in the transaction that first persists the marker.
        run.holdout_opened_at = requested_opened_at
        run.status = "holdout_opened"
        session.commit()
    else:
        persisted_opened_at = run.holdout_opened_at
        if persisted_opened_at is None or _stored_utc(persisted_opened_at) != requested_opened_at:
            raise HoldoutIntegrityError("persisted holdout opening timestamp is immutable")
    facts = _load_holdout_outcomes_after_score_flush(
        session,
        holdout_cohort_ids=graph.holdout_cohort_ids,
        forbidden_cohort_ids=_non_holdout_cohort_ids(session, graph.split.id),
    )
    for values in _expected_evaluation_row_values(run=run, scores=scores, facts=facts):
        existing = session.scalar(
            select(MultibaggerHoldoutEvaluationRow).where(
                MultibaggerHoldoutEvaluationRow.holdout_score_id
                == values["holdout_score_id"],
                MultibaggerHoldoutEvaluationRow.outcome_contract_code
                == values["outcome_contract_code"],
                MultibaggerHoldoutEvaluationRow.selection_contract_code
                == values["selection_contract_code"],
                MultibaggerHoldoutEvaluationRow.evaluation_view == values["evaluation_view"],
            )
        )
        if existing is None:
            session.add(MultibaggerHoldoutEvaluationRow(**values))
        else:
            _validate_projection(existing, values, "holdout evaluation row")
    session.flush()
    _persist_holdout_metrics(session, run=run, scores=scores)
    run.status = "evaluated"
    decision = _persist_holdout_decision(session, run=run)
    run.status = "completed"
    run.completed_at = requested_opened_at
    run.summary_json = {
        **run.summary_json,
        "decision": decision.decision_code,
        "holdout_outcomes_read": True,
        "production_promotion_performed": False,
    }
    session.commit()
    return decision


def summarize_temporal_holdout(
    session: Session,
    run_id: UUID,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy | None = None,
    split_policy: MultibaggerTemporalSplitPolicy | None = None,
) -> dict[str, object]:
    run = _holdout_run(session, run_id, policy)
    graph = _validated_run_input(
        session,
        run=run,
        policy=policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
    )
    validated_decision = (
        _validate_completed_holdout_graph(session, run=run, graph=graph)
        if run.status == "completed"
        else None
    )
    metrics = tuple(
        session.scalars(
            select(MultibaggerHoldoutMetric)
            .where(MultibaggerHoldoutMetric.holdout_run_id == run_id)
            .order_by(
                MultibaggerHoldoutMetric.subject_code,
                MultibaggerHoldoutMetric.outcome_contract_code,
                MultibaggerHoldoutMetric.selection_contract_code,
                MultibaggerHoldoutMetric.evaluation_view,
            )
        )
    )
    return {
        "run_id": str(run.id),
        "status": run.status,
        "evidence_class": run.evidence_class,
        "terminal_mode": run.terminal_mode,
        "decision": validated_decision.decision_code if validated_decision else None,
        "production_promotion_performed": False,
        "metrics": [
            {
                "subject": item.subject_code,
                "outcome_contract": item.outcome_contract_code,
                "selection_contract": item.selection_contract_code,
                "evaluation_view": item.evaluation_view,
                "metrics": item.metrics_json,
                "delta": item.delta_metrics_json,
            }
            for item in metrics
        ],
    }


def inspect_temporal_holdout(
    session: Session,
    run_id: UUID,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy | None = None,
    split_policy: MultibaggerTemporalSplitPolicy | None = None,
    subject_code: str | None = None,
) -> list[dict[str, object]]:
    run = _holdout_run(session, run_id, policy)
    graph = _validated_run_input(
        session,
        run=run,
        policy=policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
    )
    if run.status == "completed":
        _validate_completed_holdout_graph(session, run=run, graph=graph)
    statement = select(MultibaggerHoldoutScore).where(
        MultibaggerHoldoutScore.holdout_run_id == run_id
    )
    if subject_code is not None:
        if subject_code not in {"champion", "challenger"}:
            raise ValueError("unsupported holdout subject")
        statement = statement.where(MultibaggerHoldoutScore.subject_code == subject_code)
    rows = tuple(
        session.scalars(
            statement.order_by(
                MultibaggerHoldoutScore.subject_code,
                MultibaggerHoldoutScore.source_evaluation_cohort_id,
                MultibaggerHoldoutScore.display_order,
                MultibaggerHoldoutScore.historical_symbol,
            )
        )
    )
    if run.status != "completed" and rows:
        all_scores = tuple(
            session.scalars(
                select(MultibaggerHoldoutScore).where(
                    MultibaggerHoldoutScore.holdout_run_id == run_id
                )
            )
        )
        _revalidate_frozen_holdout_scores(
            session,
            run=run,
            graph=graph,
            predictions=_holdout_predictions(session, graph.holdout_cohort_ids),
            scores=all_scores,
        )
    return [
        {
            "subject": row.subject_code,
            "cohort_id": str(row.source_evaluation_cohort_id),
            "prediction_id": str(row.source_prediction_row_id),
            "security_id": str(row.security_id),
            "historical_symbol": row.historical_symbol,
            "score": str(row.score) if row.score is not None else None,
            "rankable": row.rankable,
            "unranked_reason": row.unranked_reason,
            "display_order": row.display_order,
            "dense_score_rank": row.dense_score_rank,
            "selection_flags": row.selection_flags_json,
        }
        for row in rows
    ]


def _validated_run_input(
    session: Session,
    *,
    run: MultibaggerHoldoutEvaluationRun,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy | None,
    split_policy: MultibaggerTemporalSplitPolicy | None,
) -> HoldoutInputGraph:
    if run.evidence_class not in {PRODUCTION_EVIDENCE, FIXTURE_EVIDENCE}:
        raise HoldoutIntegrityError("unsupported persisted T evidence class")
    calendar_id = _split_calendar_id(session, run.temporal_split_run_id)
    if run.evidence_class == PRODUCTION_EVIDENCE:
        if activation_policy is None or split_policy is None:
            raise HoldoutIntegrityError("production T readiness policies are required")
        readiness = temporal_holdout_readiness(
            session,
            policy=policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
            experiment_id=run.s2_experiment_run_id,
            session_calendar_run_id=calendar_id,
        )
        if readiness.get("status") != "ready":
            raise HoldoutIntegrityError("production T readiness is no longer satisfied")
    graph = _validated_input_graph(
        session,
        policy=policy,
        s2_policy=s2_policy,
        experiment_id=run.s2_experiment_run_id,
        session_calendar_run_id=calendar_id,
        require_production=run.evidence_class == PRODUCTION_EVIDENCE,
    )
    _validate_run_graph(run, graph, policy=policy)
    return graph


def _validated_input_graph(
    session: Session,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    experiment_id: UUID,
    session_calendar_run_id: UUID,
    require_production: bool,
) -> HoldoutInputGraph:
    experiment = session.get(MultibaggerChallengerExperimentRun, experiment_id)
    if experiment is None:
        raise HoldoutIntegrityError("s2_experiment_unavailable")
    if (
        experiment.policy_code != S2_POLICY_CODE
        or experiment.policy_checksum_sha256 != S2_POLICY_CHECKSUM
        or experiment.activation_policy_checksum_sha256
        != policy.inherited_checksums["historical_evaluation_activation_policy_checksum_sha256"]
        or experiment.temporal_split_policy_checksum_sha256
        != policy.inherited_checksums["temporal_split_policy_checksum_sha256"]
        or experiment.error_analysis_policy_checksum_sha256
        != policy.inherited_checksums["error_analysis_policy_checksum_sha256"]
        or experiment.candidate_algorithm_version != ALGORITHM_VERSION
        or experiment.primary_objective_json != _primary_objective(policy)
        or s2_policy.checksum_sha256 != S2_POLICY_CHECKSUM
        or experiment.holdout_outcomes_accessed
    ):
        raise HoldoutIntegrityError("s2_experiment_binding_conflict")
    if require_production and experiment.evidence_class != "production_authoritative":
        raise HoldoutIntegrityError("fixture_s2_experiment_forbidden")
    if not require_production and experiment.evidence_class not in {
        "fixture_only",
        "production_authoritative",
    }:
        raise HoldoutIntegrityError("s2_evidence_class_unsupported")
    if experiment.status not in {"candidate_frozen_for_t", "no_challenger_selected"}:
        raise HoldoutIntegrityError("s2_terminal_state_unavailable")
    if (
        experiment.validation_opened_at is None
        or experiment.sealed_candidate_set_checksum_sha256 is None
    ):
        raise HoldoutIntegrityError("s2_validation_or_seal_unavailable")
    split = session.get(MultibaggerTemporalSplitRun, experiment.temporal_split_run_id)
    if (
        split is None
        or split.status != "completed"
        or split.source_evaluation_run_id != experiment.source_evaluation_run_id
        or split.split_policy_checksum_sha256
        != policy.inherited_checksums["temporal_split_policy_checksum_sha256"]
        or split.split_checksum_sha256 != experiment.temporal_split_checksum_sha256
        or split.session_calendar_run_id != session_calendar_run_id
    ):
        raise HoldoutIntegrityError("s1_split_binding_conflict")
    evaluation = session.get(MultibaggerEvaluationRun, split.source_evaluation_run_id)
    if (
        evaluation is None
        or evaluation.status != "completed"
        or evaluation.run_key_sha256 != experiment.source_evaluation_run_key_sha256
        or evaluation.evaluation_policy_checksum_sha256
        != policy.inherited_checksums["multibagger_evaluation_policy_checksum_sha256"]
        or evaluation.backtest_policy_checksum_sha256
        != policy.inherited_checksums["backtest_policy_checksum_sha256"]
        or evaluation.universe_policy_checksum_sha256
        != policy.inherited_checksums["historical_universe_policy_checksum_sha256"]
        or evaluation.outcome_policy_checksum_sha256
        != policy.inherited_checksums["multibagger_outcome_policy_checksum_sha256"]
        or evaluation.research_profile_checksum_sha256
        != policy.inherited_checksums["research_profile_checksum_sha256"]
        or evaluation.scoring_configuration_checksum_sha256 != CHAMPION_CHECKSUM
    ):
        raise HoldoutIntegrityError("production_r_binding_conflict")
    analysis = session.get(MultibaggerErrorAnalysisRun, experiment.error_analysis_run_id)
    if (
        analysis is None
        or analysis.status != "completed"
        or analysis.error_policy_checksum_sha256
        != policy.inherited_checksums["error_analysis_policy_checksum_sha256"]
        or analysis.temporal_split_run_id != split.id
        or analysis.temporal_split_checksum_sha256 != split.split_checksum_sha256
        or analysis.source_evaluation_run_id != evaluation.id
        or analysis.holdout_outcomes_accessed
    ):
        raise HoldoutIntegrityError("s1_error_analysis_binding_conflict")
    calendar = session.get(HistoricalSessionCalendarRun, session_calendar_run_id)
    if calendar is None:
        raise HoldoutIntegrityError("session_calendar_binding_conflict")
    configs = tuple(
        session.scalars(
            select(ScoringConfiguration).where(
                ScoringConfiguration.checksum_sha256 == CHAMPION_CHECKSUM
            )
        )
    )
    if (
        len(configs) != 1
        or configs[0].id != experiment.champion_scoring_configuration_id
        or experiment.champion_scoring_checksum_sha256 != CHAMPION_CHECKSUM
    ):
        raise HoldoutIntegrityError("champion_configuration_binding_conflict")
    cohort_sets, holdout_ids, membership_checksum = _validate_split_membership(
        session, split=split, evaluation=evaluation
    )
    if set(holdout_ids) != _uuid_set(experiment.holdout_cutoff_ids_json):
        raise HoldoutIntegrityError("s2_holdout_membership_conflict")
    if _uuid_set(experiment.development_cutoff_ids_json) != cohort_sets["development"]:
        raise HoldoutIntegrityError("s2_development_membership_conflict")
    if _uuid_set(experiment.validation_cutoff_ids_json) != cohort_sets["validation"]:
        raise HoldoutIntegrityError("s2_validation_membership_conflict")
    candidates = tuple(
        session.scalars(
            select(MultibaggerChallengerCandidate)
            .where(MultibaggerChallengerCandidate.experiment_run_id == experiment.id)
            .order_by(MultibaggerChallengerCandidate.candidate_code)
        )
    )
    try:
        sealed_checksum = _sealed_candidate_set_checksum(experiment, candidates)
    except ChallengerIntegrityError as error:
        raise HoldoutIntegrityError("s2_candidate_definition_conflict") from error
    if sealed_checksum != experiment.sealed_candidate_set_checksum_sha256:
        raise HoldoutIntegrityError("s2_sealed_candidate_set_conflict")
    validation_comparisons = [
        (item.candidate_code, _metric_cells(session, item.id, "validation")) for item in candidates
    ]
    selected_code, assessments = select_validation_challenger(validation_comparisons)
    freezes = tuple(
        session.scalars(
            select(MultibaggerChallengerFreeze).where(
                MultibaggerChallengerFreeze.experiment_run_id == experiment.id
            )
        )
    )
    freeze: MultibaggerChallengerFreeze | None = None
    candidate: MultibaggerChallengerCandidate | None = None
    terminal_mode = "champion_only"
    if experiment.status == "candidate_frozen_for_t":
        if len(freezes) != 1:
            raise HoldoutIntegrityError("exactly_one_s2_freeze_required")
        freeze = freezes[0]
        candidate = next((item for item in candidates if item.id == freeze.candidate_id), None)
        if candidate is None:
            raise HoldoutIntegrityError("frozen_s2_candidate_unavailable")
        expected_freeze_key = canonical_json_sha256(
            {
                "experiment_run_key_sha256": experiment.run_key_sha256,
                "candidate_key_sha256": candidate.candidate_key_sha256,
                "purpose": "candidate_for_production_t",
            }
        )
        if (
            freeze.freeze_key_sha256 != expected_freeze_key
            or freeze.freeze_purpose != "candidate_for_production_t"
            or freeze.production_readiness_status != "ready"
            or candidate.status != "eligible_for_t"
            or candidate.algorithm_version != ALGORITHM_VERSION
            or candidate.source_error_analysis_run_id != experiment.error_analysis_run_id
            or selected_code != candidate.candidate_code
            or canonical_json_sha256(freeze.validation_gate_json)
            != canonical_json_sha256(
                {"selected_candidate_code": selected_code, "assessments": assessments}
            )
        ):
            raise HoldoutIntegrityError("s2_freeze_or_candidate_binding_conflict")
        terminal_mode = "frozen_challenger"
    else:
        if (
            freezes
            or selected_code is not None
            or any(item.status == "eligible_for_t" for item in candidates)
        ):
            raise HoldoutIntegrityError("no_challenger_terminal_state_conflict")
    s2_holdout_scores = tuple(
        session.scalars(
            select(MultibaggerChallengerScore).where(
                MultibaggerChallengerScore.experiment_run_id == experiment.id,
                MultibaggerChallengerScore.source_evaluation_cohort_id.in_(holdout_ids),
            )
        )
    )
    if s2_holdout_scores:
        raise HoldoutIntegrityError("s2_holdout_score_firewall_conflict")
    if (
        session.scalar(
            select(MultibaggerChallengerEvaluationRow.id)
            .join(
                MultibaggerChallengerScore,
                MultibaggerChallengerEvaluationRow.candidate_score_id
                == MultibaggerChallengerScore.id,
            )
            .where(
                MultibaggerChallengerEvaluationRow.experiment_run_id == experiment.id,
                MultibaggerChallengerScore.source_evaluation_cohort_id.in_(holdout_ids),
            )
            .limit(1)
        )
        is not None
    ):
        raise HoldoutIntegrityError("s2_holdout_evaluation_firewall_conflict")
    if (
        session.scalar(
            select(MultibaggerChallengerMetric.id).where(
                MultibaggerChallengerMetric.experiment_run_id == experiment.id,
                MultibaggerChallengerMetric.split_code == "holdout",
            )
        )
        is not None
    ):
        raise HoldoutIntegrityError("s2_holdout_metric_firewall_conflict")
    return HoldoutInputGraph(
        experiment=experiment,
        split=split,
        evaluation=evaluation,
        holdout_cohort_ids=holdout_ids,
        holdout_membership_checksum_sha256=membership_checksum,
        terminal_mode=terminal_mode,
        freeze=freeze,
        candidate=candidate,
    )


def _validate_split_membership(
    session: Session,
    *,
    split: MultibaggerTemporalSplitRun,
    evaluation: MultibaggerEvaluationRun,
) -> tuple[dict[str, set[UUID]], tuple[UUID, ...], str]:
    assignments = tuple(
        session.scalars(
            select(MultibaggerTemporalSplitCutoff)
            .where(MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id == split.id)
            .order_by(MultibaggerTemporalSplitCutoff.historical_cutoff)
        )
    )
    if not assignments:
        raise HoldoutIntegrityError("s1_split_has_no_assignments")
    ids = [item.multibagger_evaluation_cohort_id for item in assignments]
    if len(ids) != len(set(ids)):
        raise HoldoutIntegrityError("s1_split_has_duplicate_assignment")
    sets = {
        code: {
            item.multibagger_evaluation_cohort_id for item in assignments if item.split_code == code
        }
        for code in ("development", "validation", "holdout")
    }
    if not all(sets.values()) or any(
        sets[left] & sets[right]
        for left, right in (
            ("development", "validation"),
            ("development", "holdout"),
            ("validation", "holdout"),
        )
    ):
        raise HoldoutIntegrityError("s1_split_assignments_are_not_disjoint")
    cohorts = tuple(
        session.scalars(
            select(MultibaggerEvaluationCohort).where(MultibaggerEvaluationCohort.id.in_(ids))
        )
    )
    if len(cohorts) != len(ids) or any(
        item.multibagger_evaluation_run_id != evaluation.id or item.status != "completed"
        for item in cohorts
    ):
        raise HoldoutIntegrityError("s1_split_contains_foreign_evaluation_cohort")
    cohort_by_id = {item.id: item for item in cohorts}
    projection: list[dict[str, object]] = []
    for assignment in assignments:
        cohort = cohort_by_id[assignment.multibagger_evaluation_cohort_id]
        expected_assignment_fingerprint = canonical_json_sha256(
            {
                "split_checksum_sha256": split.split_checksum_sha256,
                "cohort_id": str(cohort.id),
                "split_code": assignment.split_code,
            }
        )
        if (
            _stored_utc(assignment.historical_cutoff) != _stored_utc(cohort.historical_cutoff)
            or assignment.assignment_fingerprint_sha256 != expected_assignment_fingerprint
        ):
            raise HoldoutIntegrityError("s1_split_assignment_fingerprint_conflict")
        projection.append(
            {
                "cohort_id": str(cohort.id),
                "cutoff": _stored_utc(cohort.historical_cutoff).isoformat(),
                "bundle_checksum_sha256": cohort.bundle_checksum_sha256,
                "split_code": assignment.split_code,
            }
        )
    if canonical_json_sha256(projection) != split.split_checksum_sha256:
        raise HoldoutIntegrityError("s1_split_checksum_conflict")
    for code, values in (
        ("development", split.development_cutoffs_json),
        ("validation", split.validation_cutoffs_json),
        ("holdout", split.holdout_cutoffs_json),
    ):
        expected_dates = [
            _stored_utc(item.historical_cutoff).isoformat()
            for item in assignments
            if item.split_code == code
        ]
        if list(values) != expected_dates:
            raise HoldoutIntegrityError(f"s1_{code}_cutoff_projection_conflict")
    holdout_ids = tuple(
        item.multibagger_evaluation_cohort_id
        for item in assignments
        if item.split_code == "holdout"
    )
    membership_checksum = canonical_json_sha256([str(item) for item in holdout_ids])
    return sets, holdout_ids, membership_checksum


def _champion_score_for_prediction(
    session: Session,
    *,
    experiment: MultibaggerChallengerExperimentRun,
    prediction: MultibaggerPredictionRow,
) -> tuple[Decimal | None, str]:
    try:
        _validate_prediction_fingerprint(prediction)
    except ChallengerIntegrityError as error:
        raise HoldoutIntegrityError(str(error)) from error
    observation = session.get(BacktestObservation, prediction.backtest_observation_id)
    if observation is None:
        raise HoldoutIntegrityError("frozen J observation is unavailable")
    if (
        observation.company_id != prediction.company_id
        or observation.security_id != prediction.security_id
        or observation.symbol != prediction.historical_symbol
        or observation.score_snapshot_id != prediction.score_snapshot_id
        or observation.snapshot_fingerprint_sha256 != prediction.snapshot_fingerprint_sha256
        or observation.snapshot_status != prediction.snapshot_status
        or _stored_utc(observation.knowledge_cutoff)
        != _cohort_cutoff(session, prediction.multibagger_evaluation_cohort_id)
    ):
        raise HoldoutIntegrityError("frozen R/J prediction binding conflict")
    if prediction.score_snapshot_id is None:
        if (
            prediction.rankable
            or prediction.final_score is not None
            or prediction.snapshot_fingerprint_sha256 is not None
            or prediction.snapshot_status is not None
            or prediction.unranked_reason not in {"issuer_unavailable", "research_failed"}
        ):
            raise HoldoutIntegrityError("frozen champion no-snapshot state conflicts")
        return None, canonical_json_sha256(
            {"prediction_fingerprint": prediction.prediction_fingerprint_sha256, "snapshot": None}
        )
    snapshot = session.get(ScoreSnapshot, prediction.score_snapshot_id)
    if snapshot is None:
        raise HoldoutIntegrityError("frozen V5 score snapshot is unavailable")
    try:
        ScoreSnapshotRepository(session).validate_persisted_snapshot(snapshot)
    except ScoreSnapshotIntegrityError as error:
        raise HoldoutIntegrityError("frozen V5 score snapshot failed integrity") from error
    expected_rankable = bool(
        snapshot.eligibility_eligible
        and snapshot.final_score is not None
        and snapshot.snapshot_status == "final_score_available"
    )
    if (
        snapshot.algorithm_version != "score_snapshot_v5"
        or snapshot.selected_security_id != prediction.security_id
        or snapshot.snapshot_fingerprint_sha256 != prediction.snapshot_fingerprint_sha256
        or snapshot.scoring_configuration_id != experiment.champion_scoring_configuration_id
        or snapshot.configuration_checksum_sha256 != CHAMPION_CHECKSUM
        or _stored_utc(snapshot.knowledge_cutoff) != _stored_utc(observation.knowledge_cutoff)
        or snapshot.snapshot_status != prediction.snapshot_status
        or expected_rankable != prediction.rankable
        or (prediction.rankable and snapshot.final_score != prediction.final_score)
    ):
        raise HoldoutIntegrityError("frozen champion snapshot binding conflict")
    components = sorted(snapshot.components, key=lambda item: item.component_code)
    component_projection = [
        {
            "component_code": item.component_code,
            "score": str(item.score) if item.score is not None else None,
            "algorithm_version": item.algorithm_version,
        }
        for item in components
    ]
    return prediction.final_score, canonical_json_sha256(component_projection)


def _load_holdout_outcomes_after_score_flush(
    session: Session,
    *,
    holdout_cohort_ids: Sequence[UUID],
    forbidden_cohort_ids: set[UUID],
    flush_before_query: bool = True,
) -> tuple[MultibaggerConfusionRow, ...]:
    target = set(holdout_cohort_ids)
    if not target or target & forbidden_cohort_ids:
        raise HoldoutIntegrityError("outcome query is not isolated to exact holdout cohorts")
    if flush_before_query:
        session.flush()
    return tuple(
        session.scalars(
            select(MultibaggerConfusionRow)
            .join(
                MultibaggerPredictionRow,
                MultibaggerConfusionRow.multibagger_prediction_row_id
                == MultibaggerPredictionRow.id,
            )
            .where(
                MultibaggerPredictionRow.multibagger_evaluation_cohort_id.in_(
                    tuple(holdout_cohort_ids)
                ),
                MultibaggerConfusionRow.selection_contract_code == "TOP_5",
                MultibaggerConfusionRow.evaluation_view == "end_to_end",
            )
        )
    )


def _expected_evaluation_row_values(
    *,
    run: MultibaggerHoldoutEvaluationRun,
    scores: Sequence[MultibaggerHoldoutScore],
    facts: Sequence[MultibaggerConfusionRow],
) -> tuple[dict[str, object], ...]:
    prediction_ids = {item.source_prediction_row_id for item in scores}
    fact_by_key = {
        (item.multibagger_prediction_row_id, item.outcome_contract_code): item for item in facts
    }
    expected_fact_keys = {
        (prediction_id, outcome)
        for prediction_id in prediction_ids
        for outcome in OUTCOME_CONTRACTS
    }
    if len(fact_by_key) != len(facts) or set(fact_by_key) != expected_fact_keys:
        raise HoldoutIntegrityError("holdout ground truth is incomplete, duplicated, or foreign")
    values: list[dict[str, object]] = []
    for score in scores:
        for outcome in OUTCOME_CONTRACTS:
            source = fact_by_key[(score.source_prediction_row_id, outcome)]
            _validate_source_confusion(source)
            if (
                source.multibagger_prediction_row_id != score.source_prediction_row_id
                or source.outcome_contract_code != outcome
                or source.selection_contract_code != "TOP_5"
                or source.evaluation_view != "end_to_end"
            ):
                raise HoldoutIntegrityError("source R confusion binding conflicts")
            for selection in SELECTION_CONTRACTS:
                selected = score.selection_flags_json.get(selection)
                if not isinstance(selected, bool):
                    raise HoldoutIntegrityError("holdout selection projection is malformed")
                for view in EVALUATION_VIEWS:
                    projection: dict[str, object] = {
                        "holdout_run_id": run.id,
                        "holdout_score_id": score.id,
                        "source_confusion_row_id": source.id,
                        "outcome_contract_code": outcome,
                        "selection_contract_code": selection,
                        "evaluation_view": view,
                        "selected": selected,
                        "actual_label": source.actual_label,
                        "confusion_class": confusion_class(
                            rankable=score.rankable,
                            selected=selected,
                            actual_label=source.actual_label,
                            evaluation_view=view,
                        ),
                    }
                    projection["row_fingerprint_sha256"] = canonical_json_sha256(
                        {
                            "score_fingerprint_sha256": score.score_fingerprint_sha256,
                            "source_confusion_fingerprint_sha256": (
                                source.confusion_fingerprint_sha256
                            ),
                            "projection": _jsonable(projection),
                        }
                    )
                    values.append(projection)
    return tuple(values)


def _persist_holdout_metrics(
    session: Session,
    *,
    run: MultibaggerHoldoutEvaluationRun,
    scores: Sequence[MultibaggerHoldoutScore],
) -> None:
    for values in _expected_metric_values(session, run=run, scores=scores):
        existing = session.scalar(
            select(MultibaggerHoldoutMetric).where(
                MultibaggerHoldoutMetric.holdout_run_id == run.id,
                MultibaggerHoldoutMetric.subject_code == values["subject_code"],
                MultibaggerHoldoutMetric.outcome_contract_code
                == values["outcome_contract_code"],
                MultibaggerHoldoutMetric.selection_contract_code
                == values["selection_contract_code"],
                MultibaggerHoldoutMetric.evaluation_view == values["evaluation_view"],
            )
        )
        if existing is None:
            session.add(MultibaggerHoldoutMetric(**values))
        else:
            _validate_projection(existing, values, "holdout metric")
    session.flush()


def _expected_metric_values(
    session: Session,
    *,
    run: MultibaggerHoldoutEvaluationRun,
    scores: Sequence[MultibaggerHoldoutScore],
) -> tuple[dict[str, object], ...]:
    score_by_id = {item.id: item for item in scores}
    subjects = tuple(sorted({item.subject_code for item in scores}))
    computed: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for subject in subjects:
        subject_scores = {item.id: item for item in scores if item.subject_code == subject}
        rows = tuple(
            session.scalars(
                select(MultibaggerHoldoutEvaluationRow).where(
                    MultibaggerHoldoutEvaluationRow.holdout_run_id == run.id,
                    MultibaggerHoldoutEvaluationRow.holdout_score_id.in_(tuple(subject_scores)),
                )
            )
        )
        for outcome in OUTCOME_CONTRACTS:
            for selection in SELECTION_CONTRACTS:
                for view in EVALUATION_VIEWS:
                    cell_rows = [
                        item
                        for item in rows
                        if item.outcome_contract_code == outcome
                        and item.selection_contract_code == selection
                        and item.evaluation_view == view
                    ]
                    metrics = _metrics(cell_rows, score_by_id, view=view)
                    computed[(subject, outcome, selection, view)] = metrics
    expected: list[dict[str, object]] = []
    for key, raw_metrics in computed.items():
        subject, outcome, selection, view = key
        metrics = dict(raw_metrics)
        champion = dict(computed[("champion", outcome, selection, view)])
        champion.pop("selected_prediction_ids", None)
        champion_projection = champion if subject == "challenger" else None
        deltas = (
            {
                name: _decimal_delta(metrics.get(name), champion.get(name))
                for name in ("precision", "recall", "f1", "positive_median_rank")
            }
            if subject == "challenger"
            else None
        )
        rankability_delta = _metric_int(metrics, "rankable_count") - _metric_int(
            champion, "rankable_count"
        )
        coverage_delta = _metric_int(metrics, "covered_count") - _metric_int(
            champion, "covered_count"
        )
        if subject == "challenger" and (rankability_delta != 0 or coverage_delta != 0):
            raise HoldoutIntegrityError("challenger rankability or coverage differs from champion")
        selection_membership = metrics.pop("selected_prediction_ids")
        selection_checksum = canonical_json_sha256(selection_membership)
        projection = {
            "holdout_run_key_sha256": run.run_key_sha256,
            "subject": subject,
            "outcome": outcome,
            "selection": selection,
            "view": view,
            "metrics": metrics,
            "champion_metrics": champion_projection,
            "delta_metrics": deltas,
            "rankability_delta": rankability_delta,
            "coverage_delta": coverage_delta,
            "selection_membership_checksum_sha256": selection_checksum,
        }
        values: dict[str, object] = {
            "holdout_run_id": run.id,
            "subject_code": subject,
            "outcome_contract_code": outcome,
            "selection_contract_code": selection,
            "evaluation_view": view,
            "metrics_json": metrics,
            "champion_metrics_json": champion_projection,
            "delta_metrics_json": deltas,
            "rankability_delta": rankability_delta,
            "coverage_delta": coverage_delta,
            "selection_membership_checksum_sha256": selection_checksum,
            "metric_fingerprint_sha256": canonical_json_sha256(projection),
        }
        expected.append(values)
    return tuple(expected)


def _revalidate_frozen_holdout_scores(
    session: Session,
    *,
    run: MultibaggerHoldoutEvaluationRun,
    graph: HoldoutInputGraph,
    predictions: Sequence[MultibaggerPredictionRow],
    scores: Sequence[MultibaggerHoldoutScore],
) -> None:
    """Recompute the complete outcome-free projection before any holdout read/reuse."""

    candidate = graph.candidate
    subjects = ("champion", "challenger") if candidate is not None else ("champion",)
    existing = {(item.subject_code, item.source_prediction_row_id): item for item in scores}
    if len(existing) != len(scores) or len(scores) != len(predictions) * len(subjects):
        raise HoldoutIntegrityError("frozen holdout score set is incomplete or duplicated")
    expected_keys = {(subject, prediction.id) for subject in subjects for prediction in predictions}
    if set(existing) != expected_keys:
        raise HoldoutIntegrityError("frozen holdout score membership conflicts")
    for subject in subjects:
        weights = (
            _weights_from_candidate(candidate) if subject == "challenger" and candidate else None
        )
        provisional: dict[UUID, list[tuple[UUID, str, UUID, Decimal | None, bool, str | None]]] = {}
        component_checksums: dict[UUID, str] = {}
        for prediction in predictions:
            score, component_checksum = _champion_score_for_prediction(
                session, experiment=graph.experiment, prediction=prediction
            )
            if subject == "challenger":
                if weights is None:
                    raise HoldoutIntegrityError("frozen challenger weights are unavailable")
                try:
                    score, candidate_checksum = _candidate_score_for_prediction(
                        session,
                        experiment=graph.experiment,
                        prediction=prediction,
                        weights=weights,
                    )
                except ChallengerIntegrityError as error:
                    raise HoldoutIntegrityError(str(error)) from error
                if candidate_checksum != component_checksum:
                    raise HoldoutIntegrityError("frozen component state differs by subject")
            component_checksums[prediction.id] = component_checksum
            provisional.setdefault(prediction.multibagger_evaluation_cohort_id, []).append(
                (
                    prediction.id,
                    prediction.historical_symbol,
                    prediction.security_id,
                    score,
                    prediction.rankable,
                    prediction.unranked_reason,
                )
            )
        for cohort_id, raw in provisional.items():
            ranked = rank_candidate_scores(raw)
            rankable_count = sum(item.rankable for item in ranked)
            flags, ties = _selection_projection(ranked, rankable_count=rankable_count)
            prediction_by_id = {item.id: item for item in predictions}
            for item in ranked:
                prediction = prediction_by_id[item.prediction_id]
                values: dict[str, object] = {
                    "holdout_run_id": run.id,
                    "subject_code": subject,
                    "candidate_id": candidate.id if subject == "challenger" and candidate else None,
                    "source_evaluation_cohort_id": cohort_id,
                    "source_prediction_row_id": prediction.id,
                    "security_id": prediction.security_id,
                    "historical_symbol": prediction.historical_symbol,
                    "source_score_snapshot_id": prediction.score_snapshot_id,
                    "source_prediction_fingerprint_sha256": (
                        prediction.prediction_fingerprint_sha256
                    ),
                    "source_snapshot_fingerprint_sha256": prediction.snapshot_fingerprint_sha256,
                    "component_state_checksum_sha256": component_checksums[prediction.id],
                    "score": item.score,
                    "rankable": item.rankable,
                    "unranked_reason": item.unranked_reason,
                    "display_order": item.display_order,
                    "dense_score_rank": item.dense_score_rank,
                    "rankable_count": rankable_count,
                    "cohort_size": len(ranked),
                    "selection_flags_json": flags[item.prediction_id],
                    "selection_boundary_ties_json": ties,
                }
                values["score_fingerprint_sha256"] = canonical_json_sha256(
                    {
                        "holdout_run_key_sha256": run.run_key_sha256,
                        "subject_code": subject,
                        "candidate_key_sha256": candidate.candidate_key_sha256
                        if subject == "challenger" and candidate
                        else None,
                        "source_prediction_fingerprint_sha256": (
                            prediction.prediction_fingerprint_sha256
                        ),
                        "source_snapshot_fingerprint_sha256": (
                            prediction.snapshot_fingerprint_sha256
                        ),
                        "component_state_checksum_sha256": component_checksums[prediction.id],
                        "projection": _jsonable(values),
                    }
                )
                _validate_projection(existing[(subject, prediction.id)], values, "holdout score")
    aggregate = canonical_json_sha256(sorted(item.score_fingerprint_sha256 for item in scores))
    if aggregate != run.score_selection_checksum_sha256:
        raise HoldoutIntegrityError("frozen holdout score aggregate checksum conflicts")


def _validate_completed_holdout_graph(
    session: Session,
    *,
    run: MultibaggerHoldoutEvaluationRun,
    graph: HoldoutInputGraph,
) -> MultibaggerHoldoutDecision:
    if (
        run.status != "completed"
        or run.holdout_opened_at is None
        or run.completed_at is None
        or _stored_utc(run.completed_at) < _stored_utc(run.holdout_opened_at)
        or run.score_selection_checksum_sha256 is None
        or run.summary_json.get("production_promotion_performed") is not False
    ):
        raise HoldoutIntegrityError("completed holdout run state conflicts")
    predictions = _holdout_predictions(session, graph.holdout_cohort_ids)
    scores = tuple(
        session.scalars(
            select(MultibaggerHoldoutScore).where(MultibaggerHoldoutScore.holdout_run_id == run.id)
        )
    )
    _revalidate_frozen_holdout_scores(
        session,
        run=run,
        graph=graph,
        predictions=predictions,
        scores=scores,
    )
    facts = _load_holdout_outcomes_after_score_flush(
        session,
        holdout_cohort_ids=graph.holdout_cohort_ids,
        forbidden_cohort_ids=_non_holdout_cohort_ids(session, graph.split.id),
        flush_before_query=False,
    )
    expected_rows = _expected_evaluation_row_values(run=run, scores=scores, facts=facts)
    persisted_rows = tuple(
        session.scalars(
            select(MultibaggerHoldoutEvaluationRow).where(
                MultibaggerHoldoutEvaluationRow.holdout_run_id == run.id
            )
        )
    )
    persisted_by_key = {
        (
            item.holdout_score_id,
            item.outcome_contract_code,
            item.selection_contract_code,
            item.evaluation_view,
        ): item
        for item in persisted_rows
    }
    expected_by_key = {
        (
            cast(UUID, item["holdout_score_id"]),
            cast(str, item["outcome_contract_code"]),
            cast(str, item["selection_contract_code"]),
            cast(str, item["evaluation_view"]),
        ): item
        for item in expected_rows
    }
    if (
        len(persisted_by_key) != len(persisted_rows)
        or set(persisted_by_key) != set(expected_by_key)
    ):
        raise HoldoutIntegrityError("completed holdout evaluation row set conflicts")
    for key, expected in expected_by_key.items():
        _validate_projection(persisted_by_key[key], expected, "holdout evaluation row")

    expected_metrics = _expected_metric_values(session, run=run, scores=scores)
    persisted_metrics = tuple(
        session.scalars(
            select(MultibaggerHoldoutMetric).where(
                MultibaggerHoldoutMetric.holdout_run_id == run.id
            )
        )
    )
    persisted_metric_by_key = {
        (
            item.subject_code,
            item.outcome_contract_code,
            item.selection_contract_code,
            item.evaluation_view,
        ): item
        for item in persisted_metrics
    }
    expected_metric_by_key = {
        (
            cast(str, item["subject_code"]),
            cast(str, item["outcome_contract_code"]),
            cast(str, item["selection_contract_code"]),
            cast(str, item["evaluation_view"]),
        ): item
        for item in expected_metrics
    }
    if (
        len(persisted_metric_by_key) != len(persisted_metrics)
        or set(persisted_metric_by_key) != set(expected_metric_by_key)
    ):
        raise HoldoutIntegrityError("completed holdout metric set conflicts")
    for key, expected in expected_metric_by_key.items():
        _validate_projection(persisted_metric_by_key[key], expected, "holdout metric")

    decisions = tuple(
        session.scalars(
            select(MultibaggerHoldoutDecision).where(
                MultibaggerHoldoutDecision.holdout_run_id == run.id
            )
        )
    )
    if len(decisions) != 1:
        raise HoldoutIntegrityError("completed holdout run requires exactly one decision")
    decision = decisions[0]
    expected_decision = _expected_decision_values(run=run, metrics=persisted_metrics)
    _validate_projection(decision, expected_decision, "holdout decision")
    if run.summary_json.get("decision") != decision.decision_code:
        raise HoldoutIntegrityError("completed holdout summary decision conflicts")
    return decision


def _persist_holdout_decision(
    session: Session, *, run: MultibaggerHoldoutEvaluationRun
) -> MultibaggerHoldoutDecision:
    metrics = tuple(
        session.scalars(
            select(MultibaggerHoldoutMetric).where(
                MultibaggerHoldoutMetric.holdout_run_id == run.id
            )
        )
    )
    values = _expected_decision_values(run=run, metrics=metrics)
    existing = session.scalar(
        select(MultibaggerHoldoutDecision).where(
            MultibaggerHoldoutDecision.holdout_run_id == run.id
        )
    )
    if existing is None:
        existing = MultibaggerHoldoutDecision(**values)
        session.add(existing)
    else:
        _validate_projection(existing, values, "holdout decision")
    session.flush()
    return existing


def _expected_decision_values(
    *,
    run: MultibaggerHoldoutEvaluationRun,
    metrics: Sequence[MultibaggerHoldoutMetric],
) -> dict[str, object]:
    if run.terminal_mode == "champion_only":
        decision_code = "CHAMPION_RETAINED_NO_CHALLENGER"
        gate: dict[str, object] = {
            "rule": "no_frozen_challenger",
            "passed": True,
            "production_promotion": False,
        }
    else:
        cells = {
            (item.outcome_contract_code, item.selection_contract_code, item.evaluation_view): item
            for item in metrics
            if item.subject_code == "challenger"
        }
        passed, reasons = holdout_decision_gate(cells)
        decision_code = "CHALLENGER_PASSES_HOLDOUT" if passed else "CHALLENGER_FAILS_HOLDOUT"
        gate = {"passed": passed, "reasons": list(reasons), "production_promotion": False}
    projection = {
        "holdout_run_key_sha256": run.run_key_sha256,
        "decision_code": decision_code,
        "decision_rule": "conservative_holdout_guardrails_v1",
        "gate": gate,
        "production_promotion_performed": False,
    }
    values: dict[str, object] = {
        "holdout_run_id": run.id,
        "decision_code": decision_code,
        "decision_rule": "conservative_holdout_guardrails_v1",
        "gate_json": gate,
        "production_promotion_performed": False,
        "decision_fingerprint_sha256": canonical_json_sha256(projection),
    }
    return values


def _validate_persisted_decision(
    run: MultibaggerHoldoutEvaluationRun, decision: MultibaggerHoldoutDecision
) -> None:
    projection = {
        "holdout_run_key_sha256": run.run_key_sha256,
        "decision_code": decision.decision_code,
        "decision_rule": decision.decision_rule,
        "gate": decision.gate_json,
        "production_promotion_performed": decision.production_promotion_performed,
    }
    if (
        decision.production_promotion_performed
        or canonical_json_sha256(projection) != decision.decision_fingerprint_sha256
    ):
        raise HoldoutIntegrityError("persisted holdout decision conflicts")


def holdout_decision_gate(
    metrics: Mapping[tuple[str, str, str], MultibaggerHoldoutMetric],
) -> tuple[bool, tuple[str, ...]]:
    """Apply the predeclared conservative final holdout gate."""

    reasons: list[str] = []
    primary = metrics.get(("MB_3X_3Y", "TOP_20", "end_to_end"))
    if primary is None or primary.champion_metrics_json is None:
        return False, ("insufficient_holdout_metric_support",)
    candidate = primary.metrics_json
    champion = primary.champion_metrics_json
    for name in ("f1", "precision", "recall", "positive_median_rank"):
        if candidate.get(name) is None or champion.get(name) is None:
            return False, ("insufficient_holdout_metric_support",)
    if Decimal(str(candidate["f1"])) <= Decimal(str(champion["f1"])):
        reasons.append("primary_f1_not_strictly_greater")
    if Decimal(str(candidate["precision"])) < Decimal(str(champion["precision"])):
        reasons.append("primary_precision_guardrail_failed")
    if Decimal(str(candidate["recall"])) < Decimal(str(champion["recall"])):
        reasons.append("primary_recall_guardrail_failed")
    if Decimal(str(candidate["positive_median_rank"])) > Decimal(
        str(champion["positive_median_rank"])
    ):
        reasons.append("positive_median_rank_guardrail_failed")
    for outcome in ("MB_2X_2Y", "MB_5X_5Y"):
        cell = metrics.get((outcome, "TOP_20", "end_to_end"))
        if (
            cell is None
            or cell.champion_metrics_json is None
            or cell.metrics_json.get("f1") is None
            or cell.champion_metrics_json.get("f1") is None
        ):
            reasons.append("insufficient_holdout_metric_support")
        elif Decimal(str(cell.metrics_json["f1"])) < Decimal(str(cell.champion_metrics_json["f1"])):
            reasons.append(f"{outcome.lower()}_f1_guardrail_failed")
    if primary.rankability_delta != 0 or primary.coverage_delta != 0:
        reasons.append("rankability_or_coverage_changed")
    return not reasons, tuple(dict.fromkeys(reasons))


def _metrics(
    rows: Sequence[MultibaggerHoldoutEvaluationRow],
    scores: Mapping[UUID, MultibaggerHoldoutScore],
    *,
    view: str,
) -> dict[str, object]:
    view_rows = [
        row for row in rows if view == "end_to_end" or scores[row.holdout_score_id].rankable
    ]
    counts = {
        code: sum(row.confusion_class == code for row in view_rows)
        for code in ("tp", "fp", "fn", "tn")
    }
    ranks = sorted(
        score.display_order
        for row in view_rows
        if row.actual_label == "positive"
        and (score := scores[row.holdout_score_id]).display_order is not None
    )
    selected_ids = sorted(
        str(scores[row.holdout_score_id].source_prediction_row_id)
        for row in view_rows
        if row.selected
    )
    covered_count = sum(
        row.confusion_class not in {"excluded_unmatured", "excluded_unavailable"}
        for row in view_rows
    )
    return {
        **counts,
        "precision": _decimal(_ratio(counts["tp"], counts["tp"] + counts["fp"])),
        "recall": _decimal(_ratio(counts["tp"], counts["tp"] + counts["fn"])),
        "f1": _decimal(_f1(counts["tp"], counts["fp"], counts["fn"])),
        "selected_count": sum(row.selected for row in view_rows),
        "rankable_count": sum(scores[row.holdout_score_id].rankable for row in view_rows),
        "eligible_count": len(view_rows),
        "covered_count": covered_count,
        "coverage": _decimal(_ratio(covered_count, len(view_rows))),
        "positive_count": counts["tp"] + counts["fn"],
        "positive_median_rank": _decimal(_median(ranks)),
        "selected_prediction_ids": selected_ids,
    }


def _validate_source_confusion(row: MultibaggerConfusionRow) -> None:
    projection: dict[str, object] = {
        "multibagger_outcome_label_id": row.multibagger_outcome_label_id,
        "outcome_contract_code": row.outcome_contract_code,
        "selection_contract_code": row.selection_contract_code,
        "evaluation_view": row.evaluation_view,
        "selected": row.selected,
        "actual_label": row.actual_label,
        "confusion_class": row.confusion_class,
        "threshold_hit_trading_date": row.threshold_hit_trading_date,
        "endpoint_return": row.endpoint_return,
        "peak_price_multiple": row.peak_price_multiple,
        "maximum_drawdown": row.maximum_drawdown,
        "label_provenance_json": row.label_provenance_json,
    }
    projection["confusion_fingerprint_sha256"] = canonical_json_sha256(_jsonable(projection))
    if projection["confusion_fingerprint_sha256"] != row.confusion_fingerprint_sha256:
        raise HoldoutIntegrityError("source R confusion fingerprint conflicts")


def _run_identity(
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
    graph: HoldoutInputGraph,
    evidence_class: str,
) -> dict[str, object]:
    experiment = graph.experiment
    return {
        "policy_checksum_sha256": policy.checksum_sha256,
        "source_evaluation_run_key_sha256": graph.evaluation.run_key_sha256,
        "temporal_split_run_key_sha256": graph.split.run_key_sha256,
        "temporal_split_checksum_sha256": graph.split.split_checksum_sha256,
        "holdout_membership_checksum_sha256": graph.holdout_membership_checksum_sha256,
        "champion_scoring_configuration_id": str(experiment.champion_scoring_configuration_id),
        "champion_scoring_checksum_sha256": CHAMPION_CHECKSUM,
        "s2_experiment_run_key_sha256": experiment.run_key_sha256,
        "evidence_class": evidence_class,
        "terminal_mode": graph.terminal_mode,
        "freeze_key_sha256": graph.freeze.freeze_key_sha256 if graph.freeze else None,
        "candidate_key_sha256": (
            graph.candidate.candidate_key_sha256 if graph.candidate else None
        ),
        "candidate_checksum_sha256": (
            graph.candidate.candidate_checksum_sha256 if graph.candidate else None
        ),
        "primary_objective": _primary_objective(policy),
        "outcome_data_cutoff": _stored_utc(graph.split.outcome_data_cutoff).isoformat(),
    }


def _validate_run_graph(
    run: MultibaggerHoldoutEvaluationRun,
    graph: HoldoutInputGraph,
    *,
    policy: MultibaggerTemporalHoldoutPolicy,
) -> None:
    expected_identity = _run_identity(
        policy=policy,
        graph=graph,
        evidence_class=run.evidence_class,
    )
    expected = {
        "run_key_sha256": canonical_json_sha256(expected_identity),
        "policy_code": policy.code,
        "policy_checksum_sha256": policy.checksum_sha256,
        "source_evaluation_run_id": graph.evaluation.id,
        "source_evaluation_run_key_sha256": graph.evaluation.run_key_sha256,
        "temporal_split_run_id": graph.split.id,
        "temporal_split_checksum_sha256": graph.split.split_checksum_sha256,
        "holdout_cohort_ids_json": [str(item) for item in graph.holdout_cohort_ids],
        "holdout_membership_checksum_sha256": graph.holdout_membership_checksum_sha256,
        "champion_scoring_configuration_id": graph.experiment.champion_scoring_configuration_id,
        "champion_scoring_checksum_sha256": CHAMPION_CHECKSUM,
        "s2_experiment_run_id": graph.experiment.id,
        "s2_experiment_run_key_sha256": graph.experiment.run_key_sha256,
        "evidence_class": run.evidence_class,
        "terminal_mode": graph.terminal_mode,
        "challenger_freeze_id": graph.freeze.id if graph.freeze else None,
        "challenger_freeze_key_sha256": graph.freeze.freeze_key_sha256 if graph.freeze else None,
        "challenger_candidate_id": graph.candidate.id if graph.candidate else None,
        "challenger_candidate_key_sha256": (
            graph.candidate.candidate_key_sha256 if graph.candidate else None
        ),
        "challenger_candidate_checksum_sha256": (
            graph.candidate.candidate_checksum_sha256 if graph.candidate else None
        ),
        "primary_objective_json": _primary_objective(policy),
        "outcome_data_cutoff": graph.split.outcome_data_cutoff,
    }
    _validate_projection(run, expected, "holdout upstream graph")


def _holdout_predictions(
    session: Session, cohort_ids: Sequence[UUID]
) -> tuple[MultibaggerPredictionRow, ...]:
    rows = tuple(
        session.scalars(
            select(MultibaggerPredictionRow)
            .options(raiseload(MultibaggerPredictionRow.confusions))
            .where(MultibaggerPredictionRow.multibagger_evaluation_cohort_id.in_(cohort_ids))
            .order_by(
                MultibaggerPredictionRow.multibagger_evaluation_cohort_id,
                MultibaggerPredictionRow.historical_symbol,
                MultibaggerPredictionRow.security_id,
            )
        )
    )
    if not rows or {item.multibagger_evaluation_cohort_id for item in rows} != set(cohort_ids):
        raise HoldoutIntegrityError("exact holdout prediction membership is unavailable")
    return rows


def _non_holdout_cohort_ids(session: Session, split_id: UUID) -> set[UUID]:
    return set(
        session.scalars(
            select(MultibaggerTemporalSplitCutoff.multibagger_evaluation_cohort_id).where(
                MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id == split_id,
                MultibaggerTemporalSplitCutoff.split_code.in_(("development", "validation")),
            )
        )
    )


def _cohort_cutoff(session: Session, cohort_id: UUID) -> datetime:
    value = session.scalar(
        select(MultibaggerEvaluationCohort.historical_cutoff).where(
            MultibaggerEvaluationCohort.id == cohort_id
        )
    )
    if value is None:
        raise HoldoutIntegrityError("source R cohort is unavailable")
    return _stored_utc(value)


def _split_calendar_id(session: Session, split_id: UUID) -> UUID:
    value = session.scalar(
        select(MultibaggerTemporalSplitRun.session_calendar_run_id).where(
            MultibaggerTemporalSplitRun.id == split_id
        )
    )
    if value is None:
        raise HoldoutIntegrityError("bound S1 session calendar is unavailable")
    return value


def _holdout_run(
    session: Session, run_id: UUID, policy: MultibaggerTemporalHoldoutPolicy
) -> MultibaggerHoldoutEvaluationRun:
    run = session.get(MultibaggerHoldoutEvaluationRun, run_id)
    if run is None:
        raise HoldoutIntegrityError("holdout run is unavailable")
    if run.policy_code != policy.code or run.policy_checksum_sha256 != policy.checksum_sha256:
        raise HoldoutIntegrityError("holdout policy binding conflict")
    return run


def _primary_objective(policy: MultibaggerTemporalHoldoutPolicy) -> dict[str, str]:
    return {
        "outcome_contract": policy.primary_outcome_contract,
        "selection_contract": policy.primary_selection_contract,
        "evaluation_view": policy.primary_evaluation_view,
        "metric": policy.primary_comparison_metric,
    }


def _validate_projection(persisted: object, expected: Mapping[str, object], label: str) -> None:
    if {field: getattr(persisted, field) for field in expected} != dict(expected):
        raise HoldoutIntegrityError(f"existing {label} conflicts with immutable projection")


def _uuid_set(value: object) -> set[UUID]:
    if not isinstance(value, list):
        raise HoldoutIntegrityError("persisted cohort identity list is malformed")
    try:
        result = {UUID(str(item)) for item in value}
    except ValueError as error:
        raise HoldoutIntegrityError("persisted cohort identity is malformed") from error
    if len(result) != len(value):
        raise HoldoutIntegrityError("persisted cohort identity list contains duplicates")
    return result


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    return None if denominator == 0 else Decimal(numerator) / Decimal(denominator)


def _f1(tp: int, fp: int, fn: int) -> Decimal | None:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    if precision is None or recall is None or precision + recall == 0:
        return None
    return Decimal("2") * precision * recall / (precision + recall)


def _median(values: Sequence[int]) -> Decimal | None:
    if not values:
        return None
    result = median([Decimal(item) for item in values])
    return result if isinstance(result, Decimal) else Decimal(str(result))


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal_delta(value: object, champion: object) -> str | None:
    if value is None or champion is None:
        return None
    return str(Decimal(str(value)) - Decimal(str(champion)))


def _metric_int(metrics: Mapping[str, object], field: str) -> int:
    value = metrics[field]
    if not isinstance(value, int):
        raise HoldoutIntegrityError(f"holdout metric {field} is malformed")
    return value


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _stored_utc(value).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("holdout timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
