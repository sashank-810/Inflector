"""Immutable persistence boundary for versioned score audits."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.balance_sheet_scoring import BALANCE_SHEET_COMPONENT_VERSION
from inflector_core.business_catalyst_scoring import BUSINESS_CATALYST_COMPONENT_VERSION
from inflector_core.business_quality_scoring import BUSINESS_QUALITY_COMPONENT_VERSION
from inflector_core.cash_flow_quality_scoring import CASH_FLOW_QUALITY_COMPONENT_VERSION
from inflector_core.component_scoring import FINANCIAL_INFLECTION_COMPONENT_VERSION
from inflector_core.low_market_attention_scoring import (
    LOW_MARKET_ATTENTION_COMPONENT_VERSION,
)
from inflector_core.market_structure_scoring import MARKET_STRUCTURE_COMPONENT_VERSION
from inflector_core.score_audit import audit_fingerprint_sha256, canonical_audit_value
from inflector_core.valuation_scoring import VALUATION_COMPONENT_VERSION
from inflector_database.models import ScoreComponent, ScoreExplanation, ScoreSnapshot, Security

V1_SNAPSHOT_STATUSES = frozenset(
    {"ineligible", "financial_inflection_unavailable", "partial_component_set"}
)
V2_SNAPSHOT_STATUSES = frozenset(
    {"ineligible", "financial_components_unavailable", "partial_component_set"}
)
V2_COMPONENT_CODES = frozenset(
    {"financial_inflection", "business_quality", "cash_flow_quality", "balance_sheet"}
)
V3_SNAPSHOT_STATUSES = frozenset(
    {"ineligible", "implemented_components_unavailable", "partial_component_set"}
)
V3_COMPONENT_CODES = frozenset(
    {
        "financial_inflection",
        "business_quality",
        "cash_flow_quality",
        "balance_sheet",
        "valuation",
        "market_structure",
    }
)
V4_SNAPSHOT_STATUSES = frozenset(
    {"ineligible", "implemented_components_unavailable", "partial_component_set"}
)
V4_COMPONENT_ORDER = (
    "financial_inflection",
    "business_catalyst",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
    "valuation",
    "market_structure",
)
V4_COMPONENT_CODES = frozenset(V4_COMPONENT_ORDER)
V5_SNAPSHOT_STATUSES = frozenset(
    {
        "ineligible",
        "implemented_components_unavailable",
        "partial_component_set",
        "final_score_available",
    }
)
V5_COMPONENT_ORDER = (
    "financial_inflection",
    "business_catalyst",
    "business_quality",
    "cash_flow_quality",
    "balance_sheet",
    "valuation",
    "market_structure",
    "low_market_attention",
)
V5_COMPONENT_CODES = frozenset(V5_COMPONENT_ORDER)
V5_COMPONENT_ALGORITHM_VERSIONS = {
    "financial_inflection": FINANCIAL_INFLECTION_COMPONENT_VERSION,
    "business_catalyst": BUSINESS_CATALYST_COMPONENT_VERSION,
    "business_quality": BUSINESS_QUALITY_COMPONENT_VERSION,
    "cash_flow_quality": CASH_FLOW_QUALITY_COMPONENT_VERSION,
    "balance_sheet": BALANCE_SHEET_COMPONENT_VERSION,
    "valuation": VALUATION_COMPONENT_VERSION,
    "market_structure": MARKET_STRUCTURE_COMPONENT_VERSION,
    "low_market_attention": LOW_MARKET_ATTENTION_COMPONENT_VERSION,
}
V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION = "opportunity_score_weighted_sum_v1"


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
    selected_security_id: UUID | None
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
            selected_security_id=value.selected_security_id,
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
            final_score=(
                value.final_score
                if value.algorithm_version == "score_snapshot_v5"
                else None
            ),
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
                final_contribution=(
                    component_value.final_contribution
                    if value.algorithm_version == "score_snapshot_v5"
                    else None
                ),
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
            select(ScoreSnapshot).where(ScoreSnapshot.snapshot_fingerprint_sha256 == fingerprint)
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

    def validate_persisted_snapshot(self, record: ScoreSnapshot) -> ScoreSnapshot:
        """Apply the versioned immutable integrity contract to an already-read row."""

        return self._validated(record)

    def validate_security_company(self, security_id: UUID, company_id: UUID) -> None:
        security = self._session.get(Security, security_id)
        if security is None:
            raise ValueError("orchestration security does not exist")
        if security.company_id != company_id:
            raise ValueError("orchestration security does not belong to company")

    @staticmethod
    def _validated(record: ScoreSnapshot) -> ScoreSnapshot:
        expected = audit_fingerprint_sha256(record.fingerprint_payload_json)
        if expected != record.snapshot_fingerprint_sha256:
            raise ScoreSnapshotIntegrityError("persisted score snapshot fingerprint mismatch")
        payload_version = record.fingerprint_payload_json.get("algorithm_version")
        if payload_version != record.algorithm_version:
            raise ScoreSnapshotIntegrityError("snapshot algorithm version does not match payload")
        if record.algorithm_version == "score_snapshot_v1":
            statuses = V1_SNAPSHOT_STATUSES
            component_codes = frozenset({"financial_inflection"})
        elif record.algorithm_version == "score_snapshot_v2":
            statuses = V2_SNAPSHOT_STATUSES
            component_codes = V2_COMPONENT_CODES
        elif record.algorithm_version == "score_snapshot_v3":
            statuses = V3_SNAPSHOT_STATUSES
            component_codes = V3_COMPONENT_CODES
        elif record.algorithm_version == "score_snapshot_v4":
            statuses = V4_SNAPSHOT_STATUSES
            component_codes = V4_COMPONENT_CODES
        elif record.algorithm_version == "score_snapshot_v5":
            statuses = V5_SNAPSHOT_STATUSES
            component_codes = V5_COMPONENT_CODES
        else:
            raise ScoreSnapshotIntegrityError("unsupported persisted snapshot algorithm version")
        if record.snapshot_status not in statuses:
            raise ScoreSnapshotIntegrityError("invalid persisted status for algorithm version")
        if record.algorithm_version in {
            "score_snapshot_v3",
            "score_snapshot_v4",
            "score_snapshot_v5",
        }:
            if record.selected_security_id is None:
                raise ScoreSnapshotIntegrityError("v3/v4/v5 snapshot requires selected security")
        elif record.selected_security_id is not None:
            raise ScoreSnapshotIntegrityError("v1/v2 snapshot must not select a security")
        if record.algorithm_version == "score_snapshot_v5":
            for component in record.components:
                if component.component_code not in component_codes:
                    raise ScoreSnapshotIntegrityError(
                        "persisted component is invalid for snapshot algorithm version"
                    )
            ScoreSnapshotRepository._validate_v5_record(record)
            return record
        if record.final_score is not None:
            raise ScoreSnapshotIntegrityError("partial persisted snapshot has a final score")
        if record.snapshot_status != "partial_component_set" and record.components:
            raise ScoreSnapshotIntegrityError("non-partial persisted snapshot has components")
        if record.snapshot_status == "partial_component_set" and not record.components:
            raise ScoreSnapshotIntegrityError("partial persisted snapshot has no components")
        for component in record.components:
            if component.component_code not in component_codes:
                raise ScoreSnapshotIntegrityError(
                    "persisted component is invalid for snapshot algorithm version"
                )
            if component.final_contribution is not None:
                raise ScoreSnapshotIntegrityError(
                    "partial persisted component has a final contribution"
                )
        if record.algorithm_version == "score_snapshot_v4":
            ScoreSnapshotRepository._validate_v4_record(record)
        return record

    @staticmethod
    def _validate_write(value: ScoreSnapshotWrite) -> None:
        if value.algorithm_version == "score_snapshot_v1":
            statuses = V1_SNAPSHOT_STATUSES
            component_codes = frozenset({"financial_inflection"})
        elif value.algorithm_version == "score_snapshot_v2":
            statuses = V2_SNAPSHOT_STATUSES
            component_codes = V2_COMPONENT_CODES
        elif value.algorithm_version == "score_snapshot_v3":
            statuses = V3_SNAPSHOT_STATUSES
            component_codes = V3_COMPONENT_CODES
        elif value.algorithm_version == "score_snapshot_v4":
            statuses = V4_SNAPSHOT_STATUSES
            component_codes = V4_COMPONENT_CODES
        elif value.algorithm_version == "score_snapshot_v5":
            statuses = V5_SNAPSHOT_STATUSES
            component_codes = V5_COMPONENT_CODES
        else:
            raise ValueError("unsupported score snapshot algorithm version")
        if value.snapshot_status not in statuses:
            raise ValueError("invalid snapshot status for algorithm version")
        if value.algorithm_version in {
            "score_snapshot_v3",
            "score_snapshot_v4",
            "score_snapshot_v5",
        }:
            if value.selected_security_id is None:
                raise ValueError("v3/v4/v5 snapshot requires selected_security_id")
        elif value.selected_security_id is not None:
            raise ValueError("v1/v2 snapshot selected_security_id must be None")
        if value.algorithm_version == "score_snapshot_v5":
            for component in value.components:
                if component.component_code not in component_codes:
                    raise ValueError("component code is invalid for snapshot algorithm version")
            ScoreSnapshotRepository._validate_v5_write(value)
            return
        if value.final_score is not None:
            raise ValueError("partial snapshot final_score must be None")
        if value.snapshot_status != "partial_component_set" and value.components:
            raise ValueError("only a partial_component_set snapshot may have components")
        if value.snapshot_status == "partial_component_set" and not value.components:
            raise ValueError("a partial_component_set snapshot requires components")
        for component in value.components:
            if component.component_code not in component_codes:
                raise ValueError("component code is invalid for snapshot algorithm version")
            if component.final_contribution is not None:
                raise ValueError("partial snapshot final component contribution must be None")
        if value.algorithm_version == "score_snapshot_v4":
            ScoreSnapshotRepository._validate_v4_write(value)

    @staticmethod
    def _validate_v4_write(value: ScoreSnapshotWrite) -> None:
        codes = tuple(component.component_code for component in value.components)
        if len(codes) != len(set(codes)):
            raise ValueError("v4 component codes must be unique")
        canonical_codes = tuple(code for code in V4_COMPONENT_ORDER if code in set(codes))
        if codes != canonical_codes:
            raise ValueError("v4 components must use canonical component ordering")
        if value.available_component_codes_json != list(canonical_codes):
            raise ValueError("v4 available component codes do not match child components")
        coverage = sum(
            (component.configured_top_level_weight for component in value.components),
            Decimal("0"),
        )
        if value.top_level_component_weight_coverage != coverage:
            raise ValueError("v4 top-level coverage does not equal persisted component weights")
        business_catalyst = next(
            (
                component
                for component in value.components
                if component.component_code == "business_catalyst"
            ),
            None,
        )
        if (
            business_catalyst is not None
            and business_catalyst.algorithm_version != BUSINESS_CATALYST_COMPONENT_VERSION
        ):
            raise ValueError("v4 business catalyst component has an invalid algorithm version")
        if value.fingerprint_payload_json.get("algorithm_version") != value.algorithm_version:
            raise ValueError("v4 fingerprint payload algorithm version mismatch")

    @staticmethod
    def _validate_v4_record(record: ScoreSnapshot) -> None:
        codes = tuple(component.component_code for component in record.components)
        if len(codes) != len(set(codes)):
            raise ScoreSnapshotIntegrityError("v4 persisted component codes are not unique")
        canonical_codes = [code for code in V4_COMPONENT_ORDER if code in set(codes)]
        if record.available_component_codes_json != canonical_codes:
            raise ScoreSnapshotIntegrityError(
                "v4 available component codes do not match persisted components"
            )
        coverage = sum(
            (component.configured_top_level_weight for component in record.components),
            Decimal("0"),
        )
        if record.top_level_component_weight_coverage != coverage:
            raise ScoreSnapshotIntegrityError(
                "v4 top-level coverage does not equal persisted component weights"
            )
        business_catalyst = next(
            (
                component
                for component in record.components
                if component.component_code == "business_catalyst"
            ),
            None,
        )
        if (
            business_catalyst is not None
            and business_catalyst.algorithm_version != BUSINESS_CATALYST_COMPONENT_VERSION
        ):
            raise ScoreSnapshotIntegrityError(
                "v4 business catalyst component has an invalid algorithm version"
            )

    @staticmethod
    def _validate_v5_write(value: ScoreSnapshotWrite) -> None:
        codes = tuple(component.component_code for component in value.components)
        if len(codes) != len(set(codes)):
            raise ValueError("v5 component codes must be unique")
        canonical_codes = tuple(code for code in V5_COMPONENT_ORDER if code in set(codes))
        if codes != canonical_codes:
            raise ValueError("v5 components must use canonical component ordering")
        if value.available_component_codes_json != list(canonical_codes):
            raise ValueError("v5 available component codes do not match child components")
        ScoreSnapshotRepository._validate_v5_code_partition(
            available=list(canonical_codes),
            missing=value.missing_component_codes_json,
            fingerprint_payload=value.fingerprint_payload_json,
            error_type=ValueError,
        )
        coverage = sum(
            (component.configured_top_level_weight for component in value.components),
            Decimal("0"),
        )
        if value.top_level_component_weight_coverage != coverage:
            raise ValueError("v5 top-level coverage does not equal persisted component weights")
        for component in value.components:
            ScoreSnapshotRepository._validate_v5_component(
                component,
                error_type=ValueError,
            )
        ScoreSnapshotRepository._validate_v5_state(
            status=value.snapshot_status,
            coverage=value.top_level_component_weight_coverage,
            available=value.available_component_codes_json,
            missing=value.missing_component_codes_json,
            final_score=value.final_score,
            components=value.components,
            error_type=ValueError,
        )
        ScoreSnapshotRepository._validate_v5_fingerprint_state(
            fingerprint_payload=value.fingerprint_payload_json,
            final_score=value.final_score,
            components=value.components,
            error_type=ValueError,
        )
        if value.fingerprint_payload_json.get("algorithm_version") != value.algorithm_version:
            raise ValueError("v5 fingerprint payload algorithm version mismatch")

    @staticmethod
    def _validate_v5_record(record: ScoreSnapshot) -> None:
        rank = {code: index for index, code in enumerate(V5_COMPONENT_ORDER)}
        record.components.sort(key=lambda component: rank.get(component.component_code, 999))
        codes = tuple(component.component_code for component in record.components)
        if len(codes) != len(set(codes)):
            raise ScoreSnapshotIntegrityError("v5 persisted component codes are not unique")
        canonical_codes = tuple(code for code in V5_COMPONENT_ORDER if code in set(codes))
        if codes != canonical_codes:
            raise ScoreSnapshotIntegrityError(
                "v5 persisted components do not use canonical ordering"
            )
        if record.available_component_codes_json != list(canonical_codes):
            raise ScoreSnapshotIntegrityError(
                "v5 available component codes do not match persisted components"
            )
        ScoreSnapshotRepository._validate_v5_code_partition(
            available=list(canonical_codes),
            missing=record.missing_component_codes_json,
            fingerprint_payload=record.fingerprint_payload_json,
            error_type=ScoreSnapshotIntegrityError,
        )
        coverage = sum(
            (component.configured_top_level_weight for component in record.components),
            Decimal("0"),
        )
        if record.top_level_component_weight_coverage != coverage:
            raise ScoreSnapshotIntegrityError(
                "v5 top-level coverage does not equal persisted component weights"
            )
        for component in record.components:
            ScoreSnapshotRepository._validate_v5_component(
                component,
                error_type=ScoreSnapshotIntegrityError,
            )
        ScoreSnapshotRepository._validate_v5_state(
            status=record.snapshot_status,
            coverage=record.top_level_component_weight_coverage,
            available=record.available_component_codes_json,
            missing=record.missing_component_codes_json,
            final_score=record.final_score,
            components=tuple(record.components),
            error_type=ScoreSnapshotIntegrityError,
        )
        ScoreSnapshotRepository._validate_v5_fingerprint_state(
            fingerprint_payload=record.fingerprint_payload_json,
            final_score=record.final_score,
            components=tuple(record.components),
            error_type=ScoreSnapshotIntegrityError,
        )

    @staticmethod
    def _validate_v5_code_partition(
        *,
        available: Sequence[object],
        missing: Sequence[object],
        fingerprint_payload: dict[str, object],
        error_type: type[Exception],
    ) -> None:
        if len(missing) != len(set(missing)) or any(
            code not in V5_COMPONENT_CODES for code in missing
        ):
            raise error_type("v5 missing component codes are invalid")
        canonical_missing = [code for code in V5_COMPONENT_ORDER if code in set(missing)]
        if missing != canonical_missing:
            raise error_type("v5 missing component codes are not canonically ordered")
        state = fingerprint_payload.get("final_score_state")
        if not isinstance(state, dict):
            raise error_type("v5 fingerprint omits final score state")
        required = state.get("required_positive_weight_component_codes")
        if not isinstance(required, list):
            raise error_type("v5 fingerprint omits required component codes")
        canonical_required = [code for code in V5_COMPONENT_ORDER if code in set(required)]
        if required != canonical_required or len(required) != len(set(required)):
            raise error_type("v5 required component codes are invalid")
        if set(available).intersection(missing) or set(available).union(missing) != set(
            required
        ):
            raise error_type("v5 available and missing codes do not partition requirements")

    @staticmethod
    def _validate_v5_component(component: object, *, error_type: type[Exception]) -> None:
        code = getattr(component, "component_code")
        score = getattr(component, "score")
        unit = getattr(component, "unit")
        weight = getattr(component, "configured_top_level_weight")
        subfactor_coverage = getattr(component, "subfactor_weight_coverage")
        algorithm_version = getattr(component, "algorithm_version")
        if code not in V5_COMPONENT_CODES:
            raise error_type("v5 component code is unsupported")
        if not isinstance(score, Decimal) or not Decimal("0") <= score <= Decimal("100"):
            raise error_type("v5 component score must be an exact Decimal in [0, 100]")
        if unit != "score_0_100":
            raise error_type("v5 component unit must be score_0_100")
        if not isinstance(weight, Decimal) or weight <= 0:
            raise error_type("v5 persisted component weight must be positive")
        if (
            not isinstance(subfactor_coverage, Decimal)
            or not Decimal("0") <= subfactor_coverage <= Decimal("1")
        ):
            raise error_type("v5 subfactor coverage must be an exact Decimal in [0, 1]")
        if algorithm_version != V5_COMPONENT_ALGORITHM_VERSIONS[code]:
            raise error_type("v5 component algorithm version is invalid")

    @staticmethod
    def _validate_v5_state(
        *,
        status: str,
        coverage: Decimal,
        available: Sequence[object],
        missing: Sequence[object],
        final_score: Decimal | None,
        components: tuple[object, ...],
        error_type: type[Exception],
    ) -> None:
        contributions = tuple(
            getattr(component, "final_contribution") for component in components
        )
        if status in {"ineligible", "implemented_components_unavailable"}:
            if components or coverage != 0 or available or final_score is not None:
                raise error_type("v5 unavailable snapshot contains score components")
            if any(contribution is not None for contribution in contributions):
                raise error_type("v5 unavailable snapshot contains final contributions")
            return
        if status == "partial_component_set":
            if not components or not Decimal("0") < coverage < Decimal("1"):
                raise error_type("v5 partial snapshot has invalid component coverage")
            if not missing:
                raise error_type("v5 partial snapshot must retain missing components")
            if final_score is not None or any(
                contribution is not None for contribution in contributions
            ):
                raise error_type("v5 partial snapshot contains final score values")
            return
        if status != "final_score_available":
            raise error_type("v5 snapshot status is invalid")
        if coverage != Decimal("1") or missing:
            raise error_type("v5 final snapshot is not fully covered")
        if (
            not isinstance(final_score, Decimal)
            or not Decimal("0") <= final_score <= Decimal("100")
        ):
            raise error_type("v5 final score must be an exact Decimal in [0, 100]")
        expected_contributions: list[Decimal] = []
        for component in components:
            contribution = getattr(component, "final_contribution")
            expected = getattr(component, "score") * getattr(
                component, "configured_top_level_weight"
            )
            if not isinstance(contribution, Decimal) or contribution != expected:
                raise error_type("v5 component final contribution is not exact")
            expected_contributions.append(contribution)
        if sum(expected_contributions, Decimal("0")) != final_score:
            raise error_type("v5 final score does not equal exact contribution sum")

    @staticmethod
    def _validate_v5_fingerprint_state(
        *,
        fingerprint_payload: dict[str, object],
        final_score: Decimal | None,
        components: tuple[object, ...],
        error_type: type[Exception],
    ) -> None:
        if (
            fingerprint_payload.get("opportunity_score_aggregation_version")
            != V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION
        ):
            raise error_type("v5 fingerprint aggregation version is invalid")
        state = fingerprint_payload.get("final_score_state")
        if not isinstance(state, dict):
            raise error_type("v5 fingerprint omits final score state")
        if state.get("aggregation_version") != V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION:
            raise error_type("v5 final score state aggregation version is invalid")
        expected_components = canonical_audit_value(
            [
                {
                    "component_code": getattr(component, "component_code"),
                    "score": getattr(component, "score"),
                    "configured_top_level_weight": getattr(
                        component, "configured_top_level_weight"
                    ),
                    "final_contribution": getattr(component, "final_contribution"),
                }
                for component in components
            ]
        )
        if state.get("components") != expected_components:
            raise error_type("v5 fingerprint component final state does not match record")
        if state.get("final_score") != canonical_audit_value(final_score):
            raise error_type("v5 fingerprint final score state does not match record")

    @classmethod
    def _optional_utc(cls, value: datetime | None, field_name: str) -> datetime | None:
        return None if value is None else cls._aware_utc(value, field_name)

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)
