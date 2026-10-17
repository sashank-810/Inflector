"""Production U release-governance contracts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.model_release import (
    CHAMPION_EVIDENCE,
    ModelReleaseIntegrityError,
    _deployment_values,
    _validate_burn_in_graph,
    _validated_scientific_status,
    activate_release,
    bootstrap_active_champion,
    complete_burn_in,
    create_activation_approval,
    create_champion_release_candidate,
    freeze_active_release_for_cycle,
    production_release_readiness,
    project_release_score_for_cycle,
    record_shadow_cycle,
    release_history,
    resolve_active_release,
    resolve_cycle_release_context,
    rollback_release,
    start_shadow_run,
)
from inflector_data.model_release_cli import _parser
from inflector_data.model_release_policy import (
    CHAMPION_CHECKSUM,
    POLICY_CHECKSUM,
    load_model_release_policy,
)
from inflector_data.multibagger_challenger import ALGORITHM_VERSION, COMPONENT_CODES
from inflector_database.base import Base
from inflector_database.models import (
    ActiveModelRelease,
    Company,
    ModelBurnInRun,
    ModelDeployment,
    ModelReleaseCandidate,
    ModelReleaseProductionScore,
    ModelRollback,
    MultibaggerChallengerCandidate,
    OperationalRun,
    ScoreComponent,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config/operations/production_model_release_v1.json"
AT = datetime(2026, 10, 9, 12, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def _policy():
    return load_model_release_policy(POLICY_PATH)


class RuntimeReadinessInputs(TypedDict):
    repository_root: Path
    observed_at: datetime
    raw_root: Path
    gdelt_raw_root: Path
    model_family: str
    telegram_bot_token_present: bool
    telegram_chat_id_present: bool


def _runtime_readiness_inputs(session: Session, tmp_path: Path) -> RuntimeReadinessInputs:
    raw_root = tmp_path / "raw"
    gdelt_root = tmp_path / "gdelt"
    raw_root.mkdir()
    gdelt_root.mkdir()
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20261009_0028')"))
    session.commit()
    return {
        "repository_root": ROOT,
        "observed_at": AT,
        "raw_root": raw_root,
        "gdelt_raw_root": gdelt_root,
        "model_family": "inflector_v5",
        "telegram_bot_token_present": True,
        "telegram_chat_id_present": True,
    }


def _champion_configuration(session: Session) -> ScoringConfiguration:
    from inflector_database.models import ModelVersion

    model = ModelVersion(
        id=UUID(int=8100),
        model_family="inflector_v5",
        semantic_version="5",
        git_sha="a" * 40,
        status="active",
    )
    config = ScoringConfiguration(
        id=UUID(int=8101),
        model_version_id=model.id,
        configuration_name="production_opportunity_v5",
        configuration_version="5",
        status="active",
        configuration_json={"fixture": "checksum-bound"},
        checksum_sha256=CHAMPION_CHECKSUM,
    )
    session.add_all([model, config])
    session.commit()
    return config


def _operational_run(session: Session, *, run_id: UUID = UUID(int=8200)) -> OperationalRun:
    row = OperationalRun(
        id=run_id,
        run_key_sha256=canonical_json_sha256({"run_id": str(run_id)}),
        operations_profile_code="production_operations_v6",
        operations_profile_checksum_sha256="c" * 64,
        research_profile_code="production_research_v4",
        research_profile_checksum_sha256="d" * 64,
        model_family="inflector_v5",
        fiscal_year=2026,
        fiscal_quarter=2,
        cycle_at=AT,
        knowledge_cutoff=AT,
        symbol_set_checksum_sha256="e" * 64,
        ordered_symbols_json=["INFY"],
        inputs_json={},
        status="planned",
        planned_at=AT,
        summary_json={},
    )
    session.add(row)
    session.commit()
    return row


def _fixture_challenger(session: Session) -> ModelReleaseCandidate:
    config = session.get(ScoringConfiguration, UUID(int=8101))
    if config is None:
        config = _champion_configuration(session)
    weights = [{"component_code": code, "weight": "0.125"} for code in COMPONENT_CODES]
    weight_checksum = canonical_json_sha256(weights)
    s2 = MultibaggerChallengerCandidate(
        id=UUID(int=8300),
        experiment_run_id=UUID(int=8301),
        candidate_code="fixture_equal_weights",
        candidate_key_sha256="1" * 64,
        candidate_checksum_sha256=weight_checksum,
        ordered_weights_json=weights,
        hypothesis="Fixture mechanics only",
        source_error_analysis_run_id=UUID(int=8302),
        development_evidence_json=[],
        development_evidence_checksum_sha256="2" * 64,
        algorithm_version=ALGORITHM_VERSION,
        status="eligible_for_t",
    )
    definition: dict[str, object] = {
        "source": "authoritative_completed_t",
        "algorithm_version": s2.algorithm_version,
        "candidate_code": s2.candidate_code,
        "candidate_key_sha256": s2.candidate_key_sha256,
        "candidate_checksum_sha256": s2.candidate_checksum_sha256,
        "development_evidence_checksum_sha256": s2.development_evidence_checksum_sha256,
        "ordered_weights": weights,
        "hypothesis": s2.hypothesis,
    }
    definition_checksum = canonical_json_sha256(definition)
    identity = {
        "policy_code": _policy().code,
        "policy_checksum_sha256": _policy().checksum_sha256,
        "subject_code": "challenger",
        "evidence_class": "production_authoritative",
        "champion_configuration_id": str(config.id),
        "champion_checksum_sha256": CHAMPION_CHECKSUM,
        "temporal_holdout_run_key_sha256": "3" * 64,
        "temporal_holdout_decision_fingerprint_sha256": "4" * 64,
        "s2_experiment_run_key_sha256": "5" * 64,
        "s2_freeze_key_sha256": "6" * 64,
        "s2_candidate_key_sha256": s2.candidate_key_sha256,
        "scoring_definition_checksum_sha256": definition_checksum,
    }
    release = ModelReleaseCandidate(
        id=UUID(int=8303),
        release_candidate_key_sha256=canonical_json_sha256(identity),
        policy_code=_policy().code,
        policy_checksum_sha256=_policy().checksum_sha256,
        subject_code="challenger",
        evidence_class="production_authoritative",
        champion_scoring_configuration_id=config.id,
        champion_scoring_checksum_sha256=CHAMPION_CHECKSUM,
        temporal_holdout_run_id=UUID(int=8304),
        temporal_holdout_run_key_sha256="3" * 64,
        temporal_holdout_decision_id=UUID(int=8305),
        temporal_holdout_decision_fingerprint_sha256="4" * 64,
        s2_experiment_run_id=UUID(int=8301),
        s2_experiment_run_key_sha256="5" * 64,
        s2_freeze_id=UUID(int=8306),
        s2_freeze_key_sha256="6" * 64,
        s2_candidate_id=s2.id,
        s2_candidate_key_sha256=s2.candidate_key_sha256,
        s2_candidate_checksum_sha256=s2.candidate_checksum_sha256,
        s2_development_evidence_checksum_sha256=s2.development_evidence_checksum_sha256,
        scoring_definition_json=definition,
        scoring_definition_checksum_sha256=definition_checksum,
        status="candidate_created",
    )
    session.add_all([s2, release])
    session.commit()
    return release


def _snapshot(session: Session) -> ScoreSnapshot:
    company = Company(
        id=UUID(int=8400),
        legal_name="U Fixture Limited",
        display_name="U Fixture",
        sector="Industrials",
        industry="Engineering",
    )
    security = Security(
        id=UUID(int=8401),
        company_id=company.id,
        isin="INE000U01001",
        security_type="equity",
        status="active",
    )
    snapshot = ScoreSnapshot(
        id=UUID(int=8402),
        company_id=company.id,
        model_version_id=UUID(int=8100),
        scoring_configuration_id=UUID(int=8101),
        configuration_checksum_sha256=CHAMPION_CHECKSUM,
        as_of_date=date(2026, 10, 9),
        knowledge_cutoff=AT,
        ending_fiscal_year=2026,
        ending_fiscal_quarter=2,
        selected_provider_dataset_id=None,
        selected_filing_scope="consolidated",
        selected_security_id=security.id,
        snapshot_status="final_score_available",
        eligibility_eligible=True,
        eligibility_inputs_json={},
        eligibility_reasons_json=[],
        eligibility_warnings_json=[],
        financial_core_coverage=Decimal("1"),
        confidence=Decimal("1"),
        confidence_inputs_json={},
        confidence_details_json={},
        top_level_component_weight_coverage=Decimal("1"),
        available_component_codes_json=list(COMPONENT_CODES),
        missing_component_codes_json=[],
        context_resolution_json={},
        input_manifest_json={},
        fingerprint_payload_json={},
        snapshot_fingerprint_sha256="7" * 64,
        final_score=Decimal("77"),
        algorithm_version="score_snapshot_v5",
    )
    components = [
        ScoreComponent(
            score_snapshot_id=snapshot.id,
            component_code=code,
            score=Decimal("80"),
            unit="score_0_100",
            configured_top_level_weight=Decimal("0.125"),
            subfactor_weight_coverage=Decimal("1"),
            final_contribution=Decimal("10"),
            available_at=AT,
            algorithm_version=f"{code}_fixture_v1",
            missing_subfactors_json=[],
            warnings_json=[],
            detail_json={},
        )
        for code in COMPONENT_CODES
    ]
    session.add_all([company, security, snapshot, *components])
    session.commit()
    return snapshot


def _shadow_run(session: Session, monkeypatch: pytest.MonkeyPatch):
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    candidate = _fixture_challenger(session)
    monkeypatch.setattr(
        "inflector_data.model_release._revalidate_candidate_t", lambda *args, **kwargs: None
    )
    run = start_shadow_run(
        session,
        policy=_policy(),
        t_policy=object(),  # type: ignore[arg-type]
        s2_policy=object(),  # type: ignore[arg-type]
        activation_policy=object(),  # type: ignore[arg-type]
        split_policy=object(),  # type: ignore[arg-type]
        release_candidate_id=candidate.id,
        started_at=AT,
    )
    return candidate, run


def _passed_burn_in(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModelReleaseCandidate, ModelBurnInRun]:
    candidate, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    for index in range(20):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key=f"cycle-{index:03d}",
            snapshot_id=snapshot.id,
            observed_at=AT + timedelta(hours=index),
            latency_ms=Decimal("25"),
        )
    return candidate, complete_burn_in(
        session,
        policy=_policy(),
        shadow_run_id=run.id,
        completed_at=AT + timedelta(hours=168),
    )


def test_u_policy_is_strict_and_checksum_bound(tmp_path: Path) -> None:
    policy = _policy()
    assert policy.code == "production_model_release_v1"
    assert policy.checksum_sha256 == POLICY_CHECKSUM
    assert policy.champion_checksum_sha256 == CHAMPION_CHECKSUM
    assert policy.minimum_completed_cycles == 20
    assert policy.minimum_wall_clock_hours == 168

    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    value["burn_in_requirements"]["minimum_completed_cycles"] = 19
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical checksum"):
        load_model_release_policy(changed)


def test_champion_bootstrap_is_exact_idempotent_and_does_not_claim_t(
    session: Session, tmp_path: Path
) -> None:
    _champion_configuration(session)
    first = bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    second = bootstrap_active_champion(
        session, policy=_policy(), activated_at=AT + timedelta(days=1)
    )
    pointer, active, candidate = resolve_active_release(session, policy=_policy())

    assert first.id == second.id == active.id
    assert pointer.pointer_version == 1
    assert candidate.subject_code == "champion"
    assert candidate.evidence_class == CHAMPION_EVIDENCE
    assert candidate.temporal_holdout_run_id is None
    readiness = production_release_readiness(
        session, policy=_policy(), **_runtime_readiness_inputs(session, tmp_path)
    )
    assert readiness["status"] == "ready"
    assert readiness["operational_status"] == "live_operationally_ready"
    assert readiness["historical_model_validation"] == "data_blocked"


def test_bootstrap_rejects_foreign_or_duplicate_champion_configuration(session: Session) -> None:
    config = _champion_configuration(session)
    config.configuration_name = "foreign"
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="binding conflicts"):
        bootstrap_active_champion(session, policy=_policy(), activated_at=AT)


def test_release_candidate_projection_is_idempotent_and_tamper_evident(session: Session) -> None:
    _champion_configuration(session)
    first = create_champion_release_candidate(session, policy=_policy())
    second = create_champion_release_candidate(session, policy=_policy())
    assert first.id == second.id
    session.commit()
    first.scoring_definition_checksum_sha256 = "0" * 64
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="immutable projection"):
        create_champion_release_candidate(session, policy=_policy())


def test_active_pointer_corruption_fails_release_readiness(
    session: Session, tmp_path: Path
) -> None:
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    pointer = session.get(ActiveModelRelease, "production")
    assert pointer is not None
    pointer.scoring_definition_checksum_sha256 = "0" * 64
    session.commit()
    report = production_release_readiness(
        session, policy=_policy(), **_runtime_readiness_inputs(session, tmp_path)
    )
    assert report["status"] == "data_blocked"
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    assert any("pointer conflicts" in str(item) for item in blockers)


def test_release_readiness_requires_complete_runtime_inputs(session: Session) -> None:
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    report = production_release_readiness(session, policy=_policy())
    assert report["status"] == "environment_blocked"
    blockers = report["blockers"]
    assert isinstance(blockers, list)
    assert "production_health_inputs_unavailable" in blockers
    assert "database_schema_unavailable" in blockers


def test_scientific_completion_revalidates_complete_t_graph(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = SimpleNamespace(id=UUID(int=8990))
    persisted = SimpleNamespace(
        id=candidate.id,
        evidence_class="production_authoritative",
        status="completed",
        policy_checksum_sha256=_policy().t_checksum_sha256,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "inflector_data.model_release._holdout_run",
        lambda *args, **kwargs: persisted,
    )
    monkeypatch.setattr(
        "inflector_data.model_release._validated_run_input",
        lambda *args, **kwargs: calls.append("input") or object(),
    )
    monkeypatch.setattr(
        "inflector_data.model_release._validate_completed_holdout_graph",
        lambda *args, **kwargs: (
            calls.append("terminal")
            or SimpleNamespace(decision_code="CHAMPION_RETAINED_NO_CHALLENGER")
        ),
    )
    status = _validated_scientific_status(
        session,
        policy=_policy(),
        t_policy=object(),  # type: ignore[arg-type]
        s2_policy=object(),  # type: ignore[arg-type]
        activation_policy=object(),  # type: ignore[arg-type]
        split_policy=object(),  # type: ignore[arg-type]
        production_t_runs=(candidate,),  # type: ignore[arg-type]
    )
    assert status == "completed"
    assert calls == ["input", "terminal"]


def test_scientific_completion_rejects_corrupt_terminal_t_graph(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = SimpleNamespace(id=UUID(int=8991))
    persisted = SimpleNamespace(
        id=candidate.id,
        evidence_class="production_authoritative",
        status="completed",
        policy_checksum_sha256=_policy().t_checksum_sha256,
    )
    monkeypatch.setattr(
        "inflector_data.model_release._holdout_run",
        lambda *args, **kwargs: persisted,
    )
    monkeypatch.setattr(
        "inflector_data.model_release._validated_run_input",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "inflector_data.model_release._validate_completed_holdout_graph",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tampered terminal graph")),
    )
    with pytest.raises(RuntimeError, match="tampered terminal graph"):
        _validated_scientific_status(
            session,
            policy=_policy(),
            t_policy=object(),  # type: ignore[arg-type]
            s2_policy=object(),  # type: ignore[arg-type]
            activation_policy=object(),  # type: ignore[arg-type]
            split_policy=object(),  # type: ignore[arg-type]
            production_t_runs=(candidate,),  # type: ignore[arg-type]
        )


def test_scheduler_cycle_freezes_active_release_exactly_once(session: Session) -> None:
    _champion_configuration(session)
    deployment = bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    run = _operational_run(session)
    first = freeze_active_release_for_cycle(
        session, policy=_policy(), operational_run_id=run.id, frozen_at=AT
    )
    second = freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=run.id,
        frozen_at=AT + timedelta(hours=1),
    )
    assert first.id == second.id
    assert first.deployment_id == deployment.id
    assert first.subject_code == "champion"


@pytest.mark.parametrize(
    ("field", "value"),
    (("pointer_name", "shadow"), ("pointer_version", 0), ("subject_code", "unknown")),
)
def test_active_pointer_database_constraints(session: Session, field: str, value: object) -> None:
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    pointer = session.get(ActiveModelRelease, "production")
    assert pointer is not None
    setattr(pointer, field, value)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_existing_cycle_binding_survives_active_pointer_switch(session: Session) -> None:
    _champion_configuration(session)
    first = bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    run = _operational_run(session)
    original = freeze_active_release_for_cycle(
        session, policy=_policy(), operational_run_id=run.id, frozen_at=AT
    )
    candidate = create_champion_release_candidate(session, policy=_policy())
    values = _deployment_values(
        policy=_policy(),
        candidate=candidate,
        previous=first,
        approval_id=None,
        reason="test_transition",
        activated_at=AT + timedelta(hours=1),
    )
    second = ModelDeployment(**values)
    session.add(second)
    session.flush()
    pointer = session.get(ActiveModelRelease, "production")
    assert pointer is not None
    pointer.deployment_id = second.id
    pointer.pointer_version += 1
    pointer.updated_at = AT + timedelta(hours=1)
    session.commit()
    resumed = freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=run.id,
        frozen_at=AT + timedelta(hours=2),
    )
    assert resumed.id == original.id
    assert resumed.deployment_id == first.id
    new_run = _operational_run(session, run_id=UUID(int=8201))
    new_binding = freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=new_run.id,
        frozen_at=AT + timedelta(hours=2),
    )
    assert new_binding.deployment_id == second.id


def test_champion_cycle_production_score_exactly_mirrors_v5_without_mutation(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    run = _operational_run(session)
    freeze_active_release_for_cycle(
        session, policy=_policy(), operational_run_id=run.id, frozen_at=AT
    )
    snapshot = _snapshot(session)
    original_final_score = snapshot.final_score
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    context = resolve_cycle_release_context(session, policy=_policy(), operational_run_id=run.id)
    first = project_release_score_for_cycle(
        session, policy=_policy(), context=context, snapshot=snapshot
    )
    second = project_release_score_for_cycle(
        session, policy=_policy(), context=context, snapshot=snapshot
    )
    assert first.id == second.id
    assert first.subject_code == "champion"
    assert first.production_final_score == Decimal("77")
    assert first.rankability_delta == first.coverage_delta == 0
    assert snapshot.final_score == original_final_score


def test_challenger_cycle_uses_exact_frozen_weight_recomposition(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _champion_configuration(session)
    champion = bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    candidate = _fixture_challenger(session)
    values = _deployment_values(
        policy=_policy(),
        candidate=candidate,
        previous=champion,
        approval_id=None,
        reason="fixture_mechanics_activation",
        activated_at=AT + timedelta(hours=1),
    )
    deployment = ModelDeployment(**values)
    session.add(deployment)
    session.flush()
    pointer = session.get(ActiveModelRelease, "production")
    assert pointer is not None
    pointer.deployment_id = deployment.id
    pointer.release_candidate_id = candidate.id
    pointer.subject_code = "challenger"
    pointer.scoring_definition_checksum_sha256 = candidate.scoring_definition_checksum_sha256
    pointer.pointer_version += 1
    pointer.updated_at = AT + timedelta(hours=1)
    session.commit()
    run = _operational_run(session)
    freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=run.id,
        frozen_at=AT + timedelta(hours=2),
    )
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    context = resolve_cycle_release_context(session, policy=_policy(), operational_run_id=run.id)
    projected = project_release_score_for_cycle(
        session, policy=_policy(), context=context, snapshot=snapshot
    )
    assert projected.subject_code == "challenger"
    assert projected.production_final_score == Decimal("80.000")
    assert projected.production_final_score != snapshot.final_score
    assert snapshot.final_score == Decimal("77")
    assert projected.rankable is True
    assert projected.rankability_delta == projected.coverage_delta == 0
    assert session.query(ModelReleaseProductionScore).count() == 1


def test_existing_release_score_projection_tampering_is_rejected(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    run = _operational_run(session)
    freeze_active_release_for_cycle(
        session, policy=_policy(), operational_run_id=run.id, frozen_at=AT
    )
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    context = resolve_cycle_release_context(session, policy=_policy(), operational_run_id=run.id)
    projected = project_release_score_for_cycle(
        session, policy=_policy(), context=context, snapshot=snapshot
    )
    projected.production_final_score = Decimal("76")
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="production score"):
        project_release_score_for_cycle(
            session, policy=_policy(), context=context, snapshot=snapshot
        )


def test_deployment_history_is_fingerprint_validated(session: Session) -> None:
    _champion_configuration(session)
    deployment = bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    assert len(release_history(session, policy=_policy())) == 1
    deployment.activation_reason = "tampered"
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="deployment immutable projection"):
        release_history(session, policy=_policy())


def test_shadow_uses_same_source_state_and_exact_decimal_recomposition(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    event = record_shadow_cycle(
        session,
        policy=_policy(),
        shadow_run_id=run.id,
        cycle_key="cycle-001",
        snapshot_id=snapshot.id,
        observed_at=AT,
        latency_ms=Decimal("12.50"),
    )
    assert event.champion_score == Decimal("77")
    assert event.challenger_score == Decimal("80.000")
    assert event.champion_rankable is event.challenger_rankable is True
    assert event.rankability_delta == event.coverage_delta == 0
    assert event.source_snapshot_fingerprint_sha256 == snapshot.snapshot_fingerprint_sha256


def test_shadow_cannot_emit_a_production_notification(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(ModelReleaseIntegrityError, match="production notification"):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key="cycle-notification",
            snapshot_id=snapshot.id,
            observed_at=AT,
            latency_ms=Decimal("10"),
            production_notification_emitted=True,
        )


def test_shadow_rejects_active_release_change(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    candidate = create_champion_release_candidate(session, policy=_policy())
    pointer, current, _ = resolve_active_release(session, policy=_policy())
    values = _deployment_values(
        policy=_policy(),
        candidate=candidate,
        previous=current,
        approval_id=None,
        reason="fixture_champion_transition",
        activated_at=AT + timedelta(minutes=1),
    )
    changed = ModelDeployment(**values)
    session.add(changed)
    session.flush()
    pointer.deployment_id = changed.id
    pointer.pointer_version += 1
    pointer.updated_at = AT + timedelta(minutes=1)
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="active champion"):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key="cycle-after-switch",
            snapshot_id=snapshot.id,
            observed_at=AT + timedelta(minutes=2),
            latency_ms=Decimal("10"),
        )


@pytest.mark.parametrize(
    ("cycles", "hours", "latency", "expected_failure"),
    [
        (1, 168, "10", "insufficient_completed_cycles"),
        (20, 1, "10", "insufficient_wall_clock_duration"),
        (20, 168, "5001", "latency_envelope_failed"),
    ],
)
def test_burn_in_predeclared_operational_failures(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    cycles: int,
    hours: int,
    latency: str,
    expected_failure: str,
) -> None:
    candidate, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    weights_before = json.dumps(candidate.scoring_definition_json, sort_keys=True)
    for index in range(cycles):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key=f"cycle-{index:03d}",
            snapshot_id=snapshot.id,
            observed_at=AT + timedelta(minutes=index),
            latency_ms=Decimal(latency),
        )
    result = complete_burn_in(
        session,
        policy=_policy(),
        shadow_run_id=run.id,
        completed_at=AT + timedelta(hours=hours),
    )
    assert result.status == "failed"
    failures = result.result_json["failures"]
    assert isinstance(failures, list)
    assert expected_failure in failures
    assert result.future_outcomes_read is False
    assert result.model_tuning_performed is False
    assert json.dumps(candidate.scoring_definition_json, sort_keys=True) == weights_before


def test_successful_burn_in_is_operational_only_and_idempotent(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    for index in range(20):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key=f"cycle-{index:03d}",
            snapshot_id=snapshot.id,
            observed_at=AT + timedelta(hours=index),
            latency_ms=Decimal("25"),
        )
    completed_at = AT + timedelta(hours=168)
    first = complete_burn_in(
        session, policy=_policy(), shadow_run_id=run.id, completed_at=completed_at
    )
    second = complete_burn_in(
        session, policy=_policy(), shadow_run_id=run.id, completed_at=completed_at
    )
    assert first.id == second.id
    assert first.status == "passed"
    assert first.result_json["failures"] == []
    assert first.future_outcomes_read is False
    assert first.model_tuning_performed is False
    assert candidate.status == "burn_in_complete"


def test_changed_burn_in_completion_cannot_rewrite_history(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    for index in range(20):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key=f"cycle-{index:03d}",
            snapshot_id=snapshot.id,
            observed_at=AT + timedelta(hours=index),
            latency_ms=Decimal("25"),
        )
    complete_burn_in(
        session,
        policy=_policy(),
        shadow_run_id=run.id,
        completed_at=AT + timedelta(hours=168),
    )
    with pytest.raises(ModelReleaseIntegrityError, match="burn-in immutable projection"):
        complete_burn_in(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            completed_at=AT + timedelta(hours=169),
        )


def test_passed_burn_in_graph_tampering_is_rejected(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, run = _shadow_run(session, monkeypatch)
    snapshot = _snapshot(session)
    monkeypatch.setattr(
        "inflector_data.model_release.ScoreSnapshotRepository.validate_persisted_snapshot",
        lambda *args, **kwargs: None,
    )
    for index in range(20):
        record_shadow_cycle(
            session,
            policy=_policy(),
            shadow_run_id=run.id,
            cycle_key=f"cycle-{index:03d}",
            snapshot_id=snapshot.id,
            observed_at=AT + timedelta(hours=index),
            latency_ms=Decimal("25"),
        )
    burn_in = complete_burn_in(
        session,
        policy=_policy(),
        shadow_run_id=run.id,
        completed_at=AT + timedelta(hours=168),
    )
    burn_in.result_json = {**burn_in.result_json, "completed_cycle_count": 999}
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="burn-in immutable projection"):
        _validate_burn_in_graph(session, burn_in=burn_in, candidate=candidate, policy=_policy())


def _stub_authoritative_t_pass(
    monkeypatch: pytest.MonkeyPatch, candidate: ModelReleaseCandidate
) -> None:
    run = SimpleNamespace(id=candidate.temporal_holdout_run_id)
    decision = SimpleNamespace(
        id=candidate.temporal_holdout_decision_id,
        decision_fingerprint_sha256=candidate.temporal_holdout_decision_fingerprint_sha256,
    )
    monkeypatch.setattr(
        "inflector_data.model_release._authoritative_t_pass",
        lambda *args, **kwargs: (run, decision, SimpleNamespace()),
    )


def test_activation_without_persisted_approval_preserves_champion(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, _ = _passed_burn_in(session, monkeypatch)
    _stub_authoritative_t_pass(monkeypatch, candidate)
    with pytest.raises(ModelReleaseIntegrityError, match="manual activation approval"):
        activate_release(
            session,
            policy=_policy(),
            t_policy=object(),  # type: ignore[arg-type]
            s2_policy=object(),  # type: ignore[arg-type]
            activation_policy=object(),  # type: ignore[arg-type]
            split_policy=object(),  # type: ignore[arg-type]
            release_candidate_id=candidate.id,
            approval_id=UUID(int=8999),
            activated_at=AT + timedelta(days=8),
        )
    _, _, active = resolve_active_release(session, policy=_policy())
    assert active.subject_code == "champion"


def test_persisted_manual_approval_activates_exact_challenger_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, burn_in = _passed_burn_in(session, monkeypatch)
    approval = create_activation_approval(
        session,
        policy=_policy(),
        release_candidate_id=candidate.id,
        burn_in_run_id=burn_in.id,
        approver_provenance="operator:fixture-review",
        approved_at=AT + timedelta(days=7, hours=1),
    )
    _stub_authoritative_t_pass(monkeypatch, candidate)
    deployment = activate_release(
        session,
        policy=_policy(),
        t_policy=object(),  # type: ignore[arg-type]
        s2_policy=object(),  # type: ignore[arg-type]
        activation_policy=object(),  # type: ignore[arg-type]
        split_policy=object(),  # type: ignore[arg-type]
        release_candidate_id=candidate.id,
        approval_id=approval.id,
        activated_at=AT + timedelta(days=8),
    )
    pointer, active_deployment, active = resolve_active_release(session, policy=_policy())
    assert deployment.id == active_deployment.id
    assert active.id == candidate.id
    assert pointer.subject_code == "challenger"
    assert pointer.pointer_version == 2


def test_rollback_restores_previous_champion_and_preserves_history(session: Session) -> None:
    _champion_configuration(session)
    champion_deployment = bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    challenger = _fixture_challenger(session)
    challenger.status = "active"
    values = _deployment_values(
        policy=_policy(),
        candidate=challenger,
        previous=champion_deployment,
        approval_id=UUID(int=8500),
        reason="manual_challenger_activation_after_t_and_burn_in",
        activated_at=AT + timedelta(days=1),
    )
    challenger_deployment = ModelDeployment(**values)
    session.add(challenger_deployment)
    session.flush()
    pointer = session.get(ActiveModelRelease, "production")
    assert pointer is not None
    pointer.deployment_id = challenger_deployment.id
    pointer.release_candidate_id = challenger.id
    pointer.subject_code = "challenger"
    pointer.scoring_definition_checksum_sha256 = challenger.scoring_definition_checksum_sha256
    pointer.pointer_version += 1
    pointer.updated_at = AT + timedelta(days=1)
    session.commit()
    bound_run = _operational_run(session)
    challenger_binding = freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=bound_run.id,
        frozen_at=AT + timedelta(days=1, hours=1),
    )
    assert challenger_binding.deployment_id == challenger_deployment.id

    rollback = rollback_release(
        session,
        policy=_policy(),
        reason_code="manual_emergency_rollback",
        health_evidence={"incident": "fixture"},
        rolled_back_at=AT + timedelta(days=2),
    )
    pointer, deployment, candidate = resolve_active_release(session, policy=_policy())
    assert rollback.rolled_back_deployment_id == challenger_deployment.id
    assert candidate.subject_code == "champion"
    assert deployment.activation_reason == "rollback:manual_emergency_rollback"
    assert pointer.pointer_version == 3
    assert len(release_history(session, policy=_policy())) == 3
    resumed = freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=bound_run.id,
        frozen_at=AT + timedelta(days=2, hours=1),
    )
    assert resumed.id == challenger_binding.id
    assert resumed.subject_code == "challenger"
    new_run = _operational_run(session, run_id=UUID(int=8202))
    post_rollback = freeze_active_release_for_cycle(
        session,
        policy=_policy(),
        operational_run_id=new_run.id,
        frozen_at=AT + timedelta(days=2, hours=1),
    )
    assert post_rollback.subject_code == "champion"
    with pytest.raises(ModelReleaseIntegrityError, match="already a rollback"):
        rollback_release(
            session,
            policy=_policy(),
            reason_code="manual_emergency_rollback",
            health_evidence={},
            rolled_back_at=AT + timedelta(days=3),
        )

    stored = session.get(ModelRollback, rollback.id)
    assert stored is not None
    stored.health_evidence_json = {"incident": "tampered"}
    session.commit()
    with pytest.raises(ModelReleaseIntegrityError, match="rollback immutable projection"):
        release_history(session, policy=_policy())


def test_rollback_reason_must_be_predeclared(session: Session) -> None:
    _champion_configuration(session)
    bootstrap_active_champion(session, policy=_policy(), activated_at=AT)
    with pytest.raises(ValueError, match="predeclared"):
        rollback_release(
            session,
            policy=_policy(),
            reason_code="observed_after_the_fact",
            health_evidence={},
            rolled_back_at=AT + timedelta(days=1),
        )


def test_environment_blocked_readiness_never_leaks_secrets() -> None:
    report = production_release_readiness(None, policy=_policy(), database_available=False)
    assert report["message"] == "REAL PRODUCTION U: ENVIRONMENT-BLOCKED"
    assert "secret" not in canonical_json_sha256(report)


def test_cli_has_release_commands_and_no_tuning_search_training_or_auto_promotion() -> None:
    commands: set[str] = set()
    for action in _parser()._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            commands = set(action.choices)
    assert {
        "model-release-readiness",
        "bootstrap-active-champion",
        "create-release-candidate",
        "start-shadow-run",
        "record-shadow-cycle",
        "complete-burn-in",
        "create-activation-approval",
        "activate-release",
        "rollback-release",
        "show-active-release",
        "release-history",
    } <= commands
    assert not any(
        token in command
        for command in commands
        for token in ("tune", "search", "train", "auto-promote")
    )


def test_migration_0028_upgrade_and_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "u-migration.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "20261009_0028")
    tables = set(inspect(create_engine(f"sqlite:///{database.as_posix()}")).get_table_names())
    assert {
        "model_release_candidates",
        "model_shadow_runs",
        "model_shadow_events",
        "model_burn_in_runs",
        "model_activation_approvals",
        "model_deployments",
        "active_model_release",
        "model_rollbacks",
        "model_release_cycle_bindings",
        "model_release_production_scores",
    } <= tables
    command.downgrade(config, "20261008_0027")
    tables = set(inspect(create_engine(f"sqlite:///{database.as_posix()}")).get_table_names())
    assert "model_release_candidates" not in tables
