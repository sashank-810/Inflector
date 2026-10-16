"""Production S2 governed counterfactual V5 top-level weight experiments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, raiseload

from inflector_data.historical_activation_policy import (
    HistoricalEvaluationActivationPolicy,
    MultibaggerTemporalSplitPolicy,
    load_historical_evaluation_activation_policy,
    load_multibagger_temporal_split_policy,
)
from inflector_data.historical_evaluation_activation import historical_evaluation_readiness
from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.multibagger_challenger_policy import (
    COMPONENT_CODES,
    EVALUATION_VIEWS,
    OUTCOME_CONTRACTS,
    SELECTION_CONTRACTS,
    MultibaggerChallengerDevelopmentPolicy,
)
from inflector_database.models import (
    BacktestObservation,
    MultibaggerChallengerCandidate,
    MultibaggerChallengerEvaluationRow,
    MultibaggerChallengerExperimentRun,
    MultibaggerChallengerFreeze,
    MultibaggerChallengerMetric,
    MultibaggerChallengerScore,
    MultibaggerConfusionRow,
    MultibaggerErrorAnalysisRow,
    MultibaggerErrorAnalysisRun,
    MultibaggerEvaluationRun,
    MultibaggerPredictionRow,
    MultibaggerTemporalSplitCutoff,
    MultibaggerTemporalSplitRun,
    ScoreSnapshot,
    ScoringConfiguration,
)
from inflector_database.score_repository import ScoreSnapshotIntegrityError, ScoreSnapshotRepository

ALGORITHM_VERSION = "counterfactual_v5_top_level_weight_recomposition_v1"
CHAMPION_CHECKSUM = "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
S2_POLICY_CHECKSUM = "17642f0e05640464b0166bc82740882e6fda5c99e62ca555c19d7be30ebe85e0"
DEVELOPMENT_EVIDENCE_VERSION = "s1_error_analysis_row_v1"


class ChallengerIntegrityError(RuntimeError):
    """The immutable S2 experiment contract was violated."""


@dataclass(frozen=True, slots=True)
class CandidateWeights:
    ordered: tuple[tuple[str, Decimal], ...]
    checksum_sha256: str

    def as_json(self) -> list[dict[str, str]]:
        return [{"component_code": code, "weight": str(weight)} for code, weight in self.ordered]


@dataclass(frozen=True, slots=True)
class RankedCandidateScore:
    prediction_id: UUID
    score: Decimal | None
    rankable: bool
    unranked_reason: str | None
    display_order: int | None
    dense_score_rank: int | None


def parse_candidate_weights(value: Mapping[str, object]) -> CandidateWeights:
    """Validate an exact string-Decimal vector in canonical V5 component order."""

    if set(value) != set(COMPONENT_CODES):
        raise ValueError("candidate weights must contain exactly the eight V5 components")
    ordered: list[tuple[str, Decimal]] = []
    for code in COMPONENT_CODES:
        raw = value[code]
        if not isinstance(raw, str):
            raise ValueError("candidate weights must be exact Decimal strings")
        try:
            weight = Decimal(raw)
        except InvalidOperation as error:
            raise ValueError("candidate weight is not a valid Decimal string") from error
        if not weight.is_finite() or weight < 0 or weight > 1:
            raise ValueError("candidate weights must be finite and between zero and one")
        ordered.append((code, weight))
    if sum((weight for _, weight in ordered), Decimal("0")) != Decimal("1"):
        raise ValueError("candidate weights must sum exactly to one")
    projection = [{"component_code": code, "weight": str(weight)} for code, weight in ordered]
    return CandidateWeights(tuple(ordered), canonical_json_sha256(projection))


def counterfactual_candidate_score(
    weights: CandidateWeights, component_scores: Mapping[str, Decimal]
) -> Decimal:
    """Recompose frozen component scores without confidence or coverage adjustment."""

    if set(component_scores) != set(COMPONENT_CODES):
        raise ChallengerIntegrityError("rankable V5 snapshot must contain all eight components")
    if any(not isinstance(value, Decimal) for value in component_scores.values()):
        raise TypeError("frozen component scores must be Decimal values")
    return sum((weight * component_scores[code] for code, weight in weights.ordered), Decimal("0"))


def rank_candidate_scores(
    rows: Sequence[tuple[UUID, str, UUID, Decimal | None, bool, str | None]],
) -> tuple[RankedCandidateScore, ...]:
    """Rank one cohort by score desc, symbol asc, security UUID asc."""

    if any((item[4] and item[3] is None) or (not item[4] and item[3] is not None) for item in rows):
        raise ChallengerIntegrityError("candidate score and champion rankability disagree")
    rankable = sorted(
        (item for item in rows if item[4]),
        key=lambda item: (-(item[3] or Decimal("0")), item[1], str(item[2])),
    )
    ranks: dict[UUID, tuple[int, int]] = {}
    prior: Decimal | None = None
    dense = 0
    for order, item in enumerate(rankable, start=1):
        if item[3] != prior:
            dense += 1
            prior = item[3]
        ranks[item[0]] = (order, dense)
    return tuple(
        RankedCandidateScore(
            prediction_id=item[0],
            score=item[3],
            rankable=item[4],
            unranked_reason=item[5],
            display_order=ranks[item[0]][0] if item[4] else None,
            dense_score_rank=ranks[item[0]][1] if item[4] else None,
        )
        for item in rows
    )


def selection_count(selection_code: str, rankable_count: int) -> int:
    if selection_code not in SELECTION_CONTRACTS:
        raise ValueError("unsupported selection contract")
    if rankable_count <= 0:
        return 0
    if selection_code.startswith("TOP_") and selection_code.endswith("_PERCENT"):
        percent = Decimal(selection_code.removeprefix("TOP_").removesuffix("_PERCENT"))
        count = int(
            (Decimal(rankable_count) * percent / Decimal("100")).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        return min(max(1, count), rankable_count)
    return min(int(selection_code.removeprefix("TOP_")), rankable_count)


def _selection_projection(
    rows: Sequence[RankedCandidateScore], *, rankable_count: int
) -> tuple[dict[UUID, dict[str, bool]], dict[str, bool]]:
    ordered = sorted(
        (item for item in rows if item.rankable and item.display_order is not None),
        key=lambda item: item.display_order or 0,
    )
    flags = {
        item.prediction_id: {
            code: bool(
                item.rankable
                and item.display_order is not None
                and item.display_order <= selection_count(code, rankable_count)
            )
            for code in SELECTION_CONTRACTS
        }
        for item in rows
    }
    ties: dict[str, bool] = {}
    for code in SELECTION_CONTRACTS:
        count = selection_count(code, rankable_count)
        ties[code] = bool(
            0 < count < len(ordered) and ordered[count - 1].score == ordered[count].score
        )
    return flags, ties


def create_challenger_experiment(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    temporal_split_run_id: UUID,
    error_analysis_run_id: UUID,
) -> MultibaggerChallengerExperimentRun:
    """Create a production-authoritative experiment only from a verified DB chain."""

    split = session.get(MultibaggerTemporalSplitRun, temporal_split_run_id)
    if split is None:
        raise ChallengerIntegrityError("bound S1 temporal split is unavailable")
    readiness = challenger_readiness(
        session,
        policy=policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
        source_evaluation_run_id=split.source_evaluation_run_id,
        session_calendar_run_id=split.session_calendar_run_id,
        temporal_split_run_id=temporal_split_run_id,
        error_analysis_run_id=error_analysis_run_id,
    )
    if readiness["status"] != "ready":
        raise ChallengerIntegrityError(str(readiness["message"]))
    return _create_challenger_experiment(
        session,
        policy=policy,
        temporal_split_run_id=temporal_split_run_id,
        error_analysis_run_id=error_analysis_run_id,
        evidence_class="production_authoritative",
    )


def create_fixture_challenger_experiment(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    temporal_split_run_id: UUID,
    error_analysis_run_id: UUID,
) -> MultibaggerChallengerExperimentRun:
    """Explicit test-only path; fixture evidence can never freeze for Production T."""

    return _create_challenger_experiment(
        session,
        policy=policy,
        temporal_split_run_id=temporal_split_run_id,
        error_analysis_run_id=error_analysis_run_id,
        evidence_class="fixture_only",
    )


def _create_challenger_experiment(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    temporal_split_run_id: UUID,
    error_analysis_run_id: UUID,
    evidence_class: str,
) -> MultibaggerChallengerExperimentRun:
    """Create/reuse the immutable input identity; no future outcomes are read."""

    if evidence_class not in {"production_authoritative", "fixture_only"}:
        raise ChallengerIntegrityError("experiment authority must be derived by an approved path")
    split = session.get(MultibaggerTemporalSplitRun, temporal_split_run_id)
    analysis = session.execute(
        select(
            MultibaggerErrorAnalysisRun.id,
            MultibaggerErrorAnalysisRun.status,
            MultibaggerErrorAnalysisRun.error_policy_checksum_sha256,
            MultibaggerErrorAnalysisRun.temporal_split_run_id,
            MultibaggerErrorAnalysisRun.source_evaluation_run_id,
            MultibaggerErrorAnalysisRun.holdout_outcomes_accessed,
        ).where(MultibaggerErrorAnalysisRun.id == error_analysis_run_id)
    ).one_or_none()
    if split is None or split.status != "completed":
        raise ChallengerIntegrityError("completed S1 temporal split is required")
    if analysis is None or analysis.status != "completed":
        raise ChallengerIntegrityError("completed S1 error analysis is required")
    if (
        split.split_policy_checksum_sha256
        != policy.inherited_checksums["temporal_split_policy_checksum_sha256"]
        or analysis.error_policy_checksum_sha256
        != policy.inherited_checksums["error_analysis_policy_checksum_sha256"]
        or analysis.temporal_split_run_id != split.id
        or analysis.source_evaluation_run_id != split.source_evaluation_run_id
        or analysis.holdout_outcomes_accessed
    ):
        raise ChallengerIntegrityError("S1 experiment binding mismatch")
    evaluation = session.execute(
        select(
            MultibaggerEvaluationRun.id,
            MultibaggerEvaluationRun.status,
            MultibaggerEvaluationRun.run_key_sha256,
            MultibaggerEvaluationRun.evaluation_policy_checksum_sha256,
            MultibaggerEvaluationRun.scoring_configuration_checksum_sha256,
            MultibaggerEvaluationRun.model_family,
        ).where(MultibaggerEvaluationRun.id == split.source_evaluation_run_id)
    ).one_or_none()
    if (
        evaluation is None
        or evaluation.status != "completed"
        or evaluation.evaluation_policy_checksum_sha256
        != policy.inherited_checksums["multibagger_evaluation_policy_checksum_sha256"]
        or evaluation.scoring_configuration_checksum_sha256 != CHAMPION_CHECKSUM
    ):
        raise ChallengerIntegrityError("accepted completed Production R evaluation is required")
    configs = tuple(
        session.scalars(
            select(ScoringConfiguration).where(
                ScoringConfiguration.checksum_sha256 == CHAMPION_CHECKSUM
            )
        )
    )
    if len(configs) != 1:
        raise ChallengerIntegrityError("exactly one accepted champion configuration is required")
    cutoffs = tuple(
        session.scalars(
            select(MultibaggerTemporalSplitCutoff)
            .where(MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id == split.id)
            .order_by(MultibaggerTemporalSplitCutoff.historical_cutoff)
        )
    )
    by_split = {
        code: [
            str(item.multibagger_evaluation_cohort_id)
            for item in cutoffs
            if item.split_code == code
        ]
        for code in ("development", "validation", "holdout")
    }
    if not all(by_split.values()):
        raise ChallengerIntegrityError("S1 split must contain development, validation, and holdout")
    run_key = canonical_json_sha256(
        {
            "policy_checksum_sha256": policy.checksum_sha256,
            "temporal_split_run_id": str(split.id),
            "temporal_split_checksum_sha256": split.split_checksum_sha256,
            "error_analysis_run_id": str(analysis.id),
            "source_evaluation_run_key_sha256": evaluation.run_key_sha256,
            "champion_scoring_checksum_sha256": CHAMPION_CHECKSUM,
            "evidence_class": evidence_class,
            "development": by_split["development"],
            "validation": by_split["validation"],
            "holdout": by_split["holdout"],
        }
    )
    existing = session.scalar(
        select(MultibaggerChallengerExperimentRun).where(
            MultibaggerChallengerExperimentRun.run_key_sha256 == run_key
        )
    )
    if existing is not None:
        return existing
    experiment = MultibaggerChallengerExperimentRun(
        run_key_sha256=run_key,
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
        temporal_split_run_id=split.id,
        temporal_split_checksum_sha256=split.split_checksum_sha256,
        error_analysis_run_id=analysis.id,
        source_evaluation_run_id=evaluation.id,
        source_evaluation_run_key_sha256=evaluation.run_key_sha256,
        champion_scoring_configuration_id=configs[0].id,
        champion_scoring_checksum_sha256=CHAMPION_CHECKSUM,
        champion_model_family=evaluation.model_family,
        development_cutoff_ids_json=by_split["development"],
        validation_cutoff_ids_json=by_split["validation"],
        holdout_cutoff_ids_json=by_split["holdout"],
        candidate_algorithm_version=ALGORITHM_VERSION,
        primary_objective_json={
            "outcome_contract": policy.primary_outcome_contract,
            "selection_contract": policy.primary_selection_contract,
            "evaluation_view": policy.primary_evaluation_view,
            "metric": policy.primary_comparison_metric,
        },
        evidence_class=evidence_class,
        status="draft",
        holdout_outcomes_accessed=False,
        summary_json={},
    )
    session.add(experiment)
    session.flush()
    return experiment


def _canonical_development_evidence(
    session: Session,
    *,
    experiment: MultibaggerChallengerExperimentRun,
    references: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], str]:
    required = {
        "reference_version",
        "error_analysis_run_id",
        "error_analysis_row_id",
        "split_code",
        "outcome_contract_code",
        "selection_contract_code",
        "evaluation_view",
        "error_signature",
    }
    analysis = session.get(MultibaggerErrorAnalysisRun, experiment.error_analysis_run_id)
    if analysis is None or analysis.status != "completed" or analysis.holdout_outcomes_accessed:
        raise ChallengerIntegrityError("bound completed S1 error analysis is unavailable")
    canonical: list[dict[str, object]] = []
    seen: set[UUID] = set()
    for supplied in references:
        if set(supplied) != required:
            raise ChallengerIntegrityError("development evidence reference fields are not exact")
        if supplied["reference_version"] != DEVELOPMENT_EVIDENCE_VERSION:
            raise ChallengerIntegrityError("development evidence reference version is unsupported")
        try:
            run_id = UUID(str(supplied["error_analysis_run_id"]))
            row_id = UUID(str(supplied["error_analysis_row_id"]))
        except ValueError as error:
            raise ChallengerIntegrityError("development evidence identity is malformed") from error
        if run_id != experiment.error_analysis_run_id:
            raise ChallengerIntegrityError("candidate references foreign S1 error analysis")
        if row_id in seen:
            raise ChallengerIntegrityError("duplicate development evidence reference")
        seen.add(row_id)
        row = session.get(MultibaggerErrorAnalysisRow, row_id)
        if row is None or row.multibagger_error_analysis_run_id != run_id:
            raise ChallengerIntegrityError("development evidence row is unresolved")
        expected: dict[str, object] = {
            "reference_version": DEVELOPMENT_EVIDENCE_VERSION,
            "error_analysis_run_id": str(run_id),
            "error_analysis_row_id": str(row.id),
            "split_code": row.split_code,
            "outcome_contract_code": row.outcome_contract_code,
            "selection_contract_code": row.selection_contract_code,
            "evaluation_view": row.evaluation_view,
            "error_signature": row.error_signature,
        }
        if expected["split_code"] != "development" or dict(supplied) != expected:
            raise ChallengerIntegrityError(
                "development evidence does not match the persisted S1 fact"
            )
        row_projection = {
            "analysis_run_key_sha256": analysis.run_key_sha256,
            "source_confusion_row_id": str(row.source_confusion_row_id),
            "split_code": row.split_code,
            "confusion_class": row.confusion_class,
            "component_values": row.component_values_json,
            "error_signature": row.error_signature,
        }
        if canonical_json_sha256(row_projection) != row.row_fingerprint_sha256:
            raise ChallengerIntegrityError(
                "persisted S1 development evidence fingerprint conflicts"
            )
        canonical.append(expected)
    if not canonical:
        raise ValueError("at least one development evidence reference is required")
    canonical.sort(key=lambda item: str(item["error_analysis_row_id"]))
    return canonical, canonical_json_sha256(canonical)


def _candidate_definition_projection(
    *,
    experiment: MultibaggerChallengerExperimentRun,
    candidate_code: str,
    weights: CandidateWeights,
    hypothesis: str,
    evidence: Sequence[Mapping[str, object]],
    evidence_checksum: str,
) -> dict[str, object]:
    return {
        "experiment_run_key_sha256": experiment.run_key_sha256,
        "candidate_code": candidate_code,
        "ordered_weights": weights.as_json(),
        "candidate_checksum_sha256": weights.checksum_sha256,
        "hypothesis": hypothesis,
        "source_error_analysis_run_id": str(experiment.error_analysis_run_id),
        "development_evidence": [dict(item) for item in evidence],
        "development_evidence_checksum_sha256": evidence_checksum,
        "algorithm_version": ALGORITHM_VERSION,
    }


def _persisted_candidate_definition(
    experiment: MultibaggerChallengerExperimentRun,
    candidate: MultibaggerChallengerCandidate,
) -> dict[str, object]:
    return {
        "experiment_run_key_sha256": experiment.run_key_sha256,
        "candidate_code": candidate.candidate_code,
        "ordered_weights": candidate.ordered_weights_json,
        "candidate_checksum_sha256": candidate.candidate_checksum_sha256,
        "hypothesis": candidate.hypothesis,
        "source_error_analysis_run_id": str(candidate.source_error_analysis_run_id),
        "development_evidence": candidate.development_evidence_json,
        "development_evidence_checksum_sha256": (
            candidate.development_evidence_checksum_sha256
        ),
        "algorithm_version": candidate.algorithm_version,
    }


def register_challenger_candidate(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    experiment_id: UUID,
    candidate_code: str,
    weights: Mapping[str, object],
    hypothesis: str,
    development_evidence: Sequence[Mapping[str, object]],
) -> MultibaggerChallengerCandidate:
    experiment = _experiment(session, experiment_id, policy)
    if (
        experiment.status not in {"draft", "development_evaluated"}
        or experiment.validation_opened_at
        or experiment.sealed_candidate_set_checksum_sha256 is not None
    ):
        raise ChallengerIntegrityError("candidate set is immutable after sealing or validation")
    existing_count = len(
        tuple(
            session.scalars(
                select(MultibaggerChallengerCandidate).where(
                    MultibaggerChallengerCandidate.experiment_run_id == experiment.id
                )
            )
        )
    )
    if not candidate_code or not hypothesis.strip() or not development_evidence:
        raise ValueError("candidate code, hypothesis, and development evidence are required")
    parsed = parse_candidate_weights(weights)
    normalized_hypothesis = " ".join(hypothesis.split())
    canonical_evidence, evidence_checksum = _canonical_development_evidence(
        session, experiment=experiment, references=development_evidence
    )
    definition = _candidate_definition_projection(
        experiment=experiment,
        candidate_code=candidate_code,
        weights=parsed,
        hypothesis=normalized_hypothesis,
        evidence=canonical_evidence,
        evidence_checksum=evidence_checksum,
    )
    key = canonical_json_sha256(definition)
    existing = session.scalar(
        select(MultibaggerChallengerCandidate).where(
            MultibaggerChallengerCandidate.experiment_run_id == experiment.id,
            MultibaggerChallengerCandidate.candidate_code == candidate_code,
        )
    )
    if existing is not None:
        if (
            existing.candidate_key_sha256 != key
            or _persisted_candidate_definition(experiment, existing) != definition
        ):
            raise ChallengerIntegrityError("candidate code conflicts with existing definition")
        return existing
    same_weights = session.scalar(
        select(MultibaggerChallengerCandidate).where(
            MultibaggerChallengerCandidate.experiment_run_id == experiment.id,
            MultibaggerChallengerCandidate.candidate_checksum_sha256 == parsed.checksum_sha256,
        )
    )
    if same_weights is not None:
        raise ChallengerIntegrityError("candidate weights conflict with existing definition")
    if existing_count >= policy.maximum_candidates_per_experiment:
        raise ChallengerIntegrityError("maximum explicit candidate count exceeded")
    candidate = MultibaggerChallengerCandidate(
        experiment_run_id=experiment.id,
        candidate_code=candidate_code,
        candidate_key_sha256=key,
        candidate_checksum_sha256=parsed.checksum_sha256,
        ordered_weights_json=parsed.as_json(),
        hypothesis=normalized_hypothesis,
        source_error_analysis_run_id=experiment.error_analysis_run_id,
        development_evidence_json=canonical_evidence,
        development_evidence_checksum_sha256=evidence_checksum,
        algorithm_version=ALGORITHM_VERSION,
        status="registered",
    )
    session.add(candidate)
    session.flush()
    return candidate


def seal_challenger_candidate_set(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    experiment_id: UUID,
) -> str:
    experiment = _experiment(session, experiment_id, policy)
    if experiment.validation_opened_at is not None:
        raise ChallengerIntegrityError("validation has already opened")
    candidates = _candidates(session, experiment.id)
    if not candidates:
        raise ChallengerIntegrityError("cannot seal an empty candidate set")
    checksum = _sealed_candidate_set_checksum(experiment, candidates)
    persisted = experiment.sealed_candidate_set_checksum_sha256
    if persisted is not None:
        if experiment.status != "candidate_set_sealed" or persisted != checksum:
            raise ChallengerIntegrityError("sealed candidate set checksum conflict")
        return persisted
    if experiment.status not in {"draft", "development_evaluated"}:
        raise ChallengerIntegrityError("experiment cannot enter the sealed state")
    experiment.sealed_candidate_set_checksum_sha256 = checksum
    experiment.status = "candidate_set_sealed"
    for candidate in candidates:
        candidate.status = "sealed"
    session.flush()
    return checksum


def _sealed_candidate_set_checksum(
    experiment: MultibaggerChallengerExperimentRun,
    candidates: Sequence[MultibaggerChallengerCandidate],
) -> str:
    projection: list[dict[str, str]] = []
    for candidate in candidates:
        if (
            canonical_json_sha256(candidate.development_evidence_json)
            != candidate.development_evidence_checksum_sha256
        ):
            raise ChallengerIntegrityError("candidate development evidence checksum conflicts")
        try:
            weights = parse_candidate_weights(
                {
                    str(item["component_code"]): item["weight"]
                    for item in candidate.ordered_weights_json
                    if isinstance(item, dict)
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ChallengerIntegrityError("persisted candidate weights are malformed") from error
        definition = _candidate_definition_projection(
            experiment=experiment,
            candidate_code=candidate.candidate_code,
            weights=weights,
            hypothesis=candidate.hypothesis,
            evidence=[
                item for item in candidate.development_evidence_json if isinstance(item, dict)
            ],
            evidence_checksum=candidate.development_evidence_checksum_sha256,
        )
        if (
            weights.checksum_sha256 != candidate.candidate_checksum_sha256
            or canonical_json_sha256(definition) != candidate.candidate_key_sha256
            or _persisted_candidate_definition(experiment, candidate) != definition
        ):
            raise ChallengerIntegrityError("persisted candidate definition conflicts")
        projection.append(
            {
                "candidate_code": candidate.candidate_code,
                "candidate_key_sha256": candidate.candidate_key_sha256,
            }
        )
    return canonical_json_sha256(projection)


def evaluate_challenger_split(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    experiment_id: UUID,
    split_code: str,
    evaluated_at: datetime,
) -> dict[str, object]:
    """Persist score/rank state, flush, and only then read outcome-bearing R rows."""

    if split_code not in policy.allowed_splits:
        raise ValueError("S2 may evaluate only development or validation")
    experiment = _experiment(session, experiment_id, policy)
    candidates = _candidates(session, experiment.id)
    if not candidates:
        raise ChallengerIntegrityError("experiment has no candidates")
    if split_code == "development":
        if (
            experiment.status not in {"draft", "development_evaluated"}
            or experiment.validation_opened_at is not None
            or experiment.sealed_candidate_set_checksum_sha256 is not None
        ):
            raise ChallengerIntegrityError("development evaluation is forbidden after sealing")
    else:
        if (
            experiment.status != "candidate_set_sealed"
            or experiment.validation_opened_at is not None
            or experiment.sealed_candidate_set_checksum_sha256 is None
        ):
            raise ChallengerIntegrityError("candidate set must be sealed before validation")
        recomputed = _sealed_candidate_set_checksum(experiment, candidates)
        if recomputed != experiment.sealed_candidate_set_checksum_sha256:
            raise ChallengerIntegrityError("sealed candidate set checksum conflict")
    cutoff_rows = tuple(
        session.scalars(
            select(MultibaggerTemporalSplitCutoff).where(
                MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id
                == experiment.temporal_split_run_id,
                MultibaggerTemporalSplitCutoff.split_code == split_code,
            )
        )
    )
    cohort_ids = tuple(item.multibagger_evaluation_cohort_id for item in cutoff_rows)
    holdout_ids = {UUID(str(value)) for value in experiment.holdout_cutoff_ids_json}
    if not cohort_ids or set(cohort_ids) & holdout_ids:
        raise ChallengerIntegrityError("S2 split cohort identity violates holdout firewall")
    predictions = tuple(
        session.scalars(
            select(MultibaggerPredictionRow)
            .options(raiseload(MultibaggerPredictionRow.confusions))
            .where(MultibaggerPredictionRow.multibagger_evaluation_cohort_id.in_(cohort_ids))
        )
    )
    if not predictions:
        raise ChallengerIntegrityError("S2 split has no frozen predictions")
    experiment.status = (
        "validation_evaluating" if split_code == "validation" else "development_evaluating"
    )
    persisted: list[MultibaggerChallengerScore] = []
    for candidate in candidates:
        weights = _weights_from_candidate(candidate)
        by_cohort: dict[UUID, list[tuple[UUID, str, UUID, Decimal | None, bool, str | None]]] = {}
        component_checksums: dict[UUID, str] = {}
        for prediction in predictions:
            score, component_checksum = _candidate_score_for_prediction(
                session, experiment=experiment, prediction=prediction, weights=weights
            )
            component_checksums[prediction.id] = component_checksum
            by_cohort.setdefault(prediction.multibagger_evaluation_cohort_id, []).append(
                (
                    prediction.id,
                    prediction.historical_symbol,
                    prediction.security_id,
                    score,
                    prediction.rankable,
                    prediction.unranked_reason,
                )
            )
        for cohort_id, raw_rows in by_cohort.items():
            ranked = rank_candidate_scores(raw_rows)
            rankable_count = sum(item.rankable for item in ranked)
            flags_by_prediction, boundary_ties = _selection_projection(
                ranked, rankable_count=rankable_count
            )
            for item in ranked:
                prediction = next(value for value in predictions if value.id == item.prediction_id)
                row_values: dict[str, object] = {
                    "experiment_run_id": experiment.id,
                    "candidate_id": candidate.id,
                    "split_code": split_code,
                    "source_evaluation_cohort_id": cohort_id,
                    "source_prediction_row_id": prediction.id,
                    "security_id": prediction.security_id,
                    "historical_symbol": prediction.historical_symbol,
                    "source_score_snapshot_id": prediction.score_snapshot_id,
                    "source_snapshot_fingerprint_sha256": (
                        prediction.snapshot_fingerprint_sha256
                    ),
                    "component_state_checksum_sha256": component_checksums[prediction.id],
                    "candidate_score": item.score,
                    "rankable": item.rankable,
                    "unranked_reason": item.unranked_reason,
                    "display_order": item.display_order,
                    "dense_score_rank": item.dense_score_rank,
                    "rankable_count": rankable_count,
                    "cohort_size": len(ranked),
                    "selection_flags_json": flags_by_prediction[item.prediction_id],
                    "selection_boundary_ties_json": boundary_ties,
                }
                fingerprint_projection = {
                    "experiment_run_key_sha256": experiment.run_key_sha256,
                    "candidate_key_sha256": candidate.candidate_key_sha256,
                    "source_prediction_fingerprint_sha256": (
                        prediction.prediction_fingerprint_sha256
                    ),
                    "source_snapshot_fingerprint_sha256": (
                        prediction.snapshot_fingerprint_sha256
                    ),
                    "component_state_checksum_sha256": component_checksums[prediction.id],
                    "algorithm_version": ALGORITHM_VERSION,
                    "score_rank_selection": _jsonable(row_values),
                }
                fingerprint = canonical_json_sha256(fingerprint_projection)
                row_values["score_fingerprint_sha256"] = fingerprint
                existing = session.scalar(
                    select(MultibaggerChallengerScore).where(
                        MultibaggerChallengerScore.candidate_id == candidate.id,
                        MultibaggerChallengerScore.source_prediction_row_id == prediction.id,
                    )
                )
                if existing is not None:
                    _validate_existing_score(existing, row_values)
                    persisted.append(existing)
                    continue
                row = MultibaggerChallengerScore(**row_values)
                session.add(row)
                persisted.append(row)
    prediction_selection_checksum = canonical_json_sha256(
        sorted(item.score_fingerprint_sha256 for item in persisted)
    )
    experiment.summary_json = {
        **experiment.summary_json,
        f"{split_code}_prediction_selection_checksum_sha256": prediction_selection_checksum,
    }
    if split_code == "validation":
        experiment.validation_opened_at = _utc(evaluated_at)
    source_facts = _load_outcome_facts_after_score_flush(
        session,
        cohort_ids=cohort_ids,
        forbidden_holdout_ids=holdout_ids,
    )

    fact_by_prediction_contract = {
        (item.multibagger_prediction_row_id, item.outcome_contract_code): item
        for item in source_facts
    }
    expected = len(predictions) * len(OUTCOME_CONTRACTS)
    if len(fact_by_prediction_contract) != expected:
        raise ChallengerIntegrityError("R ground-truth projection is incomplete or duplicated")
    score_by_key = {(item.candidate_id, item.source_prediction_row_id): item for item in persisted}
    created_evaluations = 0
    for candidate in candidates:
        for prediction in predictions:
            score_row = score_by_key[(candidate.id, prediction.id)]
            for outcome in OUTCOME_CONTRACTS:
                source = fact_by_prediction_contract[(prediction.id, outcome)]
                for selection_code in SELECTION_CONTRACTS:
                    raw_selected = score_row.selection_flags_json.get(selection_code)
                    if not isinstance(raw_selected, bool):
                        raise ChallengerIntegrityError(
                            "persisted candidate selection projection is malformed"
                        )
                    selected = raw_selected
                    for view in EVALUATION_VIEWS:
                        confusion = confusion_class(
                            rankable=score_row.rankable,
                            selected=selected,
                            actual_label=source.actual_label,
                            evaluation_view=view,
                        )
                        fingerprint = canonical_json_sha256(
                            {
                                "score_fingerprint": score_row.score_fingerprint_sha256,
                                "source_confusion_row_id": str(source.id),
                                "source_confusion_fingerprint": source.confusion_fingerprint_sha256,
                                "outcome_contract": outcome,
                                "selection_contract": selection_code,
                                "evaluation_view": view,
                                "selected": selected,
                                "actual_label": source.actual_label,
                                "confusion_class": confusion,
                            }
                        )
                        row_values: dict[str, object] = {
                            "experiment_run_id": experiment.id,
                            "candidate_id": candidate.id,
                            "candidate_score_id": score_row.id,
                            "source_confusion_row_id": source.id,
                            "split_code": split_code,
                            "outcome_contract_code": outcome,
                            "selection_contract_code": selection_code,
                            "evaluation_view": view,
                            "selected": selected,
                            "actual_label": source.actual_label,
                            "confusion_class": confusion,
                            "row_fingerprint_sha256": fingerprint,
                        }
                        existing = session.scalar(
                            select(MultibaggerChallengerEvaluationRow).where(
                                MultibaggerChallengerEvaluationRow.candidate_score_id
                                == score_row.id,
                                MultibaggerChallengerEvaluationRow.outcome_contract_code == outcome,
                                MultibaggerChallengerEvaluationRow.selection_contract_code
                                == selection_code,
                                MultibaggerChallengerEvaluationRow.evaluation_view == view,
                            )
                        )
                        if existing is None:
                            session.add(MultibaggerChallengerEvaluationRow(**row_values))
                            created_evaluations += 1
                        else:
                            _validate_existing_evaluation_row(existing, row_values)
    session.flush()
    _persist_metrics(
        session,
        experiment=experiment,
        candidates=candidates,
        predictions=predictions,
        split_code=split_code,
    )
    experiment.status = (
        "validation_evaluated" if split_code == "validation" else "development_evaluated"
    )
    for candidate in candidates:
        candidate.status = (
            "validation_evaluated" if split_code == "validation" else "development_evaluated"
        )
    session.commit()
    return {
        "experiment_id": str(experiment.id),
        "split_code": split_code,
        "candidate_count": len(candidates),
        "score_row_count": len(persisted),
        "created_evaluation_row_count": created_evaluations,
        "holdout_outcomes_accessed": False,
    }


def _load_outcome_facts_after_score_flush(
    session: Session,
    *,
    cohort_ids: Sequence[UUID],
    forbidden_holdout_ids: set[UUID] | frozenset[UUID] = frozenset(),
) -> tuple[MultibaggerConfusionRow, ...]:
    """Physical validation barrier: flush outcome-free state, then query facts."""

    if set(cohort_ids) & set(forbidden_holdout_ids):
        raise ChallengerIntegrityError("outcome-bearing S2 query includes a holdout cohort")
    # No outcome-bearing table is queried by evaluate_challenger_split before
    # this call. The flush makes score/rank/selection state durable in the
    # transaction before the first R confusion read.
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
                MultibaggerPredictionRow.multibagger_evaluation_cohort_id.in_(cohort_ids),
                MultibaggerConfusionRow.selection_contract_code == "TOP_5",
                MultibaggerConfusionRow.evaluation_view == "end_to_end",
            )
        )
    )


def confusion_class(
    *, rankable: bool, selected: bool, actual_label: str, evaluation_view: str
) -> str:
    if actual_label == "unmatured":
        return "excluded_unmatured"
    if actual_label == "unavailable":
        return "excluded_unavailable"
    if evaluation_view == "rankable_only" and not rankable:
        return "excluded_rankability_for_rankable_only"
    if actual_label == "positive":
        return "tp" if selected else "fn"
    if actual_label == "negative":
        return "fp" if selected else "tn"
    raise ChallengerIntegrityError("unsupported ground-truth label")


def validation_gate(
    metrics: Mapping[tuple[str, str, str], tuple[Mapping[str, object], Mapping[str, object]]],
) -> tuple[bool, tuple[str, ...]]:
    """Apply the immutable conservative S2 validation acceptance rule."""

    reasons: list[str] = []
    primary = ("MB_3X_3Y", "TOP_20", "end_to_end")
    candidate, champion = metrics[primary]
    for name in ("f1", "precision", "recall", "positive_median_rank"):
        if candidate.get(name) is None or champion.get(name) is None:
            reasons.append("insufficient_validation_metric_support")
            return False, tuple(reasons)
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
        other_candidate, other_champion = metrics[(outcome, "TOP_20", "end_to_end")]
        if other_candidate.get("f1") is None or other_champion.get("f1") is None:
            reasons.append("insufficient_validation_metric_support")
        elif Decimal(str(other_candidate["f1"])) < Decimal(str(other_champion["f1"])):
            reasons.append(f"{outcome.lower()}_f1_guardrail_failed")
    if candidate.get("rankability_delta") != 0 or candidate.get("coverage_delta") != 0:
        reasons.append("rankability_or_coverage_changed")
    return not reasons, tuple(dict.fromkeys(reasons))


def select_validation_challenger(
    candidates: Sequence[
        tuple[str, Mapping[tuple[str, str, str], tuple[Mapping[str, object], Mapping[str, object]]]]
    ],
) -> tuple[str | None, dict[str, tuple[str, ...]]]:
    assessments: dict[str, tuple[str, ...]] = {}
    passing: list[tuple[str, Mapping[str, object]]] = []
    primary = ("MB_3X_3Y", "TOP_20", "end_to_end")
    for code, cells in candidates:
        accepted, reasons = validation_gate(cells)
        assessments[code] = reasons
        if accepted:
            passing.append((code, cells[primary][0]))
    passing.sort(
        key=lambda item: (
            -Decimal(str(item[1]["f1"])),
            -Decimal(str(item[1]["precision"])),
            -Decimal(str(item[1]["recall"])),
            Decimal(str(item[1]["positive_median_rank"])),
            item[0],
        )
    )
    return (passing[0][0] if passing else None), assessments


def challenger_readiness(
    session: Session | None,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    source_evaluation_run_id: UUID,
    session_calendar_run_id: UUID,
    temporal_split_run_id: UUID,
    error_analysis_run_id: UUID,
    database_available: bool = True,
) -> dict[str, object]:
    """Verify one coherent persisted S1/R/champion chain; no status string is authority."""

    if not database_available or session is None:
        return {
            "status": "environment_blocked",
            "message": "REAL S2 CHALLENGER SELECTION: ENVIRONMENT-BLOCKED",
            "blockers": ["production_database_unavailable"],
        }
    blockers: list[str] = []
    if (
        policy.code != "production_multibagger_challenger_development_v1"
        or policy.checksum_sha256 != S2_POLICY_CHECKSUM
    ):
        blockers.append("accepted_s2_policy_unavailable")
    if (
        activation_policy.checksum_sha256
        != policy.inherited_checksums[
            "historical_evaluation_activation_policy_checksum_sha256"
        ]
        or split_policy.checksum_sha256
        != policy.inherited_checksums["temporal_split_policy_checksum_sha256"]
    ):
        blockers.append("accepted_s1_policy_binding_unavailable")
    s1 = historical_evaluation_readiness(
        session,
        policy=activation_policy,
        split_policy=split_policy,
        source_evaluation_run_id=source_evaluation_run_id,
        session_calendar_run_id=session_calendar_run_id,
    )
    if s1.get("status") != "READY":
        blockers.append("accepted_s1_historical_readiness_unavailable")

    split = session.get(MultibaggerTemporalSplitRun, temporal_split_run_id)
    if (
        split is None
        or split.status != "completed"
        or split.split_policy_code != "production_multibagger_temporal_split_v1"
        or split.split_policy_checksum_sha256
        != policy.inherited_checksums["temporal_split_policy_checksum_sha256"]
        or split.source_evaluation_run_id != source_evaluation_run_id
        or split.session_calendar_run_id != session_calendar_run_id
    ):
        blockers.append("accepted_s1_temporal_split_unavailable")
    else:
        cutoff_rows = tuple(
            session.scalars(
                select(MultibaggerTemporalSplitCutoff).where(
                    MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id == split.id
                )
            )
        )
        counts = {
            code: sum(row.split_code == code for row in cutoff_rows)
            for code in ("development", "validation", "holdout")
        }
        if (
            counts["development"] == 0
            or counts["validation"] < split_policy.minimum_validation_cutoffs
            or counts["holdout"] < split_policy.minimum_holdout_cutoffs
        ):
            blockers.append("s1_temporal_split_support_insufficient")
        persisted_ids = {
            code: sorted(
                str(row.multibagger_evaluation_cohort_id)
                for row in cutoff_rows
                if row.split_code == code
            )
            for code in ("development", "validation", "holdout")
        }
        split_json = {
            "development": sorted(str(value) for value in split.development_cutoffs_json),
            "validation": sorted(str(value) for value in split.validation_cutoffs_json),
            "holdout": sorted(str(value) for value in split.holdout_cutoffs_json),
        }
        # The split JSON contains cutoff timestamps while rows bind cohort IDs; both
        # representations must have the exact same cardinality and no duplicates.
        if any(
            len(persisted_ids[code]) != len(split_json[code])
            or len(persisted_ids[code]) != len(set(persisted_ids[code]))
            for code in persisted_ids
        ):
            blockers.append("s1_temporal_split_assignment_conflict")

    analysis = session.get(MultibaggerErrorAnalysisRun, error_analysis_run_id)
    if (
        analysis is None
        or analysis.status != "completed"
        or analysis.error_policy_code != "production_multibagger_error_analysis_v1"
        or analysis.error_policy_checksum_sha256
        != policy.inherited_checksums["error_analysis_policy_checksum_sha256"]
        or analysis.temporal_split_run_id != temporal_split_run_id
        or analysis.source_evaluation_run_id != source_evaluation_run_id
        or analysis.holdout_outcomes_accessed
    ):
        blockers.append("accepted_s1_error_analysis_unavailable")

    evaluation = session.get(MultibaggerEvaluationRun, source_evaluation_run_id)
    if (
        evaluation is None
        or evaluation.status != "completed"
        or evaluation.evaluation_policy_checksum_sha256
        != policy.inherited_checksums["multibagger_evaluation_policy_checksum_sha256"]
        or evaluation.scoring_configuration_checksum_sha256 != CHAMPION_CHECKSUM
    ):
        blockers.append("accepted_production_r_evaluation_unavailable")
    configs = tuple(
        session.scalars(
            select(ScoringConfiguration).where(
                ScoringConfiguration.checksum_sha256 == CHAMPION_CHECKSUM
            )
        )
    )
    if len(configs) != 1:
        blockers.append("accepted_champion_scoring_configuration_unavailable")

    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return {
            "status": "data_blocked",
            "message": "REAL S2 CHALLENGER SELECTION: DATA-BLOCKED",
            "blockers": blockers,
        }
    return {"status": "ready", "message": "REAL S2 CHALLENGER SELECTION: READY", "blockers": []}


def freeze_challenger_for_t(
    session: Session,
    *,
    policy: MultibaggerChallengerDevelopmentPolicy,
    experiment_id: UUID,
) -> MultibaggerChallengerFreeze | None:
    experiment = _experiment(session, experiment_id, policy)
    if experiment.status != "validation_evaluated":
        raise ChallengerIntegrityError("completed validation evaluation is required")
    split = session.get(MultibaggerTemporalSplitRun, experiment.temporal_split_run_id)
    if split is None:
        raise ChallengerIntegrityError("bound S1 temporal split is unavailable")
    root = Path(__file__).resolve().parents[3]
    activation_policy = load_historical_evaluation_activation_policy(
        root / "config/backtest/production_historical_evaluation_activation_v1.json",
        repository_root=root,
    )
    split_policy = load_multibagger_temporal_split_policy(
        root / "config/backtest/production_multibagger_temporal_split_v1.json"
    )
    readiness = challenger_readiness(
        session,
        policy=policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
        source_evaluation_run_id=experiment.source_evaluation_run_id,
        session_calendar_run_id=split.session_calendar_run_id,
        temporal_split_run_id=split.id,
        error_analysis_run_id=experiment.error_analysis_run_id,
    )
    if experiment.evidence_class != "production_authoritative" or readiness["status"] != "ready":
        raise ChallengerIntegrityError(
            "fixture or blocked evidence cannot freeze a candidate for T"
        )
    candidates = _candidates(session, experiment.id)
    comparisons = [
        (item.candidate_code, _metric_cells(session, item.id, "validation")) for item in candidates
    ]
    selected_code, assessments = select_validation_challenger(comparisons)
    if selected_code is None:
        experiment.status = "no_challenger_selected"
        experiment.summary_json = {"validation_gate": assessments}
        session.commit()
        return None
    candidate = next(item for item in candidates if item.candidate_code == selected_code)
    key = canonical_json_sha256(
        {
            "experiment_run_key_sha256": experiment.run_key_sha256,
            "candidate_key_sha256": candidate.candidate_key_sha256,
            "purpose": "candidate_for_production_t",
        }
    )
    freeze = MultibaggerChallengerFreeze(
        experiment_run_id=experiment.id,
        candidate_id=candidate.id,
        freeze_key_sha256=key,
        freeze_purpose="candidate_for_production_t",
        production_readiness_status="ready",
        validation_gate_json={"selected_candidate_code": selected_code, "assessments": assessments},
    )
    session.add(freeze)
    candidate.status = "eligible_for_t"
    experiment.status = "candidate_frozen_for_t"
    session.commit()
    return freeze


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _validate_projection(
    persisted: object,
    expected: Mapping[str, object],
    *,
    label: str,
) -> None:
    actual = {field: getattr(persisted, field) for field in expected}
    if actual != dict(expected):
        raise ChallengerIntegrityError(f"existing {label} conflicts with immutable projection")


def _validate_existing_score(
    persisted: MultibaggerChallengerScore, expected: Mapping[str, object]
) -> None:
    _validate_projection(persisted, expected, label="candidate score")


def _validate_existing_evaluation_row(
    persisted: MultibaggerChallengerEvaluationRow, expected: Mapping[str, object]
) -> None:
    _validate_projection(persisted, expected, label="candidate evaluation row")


def _validate_existing_metric(
    persisted: MultibaggerChallengerMetric, expected: Mapping[str, object]
) -> None:
    _validate_projection(persisted, expected, label="candidate metric")


def _validate_prediction_fingerprint(prediction: MultibaggerPredictionRow) -> None:
    projection = {
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
    if canonical_json_sha256(_jsonable(projection)) != prediction.prediction_fingerprint_sha256:
        raise ChallengerIntegrityError("frozen R prediction fingerprint conflicts")


def _candidate_score_for_prediction(
    session: Session,
    *,
    experiment: MultibaggerChallengerExperimentRun,
    prediction: MultibaggerPredictionRow,
    weights: CandidateWeights,
) -> tuple[Decimal | None, str]:
    _validate_prediction_fingerprint(prediction)
    observation = session.get(BacktestObservation, prediction.backtest_observation_id)
    if observation is None:
        raise ChallengerIntegrityError("frozen backtest observation is missing")
    if (
        observation.company_id != prediction.company_id
        or observation.security_id != prediction.security_id
        or observation.symbol != prediction.historical_symbol
        or observation.score_snapshot_id != prediction.score_snapshot_id
        or observation.snapshot_fingerprint_sha256 != prediction.snapshot_fingerprint_sha256
        or observation.snapshot_status != prediction.snapshot_status
    ):
        raise ChallengerIntegrityError("frozen R prediction and J observation disagree")
    if prediction.score_snapshot_id is None:
        if (
            prediction.rankable
            or prediction.final_score is not None
            or prediction.unranked_reason not in {"issuer_unavailable", "research_failed"}
        ):
            raise ChallengerIntegrityError("champion no-snapshot state is inconsistent")
        checksum = canonical_json_sha256(
            {
                "prediction_fingerprint": prediction.prediction_fingerprint_sha256,
                "snapshot_fingerprint": None,
                "rankable": False,
            }
        )
        return None, checksum
    if prediction.snapshot_fingerprint_sha256 is None:
        raise ChallengerIntegrityError("frozen prediction lacks its snapshot fingerprint")
    snapshot = session.get(ScoreSnapshot, prediction.score_snapshot_id)
    if snapshot is None:
        raise ChallengerIntegrityError("frozen score snapshot is missing")
    try:
        ScoreSnapshotRepository(session).validate_persisted_snapshot(snapshot)
    except ScoreSnapshotIntegrityError as error:
        raise ChallengerIntegrityError(
            "frozen score snapshot failed integrity validation"
        ) from error
    expected_rankable = bool(
        snapshot.eligibility_eligible
        and snapshot.final_score is not None
        and snapshot.snapshot_status == "final_score_available"
    )
    if (
        snapshot.algorithm_version != "score_snapshot_v5"
        or snapshot.selected_security_id != prediction.security_id
        or snapshot.snapshot_fingerprint_sha256 != prediction.snapshot_fingerprint_sha256
        or snapshot.configuration_checksum_sha256 != experiment.champion_scoring_checksum_sha256
        or snapshot.scoring_configuration_id != experiment.champion_scoring_configuration_id
        or _persisted_utc(snapshot.knowledge_cutoff)
        != _persisted_utc(observation.knowledge_cutoff)
        or snapshot.snapshot_status != prediction.snapshot_status
        or expected_rankable != prediction.rankable
        or (prediction.rankable and snapshot.final_score != prediction.final_score)
    ):
        raise ChallengerIntegrityError("frozen champion snapshot binding mismatch")
    component_rows = sorted(snapshot.components, key=lambda item: item.component_code)
    component_projection = [
        {
            "component_code": item.component_code,
            "score": str(item.score) if item.score is not None else None,
            "algorithm_version": item.algorithm_version,
        }
        for item in component_rows
    ]
    checksum = canonical_json_sha256(component_projection)
    if not prediction.rankable:
        if prediction.final_score is not None or prediction.unranked_reason is None:
            raise ChallengerIntegrityError("champion unrankable state is inconsistent")
        return None, checksum
    scores = {item.component_code: item.score for item in component_rows}
    if set(scores) != set(COMPONENT_CODES) or any(value is None for value in scores.values()):
        raise ChallengerIntegrityError("rankable snapshot has incomplete frozen components")
    decimal_scores = {key: value for key, value in scores.items() if value is not None}
    return counterfactual_candidate_score(weights, decimal_scores), checksum


def _persist_metrics(
    session: Session,
    *,
    experiment: MultibaggerChallengerExperimentRun,
    candidates: Sequence[MultibaggerChallengerCandidate],
    predictions: Sequence[MultibaggerPredictionRow],
    split_code: str,
) -> None:
    prediction_by_id = {item.id: item for item in predictions}
    for candidate in candidates:
        candidate_rows = tuple(
            session.scalars(
                select(MultibaggerChallengerEvaluationRow).where(
                    MultibaggerChallengerEvaluationRow.candidate_id == candidate.id,
                    MultibaggerChallengerEvaluationRow.split_code == split_code,
                )
            )
        )
        score_rows = tuple(
            session.scalars(
                select(MultibaggerChallengerScore).where(
                    MultibaggerChallengerScore.candidate_id == candidate.id,
                    MultibaggerChallengerScore.split_code == split_code,
                )
            )
        )
        score_by_id = {item.id: item for item in score_rows}
        for outcome in OUTCOME_CONTRACTS:
            for selection in SELECTION_CONTRACTS:
                for view in EVALUATION_VIEWS:
                    rows = [
                        item
                        for item in candidate_rows
                        if item.outcome_contract_code == outcome
                        and item.selection_contract_code == selection
                        and item.evaluation_view == view
                    ]
                    candidate_metric = _metrics(rows, score_by_id)
                    champion_rows = tuple(
                        session.scalars(
                            select(MultibaggerConfusionRow).where(
                                MultibaggerConfusionRow.multibagger_prediction_row_id.in_(
                                    tuple(prediction_by_id)
                                ),
                                MultibaggerConfusionRow.outcome_contract_code == outcome,
                                MultibaggerConfusionRow.selection_contract_code == selection,
                                MultibaggerConfusionRow.evaluation_view == view,
                            )
                        )
                    )
                    champion_metric = _champion_metrics(champion_rows, prediction_by_id, view)
                    candidate_selected = _persisted_string_set(
                        candidate_metric.pop("selected_prediction_ids")
                    )
                    champion_selected = _persisted_string_set(
                        champion_metric.pop("selected_prediction_ids")
                    )
                    candidate_metric.update(
                        {
                            "selected_set_overlap": len(candidate_selected & champion_selected),
                            "selected_set_additions": sorted(
                                candidate_selected - champion_selected
                            ),
                            "selected_set_removals": sorted(champion_selected - candidate_selected),
                        }
                    )
                    champion_metric["selected_set_overlap"] = len(
                        candidate_selected & champion_selected
                    )
                    candidate_metric["rankability_delta"] = 0
                    candidate_metric["coverage_delta"] = 0
                    deltas = {
                        name: _delta(candidate_metric.get(name), champion_metric.get(name))
                        for name in ("precision", "recall", "f1", "positive_median_rank")
                    }
                    projection = {
                        "candidate_key": candidate.candidate_key_sha256,
                        "split": split_code,
                        "outcome": outcome,
                        "selection": selection,
                        "view": view,
                        "candidate": candidate_metric,
                        "champion": champion_metric,
                        "delta": deltas,
                        "rankability_delta": 0,
                        "coverage_delta": 0,
                    }
                    metric_values: dict[str, object] = {
                        "experiment_run_id": experiment.id,
                        "candidate_id": candidate.id,
                        "split_code": split_code,
                        "outcome_contract_code": outcome,
                        "selection_contract_code": selection,
                        "evaluation_view": view,
                        "candidate_metrics_json": candidate_metric,
                        "champion_metrics_json": champion_metric,
                        "delta_metrics_json": deltas,
                        "rankability_delta": 0,
                        "coverage_delta": 0,
                        "metric_fingerprint_sha256": canonical_json_sha256(projection),
                    }
                    existing = session.scalar(
                        select(MultibaggerChallengerMetric).where(
                            MultibaggerChallengerMetric.candidate_id == candidate.id,
                            MultibaggerChallengerMetric.split_code == split_code,
                            MultibaggerChallengerMetric.outcome_contract_code == outcome,
                            MultibaggerChallengerMetric.selection_contract_code == selection,
                            MultibaggerChallengerMetric.evaluation_view == view,
                        )
                    )
                    if existing is None:
                        session.add(MultibaggerChallengerMetric(**metric_values))
                    else:
                        _validate_existing_metric(existing, metric_values)
    session.flush()


def _metrics(
    rows: Sequence[MultibaggerChallengerEvaluationRow],
    scores: Mapping[UUID, MultibaggerChallengerScore],
) -> dict[str, object]:
    view_rows = [
        row
        for row in rows
        if row.evaluation_view == "end_to_end" or scores[row.candidate_score_id].rankable
    ]
    counts = {
        code: sum(row.confusion_class == code for row in view_rows)
        for code in ("tp", "fp", "fn", "tn")
    }
    positive_ranks: list[int] = []
    for row in view_rows:
        rank = scores[row.candidate_score_id].display_order
        if row.actual_label == "positive" and rank is not None:
            positive_ranks.append(rank)
    positive_ranks.sort()
    selected_ids = {
        scores[row.candidate_score_id].source_prediction_row_id for row in view_rows if row.selected
    }
    return {
        **counts,
        "precision": _decimal(_ratio(counts["tp"], counts["tp"] + counts["fp"])),
        "recall": _decimal(_ratio(counts["tp"], counts["tp"] + counts["fn"])),
        "f1": _decimal(_f1(counts["tp"], counts["fp"], counts["fn"])),
        "selected_count": sum(row.selected for row in view_rows),
        "actual_positive_count": counts["tp"] + counts["fn"],
        "actual_negative_count": counts["fp"] + counts["tn"],
        "rankable_count": sum(scores[row.candidate_score_id].rankable for row in view_rows),
        "unrankable_count": sum(not scores[row.candidate_score_id].rankable for row in view_rows),
        "prevalence": _decimal(_ratio(counts["tp"] + counts["fn"], sum(counts.values()))),
        "positive_mean_rank": _decimal(_mean(positive_ranks)),
        "positive_median_rank": _decimal(_median_decimal(positive_ranks)),
        "positive_best_rank": min(positive_ranks) if positive_ranks else None,
        "positive_worst_rank": max(positive_ranks) if positive_ranks else None,
        "selected_prediction_ids": sorted(str(item) for item in selected_ids),
        "rankability_delta": 0,
        "coverage_delta": 0,
    }


def _champion_metrics(
    rows: Sequence[MultibaggerConfusionRow],
    predictions: Mapping[UUID, MultibaggerPredictionRow],
    view: str,
) -> dict[str, object]:
    view_rows = [
        row
        for row in rows
        if view == "end_to_end" or predictions[row.multibagger_prediction_row_id].rankable
    ]
    counts = {
        code: sum(row.confusion_class == code for row in view_rows)
        for code in ("tp", "fp", "fn", "tn")
    }
    ranks: list[int] = []
    for row in view_rows:
        rank = predictions[row.multibagger_prediction_row_id].display_order
        if row.actual_label == "positive" and rank is not None:
            ranks.append(rank)
    ranks.sort()
    selected_ids = {row.multibagger_prediction_row_id for row in view_rows if row.selected}
    return {
        **counts,
        "precision": _decimal(_ratio(counts["tp"], counts["tp"] + counts["fp"])),
        "recall": _decimal(_ratio(counts["tp"], counts["tp"] + counts["fn"])),
        "f1": _decimal(_f1(counts["tp"], counts["fp"], counts["fn"])),
        "selected_count": sum(row.selected for row in view_rows),
        "actual_positive_count": counts["tp"] + counts["fn"],
        "actual_negative_count": counts["fp"] + counts["tn"],
        "rankable_count": sum(
            predictions[row.multibagger_prediction_row_id].rankable for row in view_rows
        ),
        "unrankable_count": sum(
            not predictions[row.multibagger_prediction_row_id].rankable for row in view_rows
        ),
        "prevalence": _decimal(_ratio(counts["tp"] + counts["fn"], sum(counts.values()))),
        "positive_mean_rank": _decimal(_mean(ranks)),
        "positive_median_rank": _decimal(_median_decimal(ranks)),
        "positive_best_rank": min(ranks) if ranks else None,
        "positive_worst_rank": max(ranks) if ranks else None,
        "selected_prediction_ids": sorted(str(item) for item in selected_ids),
        "rankability_delta": 0,
        "coverage_delta": 0,
    }


def _metric_cells(
    session: Session, candidate_id: UUID, split_code: str
) -> dict[tuple[str, str, str], tuple[Mapping[str, object], Mapping[str, object]]]:
    rows = tuple(
        session.scalars(
            select(MultibaggerChallengerMetric).where(
                MultibaggerChallengerMetric.candidate_id == candidate_id,
                MultibaggerChallengerMetric.split_code == split_code,
            )
        )
    )
    return {
        (item.outcome_contract_code, item.selection_contract_code, item.evaluation_view): (
            item.candidate_metrics_json,
            item.champion_metrics_json,
        )
        for item in rows
    }


def _weights_from_candidate(candidate: MultibaggerChallengerCandidate) -> CandidateWeights:
    mapping = {
        str(item["component_code"]): str(item["weight"])
        for item in candidate.ordered_weights_json
        if isinstance(item, dict)
    }
    parsed = parse_candidate_weights(mapping)
    if parsed.checksum_sha256 != candidate.candidate_checksum_sha256:
        raise ChallengerIntegrityError("persisted candidate weight checksum mismatch")
    return parsed


def _experiment(
    session: Session, experiment_id: UUID, policy: MultibaggerChallengerDevelopmentPolicy
) -> MultibaggerChallengerExperimentRun:
    experiment = session.get(MultibaggerChallengerExperimentRun, experiment_id)
    if experiment is None:
        raise ChallengerIntegrityError("challenger experiment does not exist")
    if (
        experiment.policy_code != policy.code
        or experiment.policy_checksum_sha256 != policy.checksum_sha256
    ):
        raise ChallengerIntegrityError("challenger experiment policy binding mismatch")
    if experiment.holdout_outcomes_accessed:
        raise ChallengerIntegrityError("challenger experiment holdout firewall was violated")
    return experiment


def _candidates(
    session: Session, experiment_id: UUID
) -> tuple[MultibaggerChallengerCandidate, ...]:
    return tuple(
        session.scalars(
            select(MultibaggerChallengerCandidate)
            .where(MultibaggerChallengerCandidate.experiment_run_id == experiment_id)
            .order_by(MultibaggerChallengerCandidate.candidate_code)
        )
    )


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    return None if denominator == 0 else Decimal(numerator) / Decimal(denominator)


def _f1(tp: int, fp: int, fn: int) -> Decimal | None:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    if precision is None or recall is None or precision + recall == 0:
        return None
    return Decimal("2") * precision * recall / (precision + recall)


def _mean(values: Sequence[int]) -> Decimal | None:
    return (
        None
        if not values
        else sum((Decimal(item) for item in values), Decimal("0")) / Decimal(len(values))
    )


def _median_decimal(values: Sequence[int]) -> Decimal | None:
    if not values:
        return None
    result = median([Decimal(item) for item in values])
    return result if isinstance(result, Decimal) else Decimal(str(result))


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _delta(candidate: object, champion: object) -> str | None:
    if candidate is None or champion is None:
        return None
    return str(Decimal(str(candidate)) - Decimal(str(champion)))


def _persisted_string_set(value: object) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ChallengerIntegrityError("persisted selected prediction identities are malformed")
    return set(value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return value.astimezone(UTC)


def _persisted_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite drops otherwise-recorded UTC tzinfo."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
