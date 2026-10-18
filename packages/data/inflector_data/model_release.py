"""Production U model release governance, shadow burn-in, activation, and rollback."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from inflector_core.scoring_policy import scoring_policy_checksum
from inflector_data.historical_activation_policy import (
    HistoricalEvaluationActivationPolicy,
    MultibaggerTemporalSplitPolicy,
)
from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.model_release_policy import (
    CHAMPION_CHECKSUM,
    POLICY_CODE,
    T_CHECKSUM,
    ModelReleasePolicy,
)
from inflector_data.multibagger_challenger import (
    COMPONENT_CODES,
    _weights_from_candidate,
    counterfactual_candidate_score,
)
from inflector_data.multibagger_challenger_policy import MultibaggerChallengerDevelopmentPolicy
from inflector_data.multibagger_holdout import (
    PRODUCTION_EVIDENCE,
    _holdout_run,
    _validate_completed_holdout_graph,
    _validated_run_input,
)
from inflector_data.multibagger_holdout_policy import MultibaggerTemporalHoldoutPolicy
from inflector_data.production_health import production_health
from inflector_data.production_policy import (
    ProductionPolicyBindingError,
    bind_production_policy,
)
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    ActiveModelRelease,
    ModelActivationApproval,
    ModelBurnInRun,
    ModelDeployment,
    ModelReleaseCandidate,
    ModelReleaseCycleBinding,
    ModelReleaseProductionScore,
    ModelRollback,
    ModelShadowEvent,
    ModelShadowRun,
    MultibaggerChallengerCandidate,
    MultibaggerHoldoutDecision,
    MultibaggerHoldoutEvaluationRun,
    OperationalRun,
    ScoreSnapshot,
    ScoringConfiguration,
)
from inflector_database.score_repository import (
    ScoreSnapshotIntegrityError,
    ScoreSnapshotRepository,
)
from inflector_database.scoring_repository import (
    ScoringPolicyIntegrityError,
    ScoringPolicyRepository,
)

ACTIVE_POINTER = "production"
CHAMPION_EVIDENCE = "accepted_v5_champion"
PRODUCTION_EVIDENCE_CLASS = "production_authoritative"
RESEARCH_PROFILE_CODE = "nse_current_research_v4"
RESEARCH_PROFILE_VERSION = "4"
RESEARCH_PROFILE_CHECKSUM = "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
RESEARCH_PROFILE_PATH = Path("config/research/production_research_v4.json")
V5_ASSET_PATH = Path("config/scoring/production_opportunity_v5.json")


class ModelReleaseIntegrityError(RuntimeError):
    """A Production U immutable binding or state transition is invalid."""


@dataclass(frozen=True, slots=True)
class CycleReleaseContext:
    """One validated release identity frozen for an entire operational cycle."""

    binding: ModelReleaseCycleBinding
    deployment: ModelDeployment
    candidate: ModelReleaseCandidate


def create_champion_release_candidate(
    session: Session, *, policy: ModelReleasePolicy
) -> ModelReleaseCandidate:
    """Resolve the accepted V5 champion without claiming historical T completion."""

    config = _accepted_champion_configuration(session, policy)
    definition: dict[str, object] = {
        **_champion_runtime_definition(config, policy=policy),
        "source": "accepted_v5_champion",
        "algorithm_version": "score_snapshot_v5",
    }
    return _persist_release_candidate(
        session,
        policy=policy,
        subject_code="champion",
        evidence_class=CHAMPION_EVIDENCE,
        config=config,
        definition=definition,
        t_run=None,
        t_decision=None,
        candidate=None,
        freeze_id=None,
        freeze_key=None,
        experiment_id=None,
        experiment_key=None,
    )


def create_challenger_release_candidate(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    holdout_run_id: UUID,
) -> ModelReleaseCandidate:
    """Resolve exactly one challenger only from a fully validated authoritative T pass."""

    run, decision, graph = _authoritative_t_pass(
        session,
        policy=policy,
        t_policy=t_policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
        holdout_run_id=holdout_run_id,
    )
    candidate = graph.candidate
    freeze = graph.freeze
    if candidate is None or freeze is None:
        raise ModelReleaseIntegrityError("authoritative T pass lacks its frozen challenger")
    runtime_config = _accepted_champion_configuration(session, policy)
    definition = {
        **_champion_runtime_definition(runtime_config, policy=policy),
        "source": "authoritative_completed_t",
        "algorithm_version": candidate.algorithm_version,
        "candidate_code": candidate.candidate_code,
        "candidate_key_sha256": candidate.candidate_key_sha256,
        "candidate_checksum_sha256": candidate.candidate_checksum_sha256,
        "development_evidence_checksum_sha256": (candidate.development_evidence_checksum_sha256),
        "ordered_weights": candidate.ordered_weights_json,
        "hypothesis": candidate.hypothesis,
    }
    return _persist_release_candidate(
        session,
        policy=policy,
        subject_code="challenger",
        evidence_class=PRODUCTION_EVIDENCE_CLASS,
        config=runtime_config,
        definition=definition,
        t_run=run,
        t_decision=decision,
        candidate=candidate,
        freeze_id=freeze.id,
        freeze_key=freeze.freeze_key_sha256,
        experiment_id=graph.experiment.id,
        experiment_key=graph.experiment.run_key_sha256,
    )


def bootstrap_active_champion(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    activated_at: datetime,
) -> ModelDeployment:
    """Idempotently establish V5 as the active target without fabricating a ModelVersion."""

    activated = _aware(activated_at, "activated_at")
    candidate = create_champion_release_candidate(session, policy=policy)
    pointer = _locked_pointer(session)
    if pointer is not None:
        deployment = _validate_active_pointer(session, pointer, policy)
        if deployment.release_candidate_id != candidate.id or deployment.subject_code != "champion":
            raise ModelReleaseIntegrityError("an active release already exists and is not V5")
        return deployment
    values = _deployment_values(
        policy=policy,
        candidate=candidate,
        previous=None,
        approval_id=None,
        reason="accepted_v5_champion_bootstrap",
        activated_at=activated,
    )
    deployment = _persist_projection(
        session, ModelDeployment, "deployment_key_sha256", values, "deployment"
    )
    pointer = ActiveModelRelease(
        pointer_name=ACTIVE_POINTER,
        deployment_id=deployment.id,
        release_candidate_id=candidate.id,
        subject_code="champion",
        scoring_definition_checksum_sha256=candidate.scoring_definition_checksum_sha256,
        pointer_version=1,
        updated_at=activated,
    )
    session.add(pointer)
    candidate.status = "active"
    session.commit()
    return deployment


def resolve_active_release(
    session: Session, *, policy: ModelReleasePolicy
) -> tuple[ActiveModelRelease, ModelDeployment, ModelReleaseCandidate]:
    pointer = session.get(ActiveModelRelease, ACTIVE_POINTER)
    if pointer is None:
        raise ModelReleaseIntegrityError("active production release is not bootstrapped")
    deployment = _validate_active_pointer(session, pointer, policy)
    candidate = _required_candidate(session, deployment.release_candidate_id)
    return pointer, deployment, candidate


def start_shadow_run(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    release_candidate_id: UUID,
    started_at: datetime,
) -> ModelShadowRun:
    started = _aware(started_at, "started_at")
    candidate = _required_candidate(session, release_candidate_id)
    _validate_release_candidate(session, candidate, policy=policy)
    if (
        candidate.subject_code != "challenger"
        or candidate.evidence_class != PRODUCTION_EVIDENCE_CLASS
    ):
        raise ModelReleaseIntegrityError("shadow mode requires an authoritative challenger")
    if candidate.status not in {"candidate_created", "shadow_ready"}:
        raise ModelReleaseIntegrityError("challenger is not eligible to start shadow")
    _revalidate_candidate_t(
        session,
        candidate=candidate,
        policy=policy,
        t_policy=t_policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
    )
    _, deployment, active = resolve_active_release(session, policy=policy)
    if active.subject_code != "champion":
        raise ModelReleaseIntegrityError("shadow must begin while the champion is active")
    identity = {
        "policy_checksum_sha256": policy.checksum_sha256,
        "release_candidate_key_sha256": candidate.release_candidate_key_sha256,
        "active_champion_deployment_key_sha256": deployment.deployment_key_sha256,
        "started_at": started.isoformat(),
        "requirements": policy.burn_in_requirements,
    }
    key = canonical_json_sha256(identity)
    prior_run = session.scalar(
        select(ModelShadowRun).where(ModelShadowRun.release_candidate_id == candidate.id)
    )
    if prior_run is not None and prior_run.shadow_run_key_sha256 != key:
        raise ModelReleaseIntegrityError("challenger already has a different shadow run")
    values: dict[str, object] = {
        "shadow_run_key_sha256": key,
        "release_candidate_id": candidate.id,
        "policy_checksum_sha256": policy.checksum_sha256,
        "active_champion_deployment_id": deployment.id,
        "requirements_json": policy.burn_in_requirements,
        "started_at": started,
        "status": "shadow_ready",
        "completed_at": None,
    }
    run = _persist_projection(
        session, ModelShadowRun, "shadow_run_key_sha256", values, "shadow run"
    )
    if candidate.status == "candidate_created":
        candidate.status = "shadow_ready"
    session.commit()
    return run


def record_shadow_cycle(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    shadow_run_id: UUID,
    cycle_key: str,
    snapshot_id: UUID,
    observed_at: datetime,
    latency_ms: Decimal,
    scheduler_failure: bool = False,
    production_health_regression: bool = False,
    production_notification_emitted: bool = False,
) -> ModelShadowEvent:
    """Persist same-source V5/challenger scoring without entering production outputs."""

    observed = _aware(observed_at, "observed_at")
    if not cycle_key.strip() or not isinstance(latency_ms, Decimal) or latency_ms < 0:
        raise ValueError("shadow cycle identity and exact non-negative latency are required")
    run = session.get(ModelShadowRun, shadow_run_id)
    if run is None or run.status not in {"shadow_ready", "shadow_running"}:
        raise ModelReleaseIntegrityError("shadow run is not open")
    if observed < _stored_utc(run.started_at):
        raise ModelReleaseIntegrityError("shadow event precedes its burn-in window")
    candidate = _required_candidate(session, run.release_candidate_id)
    _validate_release_candidate(session, candidate, policy=policy)
    _, active_deployment, active_candidate = resolve_active_release(session, policy=policy)
    if (
        active_deployment.id != run.active_champion_deployment_id
        or active_candidate.subject_code != "champion"
    ):
        raise ModelReleaseIntegrityError("shadow event no longer shares the active champion")
    snapshot = session.get(ScoreSnapshot, snapshot_id)
    if snapshot is None:
        raise ModelReleaseIntegrityError("shadow source snapshot is unavailable")
    try:
        ScoreSnapshotRepository(session).validate_persisted_snapshot(snapshot)
    except ScoreSnapshotIntegrityError as error:
        raise ModelReleaseIntegrityError("shadow source snapshot failed integrity") from error
    if (
        snapshot.algorithm_version != "score_snapshot_v5"
        or snapshot.scoring_configuration_id != candidate.champion_scoring_configuration_id
        or snapshot.configuration_checksum_sha256
        != candidate.scoring_definition_json.get(
            "bound_scoring_configuration_checksum_sha256"
        )
        or snapshot.selected_security_id is None
    ):
        raise ModelReleaseIntegrityError("shadow snapshot is not the accepted frozen V5 state")
    components = sorted(snapshot.components, key=lambda item: item.component_code)
    projection = [
        {
            "component_code": item.component_code,
            "score": str(item.score) if item.score is not None else None,
            "algorithm_version": item.algorithm_version,
        }
        for item in components
    ]
    component_checksum = canonical_json_sha256(projection)
    champion_rankable = bool(
        snapshot.eligibility_eligible
        and snapshot.snapshot_status == "final_score_available"
        and snapshot.final_score is not None
    )
    scores = {item.component_code: item.score for item in components}
    complete = set(scores) == set(COMPONENT_CODES) and all(
        item is not None for item in scores.values()
    )
    missing = champion_rankable and not complete
    if missing:
        raise ModelReleaseIntegrityError("rankable V5 shadow state lacks required components")
    challenger_score: Decimal | None = None
    if champion_rankable:
        s2_candidate = _required_s2_candidate(session, candidate)
        weights = _weights_from_candidate(s2_candidate)
        challenger_score = counterfactual_candidate_score(
            weights, {code: cast(Decimal, scores[code]) for code in COMPONENT_CODES}
        )
    challenger_rankable = champion_rankable
    if production_notification_emitted:
        raise ModelReleaseIntegrityError("shadow output may not emit a production notification")
    identity = {
        "shadow_run_key_sha256": run.shadow_run_key_sha256,
        "cycle_key": cycle_key,
        "security_id": str(snapshot.selected_security_id),
        "evidence_cutoff": _stored_utc(snapshot.knowledge_cutoff).isoformat(),
        "snapshot_fingerprint_sha256": snapshot.snapshot_fingerprint_sha256,
        "component_state_checksum_sha256": component_checksum,
        "candidate_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
    }
    key = canonical_json_sha256(identity)
    values: dict[str, object] = {
        "shadow_run_id": run.id,
        "shadow_event_key_sha256": key,
        "cycle_key": cycle_key,
        "security_id": snapshot.selected_security_id,
        "evidence_cutoff": _stored_utc(snapshot.knowledge_cutoff),
        "source_score_snapshot_id": snapshot.id,
        "source_snapshot_fingerprint_sha256": snapshot.snapshot_fingerprint_sha256,
        "component_state_checksum_sha256": component_checksum,
        "champion_score": snapshot.final_score if champion_rankable else None,
        "challenger_score": challenger_score,
        "champion_rankable": champion_rankable,
        "challenger_rankable": challenger_rankable,
        "rankability_delta": 0,
        "coverage_delta": 0,
        "latency_ms": latency_ms,
        "integrity_violation": False,
        "score_definition_drift": False,
        "missing_required_components": missing,
        "scheduler_failure": scheduler_failure,
        "production_health_regression": production_health_regression,
        "production_notification_emitted": False,
        "observed_at": observed,
    }
    values["event_fingerprint_sha256"] = canonical_json_sha256(
        {"identity": identity, "projection": _jsonable(values)}
    )
    event = _persist_projection(
        session, ModelShadowEvent, "shadow_event_key_sha256", values, "shadow event"
    )
    if run.status == "shadow_ready":
        run.status = "shadow_running"
        candidate.status = "shadow_running"
    session.commit()
    return event


def complete_burn_in(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    shadow_run_id: UUID,
    completed_at: datetime,
) -> ModelBurnInRun:
    completed = _aware(completed_at, "completed_at")
    run = session.get(ModelShadowRun, shadow_run_id)
    if run is None or run.status not in {"shadow_running", "burn_in_complete", "rejected"}:
        raise ModelReleaseIntegrityError("shadow run is not burn-in eligible")
    if run.policy_checksum_sha256 != policy.checksum_sha256:
        raise ModelReleaseIntegrityError("shadow run policy binding conflicts")
    candidate = _required_candidate(session, run.release_candidate_id)
    values = _expected_burn_in_projection(
        session,
        policy=policy,
        run=run,
        candidate=candidate,
        completed_at=completed,
    )
    prior_burn_in = session.scalar(
        select(ModelBurnInRun).where(ModelBurnInRun.shadow_run_id == run.id)
    )
    if prior_burn_in is not None:
        _validate_projection(prior_burn_in, values, "burn-in")
        burn_in = prior_burn_in
    else:
        burn_in = _persist_projection(
            session, ModelBurnInRun, "burn_in_key_sha256", values, "burn-in"
        )
    run.status = "burn_in_complete" if burn_in.status == "passed" else "rejected"
    run.completed_at = completed
    candidate.status = run.status
    session.commit()
    return burn_in


def _expected_burn_in_projection(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    run: ModelShadowRun,
    candidate: ModelReleaseCandidate,
    completed_at: datetime,
) -> dict[str, object]:
    if (
        run.release_candidate_id != candidate.id
        or run.policy_checksum_sha256 != policy.checksum_sha256
        or run.requirements_json != policy.burn_in_requirements
    ):
        raise ModelReleaseIntegrityError("shadow run immutable binding conflicts")
    duration = completed_at - _stored_utc(run.started_at)
    if duration < timedelta(0):
        raise ModelReleaseIntegrityError("burn-in completion precedes shadow start")
    events = tuple(
        session.scalars(
            select(ModelShadowEvent)
            .where(ModelShadowEvent.shadow_run_id == run.id)
            .order_by(ModelShadowEvent.observed_at, ModelShadowEvent.id)
        )
    )
    for event in events:
        _validate_shadow_event(session, event, run, candidate=candidate)
    cycle_count = len({item.cycle_key for item in events})
    p95 = _percentile_95([item.latency_ms for item in events])
    counts = {
        "integrity_violations": sum(item.integrity_violation for item in events),
        "score_definition_drifts": sum(item.score_definition_drift for item in events),
        "rankability_drifts": sum(item.rankability_delta != 0 for item in events),
        "coverage_drifts": sum(item.coverage_delta != 0 for item in events),
        "missing_component_events": sum(item.missing_required_components for item in events),
        "scheduler_failures": sum(item.scheduler_failure for item in events),
        "production_health_regressions": sum(item.production_health_regression for item in events),
        "notification_contamination_events": sum(
            item.production_notification_emitted for item in events
        ),
    }
    failures: list[str] = []
    if cycle_count < policy.minimum_completed_cycles:
        failures.append("insufficient_completed_cycles")
    if duration < timedelta(hours=policy.minimum_wall_clock_hours):
        failures.append("insufficient_wall_clock_duration")
    if p95 is None or p95 > policy.maximum_p95_latency_ms:
        failures.append("latency_envelope_failed")
    limit_fields = {
        "integrity_violations": "maximum_integrity_violations",
        "score_definition_drifts": "maximum_score_definition_drifts",
        "rankability_drifts": "maximum_rankability_drifts",
        "coverage_drifts": "maximum_coverage_drifts",
        "missing_component_events": "maximum_missing_component_events",
        "scheduler_failures": "maximum_scheduler_failures",
        "production_health_regressions": "maximum_production_health_regressions",
        "notification_contamination_events": "maximum_notification_contamination_events",
    }
    for count_name, policy_name in limit_fields.items():
        if counts[count_name] > cast(int, policy.burn_in_requirements[policy_name]):
            failures.append(count_name)
    duration_seconds = Decimal(duration.days * 86400 + duration.seconds) + Decimal(
        duration.microseconds
    ) / Decimal("1000000")
    result = {
        "completed_cycle_count": cycle_count,
        "wall_clock_hours": str(duration_seconds / Decimal("3600")),
        "p95_latency_ms": str(p95) if p95 is not None else None,
        **counts,
        "failures": sorted(failures),
        "future_outcomes_read": False,
        "model_tuning_performed": False,
    }
    key = canonical_json_sha256(
        {
            "shadow_run_key_sha256": run.shadow_run_key_sha256,
            "policy_checksum_sha256": policy.checksum_sha256,
            "event_fingerprints": [item.event_fingerprint_sha256 for item in events],
            "completed_at": completed_at.isoformat(),
            "result": result,
        }
    )
    return {
        "shadow_run_id": run.id,
        "release_candidate_id": candidate.id,
        "burn_in_key_sha256": key,
        "policy_checksum_sha256": policy.checksum_sha256,
        "result_json": result,
        "status": "passed" if not failures else "failed",
        "future_outcomes_read": False,
        "model_tuning_performed": False,
        "completed_at": completed_at,
    }


def _validate_burn_in_graph(
    session: Session,
    *,
    burn_in: ModelBurnInRun,
    candidate: ModelReleaseCandidate,
    policy: ModelReleasePolicy,
) -> None:
    run = session.get(ModelShadowRun, burn_in.shadow_run_id)
    if run is None or run.status != "burn_in_complete" or run.completed_at is None:
        raise ModelReleaseIntegrityError("passed burn-in lacks a completed shadow run")
    values = _expected_burn_in_projection(
        session,
        policy=policy,
        run=run,
        candidate=candidate,
        completed_at=_stored_utc(run.completed_at),
    )
    _validate_projection(burn_in, values, "burn-in")
    if burn_in.status != "passed":
        raise ModelReleaseIntegrityError("challenger burn-in did not pass")


def create_activation_approval(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    release_candidate_id: UUID,
    burn_in_run_id: UUID,
    approver_provenance: str,
    approved_at: datetime,
) -> ModelActivationApproval:
    approved = _aware(approved_at, "approved_at")
    provenance = " ".join(approver_provenance.split())
    if not provenance:
        raise ValueError("approval requires operator provenance")
    candidate = _required_candidate(session, release_candidate_id)
    _validate_release_candidate(session, candidate, policy=policy)
    burn_in = session.get(ModelBurnInRun, burn_in_run_id)
    if (
        candidate.subject_code != "challenger"
        or burn_in is None
        or burn_in.release_candidate_id != candidate.id
        or burn_in.status != "passed"
        or burn_in.future_outcomes_read
        or burn_in.model_tuning_performed
    ):
        raise ModelReleaseIntegrityError(
            "activation approval requires a passed operational burn-in"
        )
    _validate_burn_in_graph(session, burn_in=burn_in, candidate=candidate, policy=policy)
    _, deployment, active = resolve_active_release(session, policy=policy)
    if active.subject_code != "champion":
        raise ModelReleaseIntegrityError("approval must bind the current active champion")
    identity = {
        "release_candidate_key_sha256": candidate.release_candidate_key_sha256,
        "burn_in_key_sha256": burn_in.burn_in_key_sha256,
        "current_champion_deployment_key_sha256": deployment.deployment_key_sha256,
        "policy_checksum_sha256": policy.checksum_sha256,
        "challenger_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "approver_provenance": provenance,
        "approved_at": approved.isoformat(),
    }
    key = canonical_json_sha256(identity)
    prior_approval = session.scalar(
        select(ModelActivationApproval).where(
            ModelActivationApproval.release_candidate_id == candidate.id
        )
    )
    if prior_approval is not None and prior_approval.approval_key_sha256 != key:
        raise ModelReleaseIntegrityError("challenger already has a different activation approval")
    values: dict[str, object] = {
        "approval_key_sha256": key,
        "release_candidate_id": candidate.id,
        "burn_in_run_id": burn_in.id,
        "current_champion_deployment_id": deployment.id,
        "policy_checksum_sha256": policy.checksum_sha256,
        "challenger_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "approver_provenance": provenance,
        "approved_at": approved,
        "approval_fingerprint_sha256": canonical_json_sha256(identity),
    }
    approval = _persist_projection(
        session, ModelActivationApproval, "approval_key_sha256", values, "activation approval"
    )
    candidate.status = "activation_approved"
    session.commit()
    return approval


def activate_release(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    release_candidate_id: UUID,
    approval_id: UUID,
    activated_at: datetime,
) -> ModelDeployment:
    """Atomically change the active pointer after full T/burn-in/approval validation."""

    activated = _aware(activated_at, "activated_at")
    try:
        pointer = _locked_pointer(session)
        if pointer is None:
            raise ModelReleaseIntegrityError("active champion is unavailable")
        previous = _validate_active_pointer(session, pointer, policy)
        active = _required_candidate(session, previous.release_candidate_id)
        if active.subject_code != "champion":
            raise ModelReleaseIntegrityError("challenger activation requires the active champion")
        candidate = _required_candidate(session, release_candidate_id)
        _validate_release_candidate(session, candidate, policy=policy)
        if candidate.temporal_holdout_run_id is None:
            raise ModelReleaseIntegrityError("challenger lacks authoritative T evidence")
        run, decision, _ = _authoritative_t_pass(
            session,
            policy=policy,
            t_policy=t_policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
            holdout_run_id=candidate.temporal_holdout_run_id,
        )
        if (
            run.id != candidate.temporal_holdout_run_id
            or decision.id != candidate.temporal_holdout_decision_id
            or decision.decision_fingerprint_sha256
            != candidate.temporal_holdout_decision_fingerprint_sha256
        ):
            raise ModelReleaseIntegrityError("challenger T evidence changed")
        approval = session.get(ModelActivationApproval, approval_id)
        _validate_approval(session, approval, candidate, previous, policy)
        assert approval is not None
        values = _deployment_values(
            policy=policy,
            candidate=candidate,
            previous=previous,
            approval_id=approval.id,
            reason="manual_challenger_activation_after_t_and_burn_in",
            activated_at=activated,
        )
        deployment = _persist_projection(
            session, ModelDeployment, "deployment_key_sha256", values, "deployment"
        )
        pointer.deployment_id = deployment.id
        pointer.release_candidate_id = candidate.id
        pointer.subject_code = "challenger"
        pointer.scoring_definition_checksum_sha256 = candidate.scoring_definition_checksum_sha256
        pointer.pointer_version += 1
        pointer.updated_at = activated
        candidate.status = "active"
        session.commit()
        return deployment
    except Exception:
        session.rollback()
        raise


def rollback_release(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    reason_code: str,
    health_evidence: Mapping[str, object],
    rolled_back_at: datetime,
) -> ModelRollback:
    """Atomically restore the prior immutable deployment and preserve rollback history."""

    at = _aware(rolled_back_at, "rolled_back_at")
    reason = reason_code.strip()
    if not reason:
        raise ValueError("rollback reason is required")
    allowed_reasons = policy.rollback_rules.get("allowed_reason_codes")
    if not isinstance(allowed_reasons, list) or reason not in allowed_reasons:
        raise ValueError("rollback reason is not predeclared by policy")
    try:
        pointer = _locked_pointer(session)
        if pointer is None:
            raise ModelReleaseIntegrityError("active release is unavailable")
        current = _validate_active_pointer(session, pointer, policy)
        if current.activation_reason.startswith("rollback:"):
            raise ModelReleaseIntegrityError("active deployment is already a rollback transition")
        if current.previous_deployment_id is None:
            raise ModelReleaseIntegrityError("active deployment has no rollback target")
        restored = session.get(ModelDeployment, current.previous_deployment_id)
        if restored is None:
            raise ModelReleaseIntegrityError("rollback target is unavailable")
        restored_candidate = _required_candidate(session, restored.release_candidate_id)
        _validate_release_candidate(session, restored_candidate, policy=policy)
        _validate_deployment(restored, restored_candidate, policy)
        rollback_deployment_values = _deployment_values(
            policy=policy,
            candidate=restored_candidate,
            previous=current,
            approval_id=None,
            reason=f"rollback:{reason}",
            activated_at=at,
        )
        rollback_deployment = _persist_projection(
            session,
            ModelDeployment,
            "deployment_key_sha256",
            rollback_deployment_values,
            "rollback deployment",
        )
        identity = {
            "rolled_back_deployment_key_sha256": current.deployment_key_sha256,
            "restored_deployment_key_sha256": restored.deployment_key_sha256,
            "rollback_deployment_key_sha256": rollback_deployment.deployment_key_sha256,
            "reason_code": reason,
            "health_evidence": dict(health_evidence),
            "policy_checksum_sha256": policy.checksum_sha256,
            "rolled_back_at": at.isoformat(),
        }
        key = canonical_json_sha256(identity)
        values: dict[str, object] = {
            "rollback_key_sha256": key,
            "rolled_back_deployment_id": current.id,
            "restored_deployment_id": restored.id,
            "rollback_deployment_id": rollback_deployment.id,
            "reason_code": reason,
            "health_evidence_json": dict(health_evidence),
            "policy_checksum_sha256": policy.checksum_sha256,
            "rolled_back_at": at,
            "rollback_fingerprint_sha256": canonical_json_sha256(identity),
        }
        rollback = _persist_projection(
            session, ModelRollback, "rollback_key_sha256", values, "rollback"
        )
        pointer.deployment_id = rollback_deployment.id
        pointer.release_candidate_id = restored_candidate.id
        pointer.subject_code = restored_candidate.subject_code
        pointer.scoring_definition_checksum_sha256 = (
            restored_candidate.scoring_definition_checksum_sha256
        )
        pointer.pointer_version += 1
        pointer.updated_at = at
        current_candidate = _required_candidate(session, current.release_candidate_id)
        current_candidate.status = "rolled_back"
        restored_candidate.status = "active"
        session.commit()
        return rollback
    except Exception:
        session.rollback()
        raise


def freeze_active_release_for_cycle(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    operational_run_id: UUID,
    frozen_at: datetime,
) -> ModelReleaseCycleBinding:
    """Resolve active release once; retries reuse only the exact original binding."""

    frozen = _aware(frozen_at, "frozen_at")
    if session.get(OperationalRun, operational_run_id) is None:
        raise ModelReleaseIntegrityError("operational run is unavailable")
    existing = session.scalar(
        select(ModelReleaseCycleBinding).where(
            ModelReleaseCycleBinding.operational_run_id == operational_run_id
        )
    )
    if existing is not None:
        return resolve_cycle_release_context(
            session, policy=policy, operational_run_id=operational_run_id
        ).binding

    _, deployment, candidate = resolve_active_release(session, policy=policy)
    key = _cycle_binding_key(
        operational_run_id=operational_run_id,
        deployment=deployment,
        candidate=candidate,
    )
    values: dict[str, object] = {
        "operational_run_id": operational_run_id,
        "deployment_id": deployment.id,
        "release_candidate_id": candidate.id,
        "subject_code": candidate.subject_code,
        "scoring_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "binding_key_sha256": key,
        "frozen_at": frozen,
    }
    binding = ModelReleaseCycleBinding(**values)
    session.add(binding)
    session.commit()
    return binding


def resolve_cycle_release_context(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    operational_run_id: UUID,
) -> CycleReleaseContext:
    """Reconstruct an existing cycle binding without consulting the global pointer."""

    if session.get(OperationalRun, operational_run_id) is None:
        raise ModelReleaseIntegrityError("operational run is unavailable")
    binding = session.scalar(
        select(ModelReleaseCycleBinding).where(
            ModelReleaseCycleBinding.operational_run_id == operational_run_id
        )
    )
    if binding is None:
        raise ModelReleaseIntegrityError("operational cycle release is not frozen")
    deployment = session.get(ModelDeployment, binding.deployment_id)
    if deployment is None:
        raise ModelReleaseIntegrityError("cycle deployment is unavailable")
    candidate = _required_candidate(session, binding.release_candidate_id)
    _validate_release_candidate(session, candidate, policy=policy)
    _validate_deployment(deployment, candidate, policy)
    if (
        deployment.release_candidate_id != candidate.id
        or binding.release_candidate_id != candidate.id
        or binding.subject_code != candidate.subject_code
        or binding.scoring_definition_checksum_sha256
        != candidate.scoring_definition_checksum_sha256
        or binding.binding_key_sha256
        != _cycle_binding_key(
            operational_run_id=operational_run_id,
            deployment=deployment,
            candidate=candidate,
        )
    ):
        raise ModelReleaseIntegrityError("cycle release binding conflicts with frozen release")
    _stored_utc(binding.frozen_at)
    return CycleReleaseContext(binding=binding, deployment=deployment, candidate=candidate)


def project_release_score_for_cycle(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    context: CycleReleaseContext,
    snapshot: ScoreSnapshot,
) -> ModelReleaseProductionScore:
    """Project one immutable production score from the cycle's frozen active release."""

    validated = resolve_cycle_release_context(
        session,
        policy=policy,
        operational_run_id=context.binding.operational_run_id,
    )
    if (
        validated.binding.id != context.binding.id
        or validated.deployment.id != context.deployment.id
        or validated.candidate.id != context.candidate.id
    ):
        raise ModelReleaseIntegrityError("cycle release context changed during execution")
    try:
        ScoreSnapshotRepository(session).validate_persisted_snapshot(snapshot)
    except ScoreSnapshotIntegrityError as error:
        raise ModelReleaseIntegrityError("production source snapshot failed integrity") from error
    candidate = validated.candidate
    if (
        snapshot.algorithm_version != "score_snapshot_v5"
        or snapshot.scoring_configuration_id != candidate.champion_scoring_configuration_id
        or snapshot.configuration_checksum_sha256
        != candidate.scoring_definition_json.get(
            "bound_scoring_configuration_checksum_sha256"
        )
        or snapshot.selected_security_id is None
    ):
        raise ModelReleaseIntegrityError("production snapshot is not accepted frozen V5 state")

    component_projection = _component_state_projection(snapshot)
    component_checksum = canonical_json_sha256(component_projection)
    champion_rankable = bool(
        snapshot.eligibility_eligible
        and snapshot.snapshot_status == "final_score_available"
        and snapshot.final_score is not None
    )
    production_score: Decimal | None = None
    unranked_reason: str | None = None
    if champion_rankable:
        if candidate.subject_code == "champion":
            production_score = snapshot.final_score
        else:
            scores = {item.component_code: item.score for item in snapshot.components}
            if set(scores) != set(COMPONENT_CODES) or any(
                value is None for value in scores.values()
            ):
                raise ModelReleaseIntegrityError(
                    "rankable V5 production state lacks required components"
                )
            weights = _weights_from_candidate(_required_s2_candidate(session, candidate))
            production_score = counterfactual_candidate_score(
                weights,
                {code: cast(Decimal, scores[code]) for code in COMPONENT_CODES},
            )
    elif not snapshot.eligibility_eligible:
        unranked_reason = "v5_ineligible"
    elif snapshot.final_score is None:
        unranked_reason = "partial_score"
    else:
        unranked_reason = "invalid_snapshot_state"

    identity = {
        "operational_run_id": str(validated.binding.operational_run_id),
        "cycle_release_binding_key_sha256": validated.binding.binding_key_sha256,
        "deployment_key_sha256": validated.deployment.deployment_key_sha256,
        "release_candidate_key_sha256": candidate.release_candidate_key_sha256,
        "subject_code": candidate.subject_code,
        "source_score_snapshot_id": str(snapshot.id),
        "source_snapshot_fingerprint_sha256": snapshot.snapshot_fingerprint_sha256,
        "component_state_checksum_sha256": component_checksum,
        "scoring_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "knowledge_cutoff": _stored_utc(snapshot.knowledge_cutoff).isoformat(),
        "production_final_score": str(production_score) if production_score is not None else None,
        "rankable": champion_rankable,
        "unranked_reason": unranked_reason,
        "rankability_delta": 0,
        "coverage_delta": 0,
    }
    fingerprint = canonical_json_sha256(identity)
    values: dict[str, object] = {
        "operational_run_id": validated.binding.operational_run_id,
        "cycle_release_binding_id": validated.binding.id,
        "deployment_id": validated.deployment.id,
        "release_candidate_id": candidate.id,
        "subject_code": candidate.subject_code,
        "source_score_snapshot_id": snapshot.id,
        "source_snapshot_fingerprint_sha256": snapshot.snapshot_fingerprint_sha256,
        "component_state_checksum_sha256": component_checksum,
        "scoring_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "production_final_score": production_score,
        "rankable": champion_rankable,
        "unranked_reason": unranked_reason,
        "rankability_delta": 0,
        "coverage_delta": 0,
        "knowledge_cutoff": _stored_utc(snapshot.knowledge_cutoff),
        "projection_fingerprint_sha256": fingerprint,
    }
    existing = session.scalar(
        select(ModelReleaseProductionScore).where(
            ModelReleaseProductionScore.operational_run_id == validated.binding.operational_run_id,
            ModelReleaseProductionScore.source_score_snapshot_id == snapshot.id,
        )
    )
    if existing is not None:
        _validate_projection(existing, values, "release production score")
        return existing
    row = ModelReleaseProductionScore(**values)
    session.add(row)
    session.flush()
    return row


def production_release_readiness(
    session: Session | None,
    *,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy | None = None,
    s2_policy: MultibaggerChallengerDevelopmentPolicy | None = None,
    activation_policy: HistoricalEvaluationActivationPolicy | None = None,
    split_policy: MultibaggerTemporalSplitPolicy | None = None,
    database_available: bool = True,
    repository_root: Path | None = None,
    observed_at: datetime | None = None,
    raw_root: Path | None = None,
    gdelt_raw_root: Path | None = None,
    model_family: str | None = None,
    telegram_bot_token_present: bool = False,
    telegram_chat_id_present: bool = False,
) -> dict[str, object]:
    """Report operational release state separately from historical scientific validation."""

    if not database_available or session is None:
        return {
            "status": "environment_blocked",
            "message": "REAL PRODUCTION U: ENVIRONMENT-BLOCKED",
            "operational_status": "environment_blocked",
            "historical_model_validation": "environment_blocked",
            "blockers": ["production_database_unavailable"],
        }
    blockers: list[str] = []
    environment_blockers: list[str] = []
    health: dict[str, object] | None = None
    if repository_root is None or observed_at is None:
        environment_blockers.append("production_health_inputs_unavailable")
    else:
        health = production_health(
            repository_root=repository_root,
            observed_at=_aware(observed_at, "observed_at"),
            session=session,
            raw_root=raw_root,
            gdelt_raw_root=gdelt_raw_root,
            model_family=model_family,
            telegram_bot_token_present=telegram_bot_token_present,
            telegram_chat_id_present=telegram_chat_id_present,
        )
        if health["status"] != "ready":
            issues = health.get("issues", [])
            if isinstance(issues, list):
                environment_blockers.extend(str(item) for item in issues)
            else:  # pragma: no cover - production health contract
                environment_blockers.append("production_health_unavailable")
    try:
        head = session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        environment_blockers.append("database_schema_unavailable")
        session.rollback()
    else:
        if head != "20261009_0028":
            environment_blockers.append("database_schema_not_at_20261009_0028")
    try:
        session.scalar(select(ModelReleaseProductionScore.id).limit(1))
    except Exception:
        environment_blockers.append("release_scoped_production_score_unavailable")
        session.rollback()
    try:
        _, deployment, active = resolve_active_release(session, policy=policy)
        if deployment.previous_deployment_id is not None:
            previous = session.get(ModelDeployment, deployment.previous_deployment_id)
            if previous is None:
                raise ModelReleaseIntegrityError("active deployment rollback target is unavailable")
            previous_candidate = _required_candidate(session, previous.release_candidate_id)
            _validate_release_candidate(session, previous_candidate, policy=policy)
            _validate_deployment(previous, previous_candidate, policy)
        for rollback in session.scalars(select(ModelRollback)):
            _validate_rollback(session, rollback, policy)
        if active.subject_code == "challenger":
            if (
                t_policy is None
                or s2_policy is None
                or activation_policy is None
                or split_policy is None
                or active.temporal_holdout_run_id is None
            ):
                raise ModelReleaseIntegrityError(
                    "active challenger requires complete upstream readiness policies"
                )
            run, decision, _ = _authoritative_t_pass(
                session,
                policy=policy,
                t_policy=t_policy,
                s2_policy=s2_policy,
                activation_policy=activation_policy,
                split_policy=split_policy,
                holdout_run_id=active.temporal_holdout_run_id,
            )
            if (
                run.id != active.temporal_holdout_run_id
                or decision.id != active.temporal_holdout_decision_id
            ):
                raise ModelReleaseIntegrityError("active challenger T evidence conflicts")
            if deployment.previous_deployment_id is None:
                raise ModelReleaseIntegrityError("active challenger lacks its prior champion")
            prior = session.get(ModelDeployment, deployment.previous_deployment_id)
            if prior is None or prior.subject_code != "champion":
                raise ModelReleaseIntegrityError("active challenger prior champion conflicts")
            approval = (
                session.get(ModelActivationApproval, deployment.activation_approval_id)
                if deployment.activation_approval_id is not None
                else None
            )
            _validate_approval(session, approval, active, prior, policy)
    except ModelReleaseIntegrityError as error:
        blockers.append(str(error))
        deployment = None
        active = None
    production_t = tuple(
        session.scalars(
            select(MultibaggerHoldoutEvaluationRun).where(
                MultibaggerHoldoutEvaluationRun.evidence_class == PRODUCTION_EVIDENCE,
                MultibaggerHoldoutEvaluationRun.status == "completed",
                MultibaggerHoldoutEvaluationRun.policy_checksum_sha256 == T_CHECKSUM,
            )
        )
    )
    scientific = "data_blocked"
    if production_t:
        if (
            t_policy is None
            or s2_policy is None
            or activation_policy is None
            or split_policy is None
        ):
            blockers.append("completed_production_t_requires_upstream_policies")
        else:
            try:
                scientific = _validated_scientific_status(
                    session,
                    policy=policy,
                    t_policy=t_policy,
                    s2_policy=s2_policy,
                    activation_policy=activation_policy,
                    split_policy=split_policy,
                    production_t_runs=production_t,
                )
            except Exception as error:
                blockers.append(f"completed_production_t_integrity_failed:{type(error).__name__}")
    status = (
        "environment_blocked"
        if environment_blockers
        else "ready"
        if not blockers
        else "data_blocked"
    )
    shadow_status = None
    burn_in_status = None
    approval_status = None
    if active is not None and active.subject_code == "challenger":
        shadow = session.scalar(
            select(ModelShadowRun)
            .where(ModelShadowRun.release_candidate_id == active.id)
            .order_by(ModelShadowRun.created_at.desc())
        )
        shadow_status = shadow.status if shadow else None
        burn_in = (
            session.scalar(
                select(ModelBurnInRun).where(ModelBurnInRun.release_candidate_id == active.id)
            )
            if shadow
            else None
        )
        burn_in_status = burn_in.status if burn_in else None
        approval_status = "approved" if deployment and deployment.activation_approval_id else None
    return {
        "status": status,
        "message": (
            "REAL PRODUCTION U: READY"
            if status == "ready"
            else "REAL PRODUCTION U: ENVIRONMENT-BLOCKED"
            if status == "environment_blocked"
            else "REAL PRODUCTION U: DATA-BLOCKED"
        ),
        "operational_status": (
            "live_operationally_ready"
            if status == "ready"
            else "environment_blocked"
            if status == "environment_blocked"
            else "data_blocked"
        ),
        "historical_model_validation": scientific,
        "active_subject": active.subject_code if active else None,
        "active_model_checksum": (active.scoring_definition_checksum_sha256 if active else None),
        "active_deployment_id": str(deployment.id) if deployment else None,
        "challenger_activation": (
            "ready" if active and active.subject_code == "challenger" else "data_blocked"
        ),
        "shadow_candidate_status": shadow_status,
        "burn_in_status": burn_in_status,
        "activation_approval_status": approval_status,
        "last_deployment_transition": str(deployment.id) if deployment else None,
        "rollback_ready": bool(deployment and deployment.previous_deployment_id),
        "blockers": sorted(set((*blockers, *environment_blockers))),
        "production_health": health,
        "release_scoped_scoring": "cycle_binding_required",
        "opportunity_score_source": "model_release_production_scores",
        "shadow_notification_access": "forbidden",
        "secrets": "presence_checks_only",
    }


def _validated_scientific_status(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    production_t_runs: Sequence[MultibaggerHoldoutEvaluationRun],
) -> str:
    """Return completed only after every purported real T terminal graph validates."""

    if not production_t_runs:
        return "data_blocked"
    for candidate_run in production_t_runs:
        run = _holdout_run(session, candidate_run.id, t_policy)
        if (
            run.evidence_class != PRODUCTION_EVIDENCE
            or run.status != "completed"
            or run.policy_checksum_sha256 != policy.t_checksum_sha256
        ):
            raise ModelReleaseIntegrityError("purported Production T run is not authoritative")
        graph = _validated_run_input(
            session,
            run=run,
            policy=t_policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
        )
        decision = _validate_completed_holdout_graph(session, run=run, graph=graph)
        if decision.decision_code not in policy.allowed_t_decisions:
            raise ModelReleaseIntegrityError("completed Production T decision is not accepted by U")
    return "completed"


def release_history(session: Session, *, policy: ModelReleasePolicy) -> list[dict[str, object]]:
    rows = tuple(session.scalars(select(ModelDeployment).order_by(ModelDeployment.activated_at)))
    result: list[dict[str, object]] = []
    for row in rows:
        candidate = _required_candidate(session, row.release_candidate_id)
        _validate_release_candidate(session, candidate, policy=policy)
        _validate_deployment(row, candidate, policy)
        result.append(
            {
                "deployment_id": str(row.id),
                "subject": row.subject_code,
                "release_candidate_id": str(row.release_candidate_id),
                "activated_at": _stored_utc(row.activated_at),
                "activation_reason": row.activation_reason,
                "previous_deployment_id": (
                    str(row.previous_deployment_id) if row.previous_deployment_id else None
                ),
            }
        )
    for rollback in session.scalars(select(ModelRollback).order_by(ModelRollback.rolled_back_at)):
        _validate_rollback(session, rollback, policy)
    return result


def _persist_release_candidate(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    subject_code: str,
    evidence_class: str,
    config: ScoringConfiguration,
    definition: dict[str, object],
    t_run: MultibaggerHoldoutEvaluationRun | None,
    t_decision: MultibaggerHoldoutDecision | None,
    candidate: MultibaggerChallengerCandidate | None,
    freeze_id: UUID | None,
    freeze_key: str | None,
    experiment_id: UUID | None,
    experiment_key: str | None,
) -> ModelReleaseCandidate:
    definition_checksum = canonical_json_sha256(definition)
    identity = {
        "policy_code": policy.code,
        "policy_checksum_sha256": policy.checksum_sha256,
        "subject_code": subject_code,
        "evidence_class": evidence_class,
        "champion_configuration_id": str(config.id),
        "champion_checksum_sha256": policy.champion_checksum_sha256,
        "temporal_holdout_run_key_sha256": t_run.run_key_sha256 if t_run else None,
        "temporal_holdout_decision_fingerprint_sha256": (
            t_decision.decision_fingerprint_sha256 if t_decision else None
        ),
        "s2_experiment_run_key_sha256": experiment_key,
        "s2_freeze_key_sha256": freeze_key,
        "s2_candidate_key_sha256": candidate.candidate_key_sha256 if candidate else None,
        "scoring_definition_checksum_sha256": definition_checksum,
    }
    key = canonical_json_sha256(identity)
    values: dict[str, object] = {
        "release_candidate_key_sha256": key,
        "policy_code": policy.code,
        "policy_checksum_sha256": policy.checksum_sha256,
        "subject_code": subject_code,
        "evidence_class": evidence_class,
        "champion_scoring_configuration_id": config.id,
        "champion_scoring_checksum_sha256": policy.champion_checksum_sha256,
        "temporal_holdout_run_id": t_run.id if t_run else None,
        "temporal_holdout_run_key_sha256": t_run.run_key_sha256 if t_run else None,
        "temporal_holdout_decision_id": t_decision.id if t_decision else None,
        "temporal_holdout_decision_fingerprint_sha256": (
            t_decision.decision_fingerprint_sha256 if t_decision else None
        ),
        "s2_experiment_run_id": experiment_id,
        "s2_experiment_run_key_sha256": experiment_key,
        "s2_freeze_id": freeze_id,
        "s2_freeze_key_sha256": freeze_key,
        "s2_candidate_id": candidate.id if candidate else None,
        "s2_candidate_key_sha256": candidate.candidate_key_sha256 if candidate else None,
        "s2_candidate_checksum_sha256": (
            candidate.candidate_checksum_sha256 if candidate else None
        ),
        "s2_development_evidence_checksum_sha256": (
            candidate.development_evidence_checksum_sha256 if candidate else None
        ),
        "scoring_definition_json": definition,
        "scoring_definition_checksum_sha256": definition_checksum,
        "status": "candidate_created",
    }
    row = _persist_projection(
        session,
        ModelReleaseCandidate,
        "release_candidate_key_sha256",
        values,
        "release candidate",
        ignore=("status",),
    )
    session.flush()
    return row


def _authoritative_t_pass(
    session: Session,
    *,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    holdout_run_id: UUID,
):
    if (
        policy.code != POLICY_CODE
        or policy.t_checksum_sha256 != T_CHECKSUM
        or t_policy.code != policy.t_policy_code
        or t_policy.checksum_sha256 != policy.t_checksum_sha256
        or s2_policy.checksum_sha256 != policy.s2_checksum_sha256
    ):
        raise ModelReleaseIntegrityError("accepted U/S2/T policy binding conflicts")
    run = _holdout_run(session, holdout_run_id, t_policy)
    if run.evidence_class != PRODUCTION_EVIDENCE or run.status != "completed":
        raise ModelReleaseIntegrityError("challenger requires completed authoritative T evidence")
    graph = _validated_run_input(
        session,
        run=run,
        policy=t_policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
    )
    decision = _validate_completed_holdout_graph(session, run=run, graph=graph)
    if decision.decision_code != "CHALLENGER_PASSES_HOLDOUT":
        raise ModelReleaseIntegrityError("T did not pass the frozen challenger")
    if decision.production_promotion_performed:
        raise ModelReleaseIntegrityError("T may not perform production promotion")
    return run, decision, graph


def _revalidate_candidate_t(
    session: Session,
    *,
    candidate: ModelReleaseCandidate,
    policy: ModelReleasePolicy,
    t_policy: MultibaggerTemporalHoldoutPolicy,
    s2_policy: MultibaggerChallengerDevelopmentPolicy,
    activation_policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
) -> None:
    if candidate.temporal_holdout_run_id is None:
        raise ModelReleaseIntegrityError("challenger release lacks authoritative T evidence")
    run, decision, graph = _authoritative_t_pass(
        session,
        policy=policy,
        t_policy=t_policy,
        s2_policy=s2_policy,
        activation_policy=activation_policy,
        split_policy=split_policy,
        holdout_run_id=candidate.temporal_holdout_run_id,
    )
    if (
        run.run_key_sha256 != candidate.temporal_holdout_run_key_sha256
        or decision.id != candidate.temporal_holdout_decision_id
        or decision.decision_fingerprint_sha256
        != candidate.temporal_holdout_decision_fingerprint_sha256
        or graph.experiment.id != candidate.s2_experiment_run_id
        or graph.experiment.run_key_sha256 != candidate.s2_experiment_run_key_sha256
        or graph.freeze is None
        or graph.freeze.id != candidate.s2_freeze_id
        or graph.freeze.freeze_key_sha256 != candidate.s2_freeze_key_sha256
        or graph.candidate is None
        or graph.candidate.id != candidate.s2_candidate_id
    ):
        raise ModelReleaseIntegrityError("challenger release T/S2 evidence binding conflicts")


def _accepted_champion_configuration(
    session: Session, policy: ModelReleasePolicy
) -> ScoringConfiguration:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        profile = load_research_profile(repository_root / RESEARCH_PROFILE_PATH)
    except ValueError as error:
        raise ModelReleaseIntegrityError("accepted Research V4 profile is invalid") from error
    if (
        profile.research_profile_code != RESEARCH_PROFILE_CODE
        or profile.profile_version != RESEARCH_PROFILE_VERSION
        or profile.checksum_sha256 != RESEARCH_PROFILE_CHECKSUM
        or Path(profile.scoring_policy_asset) != V5_ASSET_PATH
    ):
        raise ModelReleaseIntegrityError("accepted Research V4 profile binding conflicts")
    try:
        static_definition = json.loads(
            (repository_root / V5_ASSET_PATH).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ModelReleaseIntegrityError("accepted V5 definition is unavailable") from error
    if (
        not isinstance(static_definition, dict)
        or canonical_json_sha256(static_definition) != policy.champion_checksum_sha256
        or policy.champion_checksum_sha256 != CHAMPION_CHECKSUM
    ):
        raise ModelReleaseIntegrityError("accepted static V5 definition binding conflicts")
    try:
        bound = bind_production_policy(session, profile, repository_root=repository_root)
    except ProductionPolicyBindingError as error:
        raise ModelReleaseIntegrityError(
            "accepted Research V4 provider binding is unavailable"
        ) from error
    bound_checksum = scoring_policy_checksum(bound.policy)
    configs = tuple(
        session.scalars(
            select(ScoringConfiguration).where(
                ScoringConfiguration.configuration_name == profile.research_profile_code,
                ScoringConfiguration.configuration_version == profile.profile_version,
                ScoringConfiguration.status == "active",
                ScoringConfiguration.checksum_sha256 == bound_checksum,
            )
        )
    )
    if len(configs) != 1:
        raise ModelReleaseIntegrityError(
            "exactly one accepted V5 scoring configuration is required"
        )
    config = configs[0]
    model = config.model_version
    if (
        config.checksum_sha256 != bound_checksum
        or config.configuration_name != RESEARCH_PROFILE_CODE
        or config.configuration_version != RESEARCH_PROFILE_VERSION
        or config.effective_from is None
        or config.effective_to is not None
        or model.status != "active"
        or model.retired_at is not None
        or not model.model_family.strip()
        or not model.semantic_version.strip()
        or not model.git_sha.strip()
    ):
        raise ModelReleaseIntegrityError("accepted V5 scoring configuration binding conflicts")
    try:
        persisted = ScoringPolicyRepository(session).get_scoring_configuration(config.id)
    except (ScoringPolicyIntegrityError, ValueError) as error:
        raise ModelReleaseIntegrityError(
            "accepted V5 scoring configuration failed persisted integrity"
        ) from error
    if persisted is None:
        raise ModelReleaseIntegrityError("accepted V5 scoring configuration is unavailable")
    if (
        persisted.record.id != config.id
        or scoring_policy_checksum(persisted.policy) != bound_checksum
        or persisted.record.configuration_json != config.configuration_json
    ):
        raise ModelReleaseIntegrityError("accepted V5 bound policy projection conflicts")
    return config


def _champion_runtime_definition(
    config: ScoringConfiguration, *, policy: ModelReleasePolicy
) -> dict[str, object]:
    """Bind portable V5 semantics separately from one database's live policy."""

    model = config.model_version
    return {
        "static_champion_definition_checksum_sha256": policy.champion_checksum_sha256,
        "research_profile_code": RESEARCH_PROFILE_CODE,
        "research_profile_version": RESEARCH_PROFILE_VERSION,
        "research_profile_checksum_sha256": RESEARCH_PROFILE_CHECKSUM,
        "scoring_configuration_id": str(config.id),
        "bound_scoring_configuration_checksum_sha256": config.checksum_sha256,
        "model_version_id": str(model.id),
        "model_family": model.model_family,
        "model_semantic_version": model.semantic_version,
        "model_git_sha": model.git_sha,
    }


def _required_candidate(session: Session, candidate_id: UUID) -> ModelReleaseCandidate:
    candidate = session.get(ModelReleaseCandidate, candidate_id)
    if candidate is None:
        raise ModelReleaseIntegrityError("release candidate is unavailable")
    return candidate


def _required_s2_candidate(
    session: Session, release: ModelReleaseCandidate
) -> MultibaggerChallengerCandidate:
    if release.s2_candidate_id is None:
        raise ModelReleaseIntegrityError("release lacks its frozen S2 candidate")
    candidate = session.get(MultibaggerChallengerCandidate, release.s2_candidate_id)
    if candidate is None:
        raise ModelReleaseIntegrityError("frozen S2 candidate is unavailable")
    definition = release.scoring_definition_json
    if (
        candidate.candidate_key_sha256 != release.s2_candidate_key_sha256
        or candidate.candidate_checksum_sha256 != release.s2_candidate_checksum_sha256
        or candidate.development_evidence_checksum_sha256
        != release.s2_development_evidence_checksum_sha256
        or candidate.ordered_weights_json != definition.get("ordered_weights")
        or candidate.hypothesis != definition.get("hypothesis")
        or candidate.algorithm_version != definition.get("algorithm_version")
    ):
        raise ModelReleaseIntegrityError("frozen S2 candidate definition conflicts")
    return candidate


def _validate_release_candidate(
    session: Session, candidate: ModelReleaseCandidate, *, policy: ModelReleasePolicy
) -> None:
    if (
        candidate.policy_code != policy.code
        or candidate.policy_checksum_sha256 != policy.checksum_sha256
        or candidate.champion_scoring_checksum_sha256 != policy.champion_checksum_sha256
        or canonical_json_sha256(candidate.scoring_definition_json)
        != candidate.scoring_definition_checksum_sha256
    ):
        raise ModelReleaseIntegrityError("release candidate immutable definition conflicts")
    config = session.get(ScoringConfiguration, candidate.champion_scoring_configuration_id)
    accepted = _accepted_champion_configuration(session, policy)
    if config is None or config.id != accepted.id:
        raise ModelReleaseIntegrityError("release candidate champion binding conflicts")
    expected_runtime = _champion_runtime_definition(config, policy=policy)
    if any(
        candidate.scoring_definition_json.get(field) != value
        for field, value in expected_runtime.items()
    ):
        raise ModelReleaseIntegrityError("release candidate live V5 definition conflicts")
    if candidate.subject_code == "champion":
        if (
            candidate.evidence_class != CHAMPION_EVIDENCE
            or candidate.temporal_holdout_run_id is not None
            or candidate.s2_candidate_id is not None
        ):
            raise ModelReleaseIntegrityError("champion release evidence conflicts")
    elif candidate.subject_code == "challenger":
        if candidate.evidence_class != PRODUCTION_EVIDENCE_CLASS:
            raise ModelReleaseIntegrityError("fixture challenger release is forbidden")
        _required_s2_candidate(session, candidate)
    else:
        raise ModelReleaseIntegrityError("unsupported release subject")
    expected_key = canonical_json_sha256(
        {
            "policy_code": candidate.policy_code,
            "policy_checksum_sha256": candidate.policy_checksum_sha256,
            "subject_code": candidate.subject_code,
            "evidence_class": candidate.evidence_class,
            "champion_configuration_id": str(candidate.champion_scoring_configuration_id),
            "champion_checksum_sha256": candidate.champion_scoring_checksum_sha256,
            "temporal_holdout_run_key_sha256": candidate.temporal_holdout_run_key_sha256,
            "temporal_holdout_decision_fingerprint_sha256": (
                candidate.temporal_holdout_decision_fingerprint_sha256
            ),
            "s2_experiment_run_key_sha256": candidate.s2_experiment_run_key_sha256,
            "s2_freeze_key_sha256": candidate.s2_freeze_key_sha256,
            "s2_candidate_key_sha256": candidate.s2_candidate_key_sha256,
            "scoring_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        }
    )
    if expected_key != candidate.release_candidate_key_sha256:
        raise ModelReleaseIntegrityError("release candidate key conflicts")


def _validate_active_pointer(
    session: Session, pointer: ActiveModelRelease, policy: ModelReleasePolicy
) -> ModelDeployment:
    if pointer.pointer_name != ACTIVE_POINTER or pointer.pointer_version < 1:
        raise ModelReleaseIntegrityError("active model pointer is malformed")
    deployment = session.get(ModelDeployment, pointer.deployment_id)
    if deployment is None:
        raise ModelReleaseIntegrityError("active deployment is unavailable")
    candidate = _required_candidate(session, deployment.release_candidate_id)
    _validate_release_candidate(session, candidate, policy=policy)
    _validate_deployment(deployment, candidate, policy)
    if (
        pointer.release_candidate_id != candidate.id
        or pointer.subject_code != candidate.subject_code
        or pointer.scoring_definition_checksum_sha256
        != candidate.scoring_definition_checksum_sha256
    ):
        raise ModelReleaseIntegrityError("active model pointer conflicts with deployment")
    return deployment


def _validate_deployment(
    deployment: ModelDeployment,
    candidate: ModelReleaseCandidate,
    policy: ModelReleasePolicy,
) -> None:
    values = _deployment_values(
        policy=policy,
        candidate=candidate,
        previous=None,
        approval_id=deployment.activation_approval_id,
        reason=deployment.activation_reason,
        activated_at=_stored_utc(deployment.activated_at),
        previous_id=deployment.previous_deployment_id,
    )
    _validate_projection(deployment, values, "deployment")


def _cycle_binding_key(
    *,
    operational_run_id: UUID,
    deployment: ModelDeployment,
    candidate: ModelReleaseCandidate,
) -> str:
    return canonical_json_sha256(
        {
            "operational_run_id": str(operational_run_id),
            "deployment_key_sha256": deployment.deployment_key_sha256,
            "release_candidate_key_sha256": candidate.release_candidate_key_sha256,
            "scoring_definition_checksum_sha256": (candidate.scoring_definition_checksum_sha256),
        }
    )


def _component_state_projection(snapshot: ScoreSnapshot) -> list[dict[str, object]]:
    return [
        {
            "component_code": item.component_code,
            "score": str(item.score) if item.score is not None else None,
            "algorithm_version": item.algorithm_version,
        }
        for item in sorted(snapshot.components, key=lambda value: value.component_code)
    ]


def _validate_rollback(
    session: Session, rollback: ModelRollback, policy: ModelReleasePolicy
) -> None:
    rolled_back = session.get(ModelDeployment, rollback.rolled_back_deployment_id)
    restored = session.get(ModelDeployment, rollback.restored_deployment_id)
    transition = session.get(ModelDeployment, rollback.rollback_deployment_id)
    if rolled_back is None or restored is None or transition is None:
        raise ModelReleaseIntegrityError("rollback deployment graph is incomplete")
    for deployment in (rolled_back, restored, transition):
        candidate = _required_candidate(session, deployment.release_candidate_id)
        _validate_release_candidate(session, candidate, policy=policy)
        _validate_deployment(deployment, candidate, policy)
    if (
        rolled_back.previous_deployment_id != restored.id
        or transition.previous_deployment_id != rolled_back.id
        or transition.release_candidate_id != restored.release_candidate_id
        or transition.activation_reason != f"rollback:{rollback.reason_code}"
        or rollback.reason_code
        not in cast(list[str], policy.rollback_rules["allowed_reason_codes"])
    ):
        raise ModelReleaseIntegrityError("rollback deployment binding conflicts")
    identity = {
        "rolled_back_deployment_key_sha256": rolled_back.deployment_key_sha256,
        "restored_deployment_key_sha256": restored.deployment_key_sha256,
        "rollback_deployment_key_sha256": transition.deployment_key_sha256,
        "reason_code": rollback.reason_code,
        "health_evidence": rollback.health_evidence_json,
        "policy_checksum_sha256": policy.checksum_sha256,
        "rolled_back_at": _stored_utc(rollback.rolled_back_at).isoformat(),
    }
    expected = canonical_json_sha256(identity)
    if (
        rollback.policy_checksum_sha256 != policy.checksum_sha256
        or rollback.rollback_key_sha256 != expected
        or rollback.rollback_fingerprint_sha256 != expected
    ):
        raise ModelReleaseIntegrityError("rollback immutable projection conflicts")


def _deployment_values(
    *,
    policy: ModelReleasePolicy,
    candidate: ModelReleaseCandidate,
    previous: ModelDeployment | None,
    approval_id: UUID | None,
    reason: str,
    activated_at: datetime,
    previous_id: UUID | None = None,
) -> dict[str, object]:
    prior_id = previous.id if previous is not None else previous_id
    identity = {
        "release_candidate_key_sha256": candidate.release_candidate_key_sha256,
        "subject_code": candidate.subject_code,
        "scoring_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "previous_deployment_id": str(prior_id) if prior_id else None,
        "activation_approval_id": str(approval_id) if approval_id else None,
        "activation_reason": reason,
        "policy_checksum_sha256": policy.checksum_sha256,
        "activated_at": activated_at.isoformat(),
    }
    key = canonical_json_sha256(identity)
    return {
        "deployment_key_sha256": key,
        "release_candidate_id": candidate.id,
        "subject_code": candidate.subject_code,
        "scoring_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "previous_deployment_id": prior_id,
        "activation_approval_id": approval_id,
        "activation_reason": reason,
        "policy_checksum_sha256": policy.checksum_sha256,
        "activated_at": activated_at,
        "deployment_fingerprint_sha256": canonical_json_sha256(identity),
    }


def _validate_approval(
    session: Session,
    approval: ModelActivationApproval | None,
    candidate: ModelReleaseCandidate,
    current: ModelDeployment,
    policy: ModelReleasePolicy,
) -> None:
    if approval is None:
        raise ModelReleaseIntegrityError("persisted manual activation approval is required")
    burn_in = session.get(ModelBurnInRun, approval.burn_in_run_id)
    if (
        approval.release_candidate_id != candidate.id
        or approval.current_champion_deployment_id != current.id
        or approval.policy_checksum_sha256 != policy.checksum_sha256
        or approval.challenger_definition_checksum_sha256
        != candidate.scoring_definition_checksum_sha256
        or burn_in is None
        or burn_in.release_candidate_id != candidate.id
        or burn_in.status != "passed"
        or burn_in.future_outcomes_read
        or burn_in.model_tuning_performed
    ):
        raise ModelReleaseIntegrityError("activation approval binding conflicts")
    _validate_burn_in_graph(session, burn_in=burn_in, candidate=candidate, policy=policy)
    identity = {
        "release_candidate_key_sha256": candidate.release_candidate_key_sha256,
        "burn_in_key_sha256": burn_in.burn_in_key_sha256,
        "current_champion_deployment_key_sha256": current.deployment_key_sha256,
        "policy_checksum_sha256": policy.checksum_sha256,
        "challenger_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
        "approver_provenance": approval.approver_provenance,
        "approved_at": _stored_utc(approval.approved_at).isoformat(),
    }
    expected_fingerprint = canonical_json_sha256(identity)
    if (
        approval.approval_key_sha256 != expected_fingerprint
        or approval.approval_fingerprint_sha256 != expected_fingerprint
    ):
        raise ModelReleaseIntegrityError("activation approval fingerprint conflicts")


def _validate_shadow_event(
    session: Session,
    event: ModelShadowEvent,
    run: ModelShadowRun,
    *,
    candidate: ModelReleaseCandidate,
) -> None:
    if (
        event.shadow_run_id != run.id
        or event.rankability_delta != 0
        or event.coverage_delta != 0
        or event.champion_rankable != event.challenger_rankable
        or event.production_notification_emitted
    ):
        raise ModelReleaseIntegrityError("shadow event isolation invariant failed")
    snapshot = session.get(ScoreSnapshot, event.source_score_snapshot_id)
    if snapshot is None:
        raise ModelReleaseIntegrityError("shadow event source snapshot is unavailable")
    try:
        ScoreSnapshotRepository(session).validate_persisted_snapshot(snapshot)
    except ScoreSnapshotIntegrityError as error:
        raise ModelReleaseIntegrityError("shadow event source snapshot failed integrity") from error
    components = sorted(snapshot.components, key=lambda item: item.component_code)
    component_projection = [
        {
            "component_code": item.component_code,
            "score": str(item.score) if item.score is not None else None,
            "algorithm_version": item.algorithm_version,
        }
        for item in components
    ]
    component_checksum = canonical_json_sha256(component_projection)
    champion_rankable = bool(
        snapshot.eligibility_eligible
        and snapshot.snapshot_status == "final_score_available"
        and snapshot.final_score is not None
    )
    expected_challenger_score: Decimal | None = None
    if champion_rankable:
        scores = {item.component_code: item.score for item in components}
        if set(scores) != set(COMPONENT_CODES) or any(item is None for item in scores.values()):
            raise ModelReleaseIntegrityError("shadow event component state is incomplete")
        weights = _weights_from_candidate(_required_s2_candidate(session, candidate))
        expected_challenger_score = counterfactual_candidate_score(
            weights, {code: cast(Decimal, scores[code]) for code in COMPONENT_CODES}
        )
    if (
        snapshot.selected_security_id != event.security_id
        or _stored_utc(snapshot.knowledge_cutoff) != _stored_utc(event.evidence_cutoff)
        or snapshot.snapshot_fingerprint_sha256 != event.source_snapshot_fingerprint_sha256
        or component_checksum != event.component_state_checksum_sha256
        or (snapshot.final_score if champion_rankable else None) != event.champion_score
        or champion_rankable != event.champion_rankable
        or expected_challenger_score != event.challenger_score
    ):
        raise ModelReleaseIntegrityError("shadow event source-state binding conflicts")
    identity = {
        "shadow_run_key_sha256": run.shadow_run_key_sha256,
        "cycle_key": event.cycle_key,
        "security_id": str(event.security_id),
        "evidence_cutoff": _stored_utc(event.evidence_cutoff).isoformat(),
        "snapshot_fingerprint_sha256": event.source_snapshot_fingerprint_sha256,
        "component_state_checksum_sha256": event.component_state_checksum_sha256,
        "candidate_definition_checksum_sha256": candidate.scoring_definition_checksum_sha256,
    }
    values = {
        "shadow_run_id": event.shadow_run_id,
        "shadow_event_key_sha256": event.shadow_event_key_sha256,
        "cycle_key": event.cycle_key,
        "security_id": event.security_id,
        "evidence_cutoff": _stored_utc(event.evidence_cutoff),
        "source_score_snapshot_id": event.source_score_snapshot_id,
        "source_snapshot_fingerprint_sha256": event.source_snapshot_fingerprint_sha256,
        "component_state_checksum_sha256": event.component_state_checksum_sha256,
        "champion_score": event.champion_score,
        "challenger_score": event.challenger_score,
        "champion_rankable": event.champion_rankable,
        "challenger_rankable": event.challenger_rankable,
        "rankability_delta": event.rankability_delta,
        "coverage_delta": event.coverage_delta,
        "latency_ms": event.latency_ms,
        "integrity_violation": event.integrity_violation,
        "score_definition_drift": event.score_definition_drift,
        "missing_required_components": event.missing_required_components,
        "scheduler_failure": event.scheduler_failure,
        "production_health_regression": event.production_health_regression,
        "production_notification_emitted": event.production_notification_emitted,
        "observed_at": _stored_utc(event.observed_at),
    }
    expected_key = canonical_json_sha256(identity)
    expected_fingerprint = canonical_json_sha256(
        {"identity": identity, "projection": _jsonable(values)}
    )
    if (
        event.shadow_event_key_sha256 != expected_key
        or event.event_fingerprint_sha256 != expected_fingerprint
    ):
        raise ModelReleaseIntegrityError("shadow event fingerprint conflicts")


def _locked_pointer(session: Session) -> ActiveModelRelease | None:
    return session.scalar(
        select(ActiveModelRelease)
        .where(ActiveModelRelease.pointer_name == ACTIVE_POINTER)
        .with_for_update()
    )


def _persist_projection(
    session: Session,
    model: type,
    key_field: str,
    values: dict[str, object],
    label: str,
    *,
    ignore: Sequence[str] = (),
):
    existing = session.scalar(select(model).where(getattr(model, key_field) == values[key_field]))
    if existing is not None:
        _validate_projection(existing, values, label, ignore=ignore)
        return existing
    row = model(**values)
    session.add(row)
    session.flush()
    return row


def _validate_projection(
    persisted: object,
    expected: Mapping[str, object],
    label: str,
    *,
    ignore: Sequence[str] = (),
) -> None:
    ignored = set(ignore)
    for field, value in expected.items():
        if field in ignored:
            continue
        actual = getattr(persisted, field)
        if isinstance(actual, datetime) and isinstance(value, datetime):
            if _stored_utc(actual) != _stored_utc(value):
                raise ModelReleaseIntegrityError(f"{label} immutable projection conflicts")
        elif actual != value:
            raise ModelReleaseIntegrityError(f"{label} immutable projection conflicts")


def _percentile_95(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    ordinal = int(
        (Decimal(len(ordered)) * Decimal("0.95")).to_integral_value(rounding=ROUND_CEILING)
    )
    return ordered[max(ordinal - 1, 0)]


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _jsonable(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _stored_utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
