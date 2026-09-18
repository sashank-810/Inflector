"""Immutable persistence boundary for partial Phase 4C score audits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.score_audit import audit_fingerprint_sha256
from inflector_database.models import ScoreComponent, ScoreExplanation, ScoreSnapshot

VALID_SNAPSHOT_STATUSES = frozenset(
    {"ineligible", "financial_inflection_unavailable", "partial_component_set"}
)


class ScoreSnapshotIntegrityError(RuntimeError):
    """Raised when persisted fingerprint content no longer matches its checksum."""


@dataclass(frozen=True, slots=True)
class ScoreExplanationWrite:
    factor_code: str
    rank: int
    raw_value: Decimal
    raw_unit: str
    normalized_score: Decimal
    configured_weight: Decimal
    effective_weight: Decimal
    component_contribution: Decimal
    input_available_at: datetime
    evidence_type: str
    template_code: str
    direction: str | None
    evidence_manifest_json: dict[str, object]


@dataclass(frozen=True, slots=True)
class ScoreComponentWrite:
    component_code: str
    score: Decimal | None
    unit: str
    configured_top_level_weight: Decimal
    subfactor_weight_coverage: Decimal
    final_contribution: Decimal | None
    available_at: datetime | None
    algorithm_version: str
    missing_subfactors_json: list[str]
    warnings_json: list[str]
    detail_json: dict[str, object]
    explanations: tuple[ScoreExplanationWrite, ...]


@dataclass(frozen=True, slots=True)
class ScoreSnapshotWrite:
    company_id: UUID
    model_version_id: UUID
    scoring_configuration_id: UUID
    configuration_checksum_sha256: str
    as_of_date: date
    knowledge_cutoff: datetime
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    selected_provider_dataset_id: UUID | None
    selected_filing_scope: str | None
    snapshot_status: str
    eligibility_eligible: bool
    eligibility_inputs_json: dict[str, object]
    eligibility_reasons_json: list[str]
    eligibility_warnings_json: list[str]
    financial_core_coverage: Decimal
    confidence: Decimal
    confidence_inputs_json: dict[str, object]
    confidence_details_json: dict[str, object]
    top_level_component_weight_coverage: Decimal
    available_component_codes_json: list[str]
    missing_component_codes_json: list[str]
    context_resolution_json: dict[str, object]
    input_manifest_json: dict[str, object]
    fingerprint_payload_json: dict[str, object]
    final_score: Decimal | None
    algorithm_version: str
    components: tuple[ScoreComponentWrite, ...]


@dataclass(frozen=True, slots=True)
class PersistedScoreSnapshotResult:
    record: ScoreSnapshot
    created: bool


class ScoreSnapshotRepository:
    """Create and read immutable snapshots without update operations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def persist_snapshot(self, value: ScoreSnapshotWrite) -> PersistedScoreSnapshotResult:
        self._validate_write(value)
        fingerprint = audit_fingerprint_sha256(value.fingerprint_payload_json)
        existing = self.get_by_fingerprint(fingerprint)
        if existing is not None:
            return PersistedScoreSnapshotResult(record=existing, created=False)

        record = ScoreSnapshot(
            company_id=value.company_id,
            model_version_id=value.model_version_id,
            scoring_configuration_id=value.scoring_configuration_id,
            configuration_checksum_sha256=value.configuration_checksum_sha256,
            as_of_date=value.as_of_date,
            knowledge_cutoff=self._aware_utc(value.knowledge_cutoff, "knowledge_cutoff"),
            ending_fiscal_year=value.ending_fiscal_year,
            ending_fiscal_quarter=value.ending_fiscal_quarter,
            selected_provider_dataset_id=value.selected_provider_dataset_id,
            selected_filing_scope=value.selected_filing_scope,
            snapshot_status=value.snapshot_status,
            eligibility_eligible=value.eligibility_eligible,
            eligibility_inputs_json=value.eligibility_inputs_json,
            eligibility_reasons_json=value.eligibility_reasons_json,
            eligibility_warnings_json=value.eligibility_warnings_json,
            financial_core_coverage=value.financial_core_coverage,
            confidence=value.confidence,
            confidence_inputs_json=value.confidence_inputs_json,
            confidence_details_json=value.confidence_details_json,
            top_level_component_weight_coverage=value.top_level_component_weight_coverage,
            available_component_codes_json=value.available_component_codes_json,
            missing_component_codes_json=value.missing_component_codes_json,
            context_resolution_json=value.context_resolution_json,
            input_manifest_json=value.input_manifest_json,
            fingerprint_payload_json=value.fingerprint_payload_json,
            snapshot_fingerprint_sha256=fingerprint,
            final_score=None,
            algorithm_version=value.algorithm_version,
        )
        self._session.add(record)
        self._session.flush()
        for component_value in value.components:
            component = ScoreComponent(
                score_snapshot_id=record.id,
                component_code=component_value.component_code,
                score=component_value.score,
                unit=component_value.unit,
                configured_top_level_weight=component_value.configured_top_level_weight,
                subfactor_weight_coverage=component_value.subfactor_weight_coverage,
                final_contribution=None,
                available_at=self._optional_utc(
                    component_value.available_at, "component available_at"
                ),
                algorithm_version=component_value.algorithm_version,
                missing_subfactors_json=component_value.missing_subfactors_json,
                warnings_json=component_value.warnings_json,
                detail_json=component_value.detail_json,
            )
            self._session.add(component)
            self._session.flush()
            for explanation_value in component_value.explanations:
                self._session.add(
                    ScoreExplanation(
                        score_snapshot_id=record.id,
                        score_component_id=component.id,
                        factor_code=explanation_value.factor_code,
                        rank=explanation_value.rank,
                        raw_value=explanation_value.raw_value,
                        raw_unit=explanation_value.raw_unit,
                        normalized_score=explanation_value.normalized_score,
                        configured_weight=explanation_value.configured_weight,
                        effective_weight=explanation_value.effective_weight,
                        component_contribution=explanation_value.component_contribution,
                        input_available_at=self._aware_utc(
                            explanation_value.input_available_at,
                            "explanation input_available_at",
                        ),
                        evidence_type=explanation_value.evidence_type,
                        template_code=explanation_value.template_code,
                        direction=explanation_value.direction,
                        evidence_manifest_json=explanation_value.evidence_manifest_json,
                    )
                )
        self._session.flush()
        self._session.refresh(record)
        return PersistedScoreSnapshotResult(record=record, created=True)

    def get_score_snapshot(self, snapshot_id: UUID) -> ScoreSnapshot | None:
        record = self._session.get(ScoreSnapshot, snapshot_id)
        return None if record is None else self._validated(record)

    def get_by_fingerprint(self, fingerprint: str) -> ScoreSnapshot | None:
        record = self._session.scalar(
            select(ScoreSnapshot).where(
                ScoreSnapshot.snapshot_fingerprint_sha256 == fingerprint
            )
        )
        return None if record is None else self._validated(record)

    def list_company_score_snapshots(self, company_id: UUID) -> tuple[ScoreSnapshot, ...]:
        records = tuple(
            self._session.scalars(
                select(ScoreSnapshot)
                .where(ScoreSnapshot.company_id == company_id)
                .order_by(
                    ScoreSnapshot.knowledge_cutoff.desc(),
                    ScoreSnapshot.created_at.desc(),
                    ScoreSnapshot.id.desc(),
                )
            )
        )
        return tuple(self._validated(record) for record in records)

    @staticmethod
    def _validated(record: ScoreSnapshot) -> ScoreSnapshot:
        expected = audit_fingerprint_sha256(record.fingerprint_payload_json)
        if expected != record.snapshot_fingerprint_sha256:
            raise ScoreSnapshotIntegrityError("persisted score snapshot fingerprint mismatch")
        return record

    @staticmethod
    def _validate_write(value: ScoreSnapshotWrite) -> None:
        if value.snapshot_status not in VALID_SNAPSHOT_STATUSES:
            raise ValueError("invalid Phase 4C snapshot status")
        if value.final_score is not None:
            raise ValueError("Phase 4C final_score must be None")
        if value.snapshot_status != "partial_component_set" and value.components:
            raise ValueError("only a partial_component_set snapshot may have components")
        for component in value.components:
            if component.component_code != "financial_inflection":
                raise ValueError("Phase 4C only persists financial_inflection")
            if component.final_contribution is not None:
                raise ValueError("Phase 4C final component contribution must be None")

    @classmethod
    def _optional_utc(cls, value: datetime | None, field_name: str) -> datetime | None:
        return None if value is None else cls._aware_utc(value, field_name)

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)
