"""Immutable Phase 4 scoring policy, context, eligibility, and confidence contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

CONFIDENCE_VERSION = "confidence_v1"
VALID_FILING_SCOPES = frozenset({"standalone", "consolidated"})


class _PolicyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FinancialContextPolicy(_PolicyModel):
    provider_dataset_priority: tuple[UUID, ...]
    filing_scope_priority: tuple[str, ...]

    @field_validator("provider_dataset_priority")
    @classmethod
    def validate_provider_priority(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if not value:
            raise ValueError("provider_dataset_priority must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("provider_dataset_priority must not contain duplicates")
        return value

    @field_validator("filing_scope_priority")
    @classmethod
    def validate_scope_priority(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("filing_scope_priority must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("filing_scope_priority must not contain duplicates")
        if any(scope not in VALID_FILING_SCOPES for scope in value):
            raise ValueError("filing scopes must be standalone or consolidated")
        return value


class EligibilityPolicy(_PolicyModel):
    allowed_security_types: tuple[str, ...]
    allowed_security_statuses: tuple[str, ...]
    allowed_listing_statuses: tuple[str, ...]
    minimum_comparable_yoy_observations: int
    minimum_financial_core_coverage: Decimal
    minimum_average_daily_traded_value_inr: Decimal | None
    new_listing_exception_enabled: bool

    @field_validator(
        "allowed_security_types",
        "allowed_security_statuses",
        "allowed_listing_statuses",
    )
    @classmethod
    def validate_allowed_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("allowed values must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("allowed values must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_thresholds(self) -> EligibilityPolicy:
        if self.minimum_comparable_yoy_observations < 1:
            raise ValueError("minimum comparable YoY observations must be at least 1")
        if not Decimal("0") <= self.minimum_financial_core_coverage <= Decimal("1"):
            raise ValueError("minimum financial core coverage must be in [0, 1]")
        if (
            self.minimum_average_daily_traded_value_inr is not None
            and self.minimum_average_daily_traded_value_inr < 0
        ):
            raise ValueError("minimum liquidity must not be negative")
        return self


class ConfidencePolicy(_PolicyModel):
    completeness_weight: Decimal
    source_reliability_weight: Decimal
    recency_weight: Decimal
    history_weight: Decimal
    evidence_weight: Decimal
    stale_after_days: int
    maximum_history_observations: int

    @model_validator(mode="after")
    def validate_confidence(self) -> ConfidencePolicy:
        weights = (
            self.completeness_weight,
            self.source_reliability_weight,
            self.recency_weight,
            self.history_weight,
            self.evidence_weight,
        )
        if any(weight < 0 for weight in weights):
            raise ValueError("confidence weights must not be negative")
        if sum(weights, Decimal("0")) != Decimal("1"):
            raise ValueError("confidence weights must sum exactly to 1")
        if self.stale_after_days < 1:
            raise ValueError("stale_after_days must be at least 1")
        if self.maximum_history_observations < 1:
            raise ValueError("maximum_history_observations must be at least 1")
        return self


class ComponentWeights(_PolicyModel):
    financial_inflection: Decimal
    business_catalyst: Decimal
    business_quality: Decimal
    cash_flow_quality: Decimal
    balance_sheet: Decimal
    valuation: Decimal
    market_structure: Decimal
    low_market_attention: Decimal

    @model_validator(mode="after")
    def validate_weights(self) -> ComponentWeights:
        weights = tuple(self.__dict__.values())
        if any(weight < 0 for weight in weights):
            raise ValueError("component weights must not be negative")
        if sum(weights, Decimal("0")) != Decimal("1"):
            raise ValueError("component weights must sum exactly to 1")
        return self


class ScoreBreakpoint(_PolicyModel):
    raw_value: Decimal
    score: Decimal

    @field_validator("raw_value", "score", mode="before")
    @classmethod
    def reject_binary_float(cls, value: object) -> object:
        if isinstance(value, float):
            raise ValueError("scoring curve values must not use binary floats")
        return value

    @field_validator("score")
    @classmethod
    def validate_score(cls, value: Decimal) -> Decimal:
        if not Decimal("0") <= value <= Decimal("100"):
            raise ValueError("breakpoint score must be in [0, 100]")
        return value


class PiecewiseLinearScoringCurve(_PolicyModel):
    breakpoints: tuple[ScoreBreakpoint, ...]

    @model_validator(mode="after")
    def validate_breakpoints(self) -> PiecewiseLinearScoringCurve:
        if len(self.breakpoints) < 2:
            raise ValueError("a scoring curve requires at least two breakpoints")
        for previous, current in zip(self.breakpoints, self.breakpoints[1:], strict=False):
            if current.raw_value <= previous.raw_value:
                raise ValueError("breakpoint raw values must be strictly increasing")
            if current.score < previous.score:
                raise ValueError("breakpoint scores must be non-decreasing")
        return self


class FinancialInflectionSubfactorWeights(_PolicyModel):
    revenue_acceleration: Decimal
    pat_acceleration: Decimal
    margin_expansion: Decimal
    roce_improvement: Decimal
    growth_consistency: Decimal
    growth_persistence: Decimal

    @model_validator(mode="after")
    def validate_weights(self) -> FinancialInflectionSubfactorWeights:
        weights = tuple(self.__dict__.values())
        if any(weight < 0 for weight in weights):
            raise ValueError("financial-inflection subfactor weights must not be negative")
        if sum(weights, Decimal("0")) != Decimal("1"):
            raise ValueError("financial-inflection subfactor weights must sum exactly to 1")
        return self


class FinancialInflectionScoringPolicy(_PolicyModel):
    subfactor_weights: FinancialInflectionSubfactorWeights
    minimum_weight_coverage: Decimal
    margin_code: str
    growth_history_metric_code: str
    revenue_acceleration_curve: PiecewiseLinearScoringCurve
    pat_acceleration_curve: PiecewiseLinearScoringCurve
    margin_expansion_bps_curve: PiecewiseLinearScoringCurve
    roce_improvement_curve: PiecewiseLinearScoringCurve
    growth_consistency_curve: PiecewiseLinearScoringCurve
    growth_persistence_ratio_curve: PiecewiseLinearScoringCurve

    @model_validator(mode="after")
    def validate_scoring_policy(self) -> FinancialInflectionScoringPolicy:
        if not Decimal("0") < self.minimum_weight_coverage <= Decimal("1"):
            raise ValueError("minimum_weight_coverage must be in (0, 1]")
        if self.margin_code not in {"operating_margin", "ebitda_margin"}:
            raise ValueError("margin_code must be operating_margin or ebitda_margin")
        if not self.growth_history_metric_code:
            raise ValueError("growth_history_metric_code must not be empty")
        return self


class FinancialInflectionPolicy(_PolicyModel):
    consistency_window_size: int
    persistence_window_size: int
    persistence_threshold: Decimal
    scoring: FinancialInflectionScoringPolicy | None = None

    @model_validator(mode="after")
    def validate_windows(self) -> FinancialInflectionPolicy:
        if self.consistency_window_size < 1:
            raise ValueError("consistency_window_size must be at least 1")
        if self.persistence_window_size < 1:
            raise ValueError("persistence_window_size must be at least 1")
        return self


class BusinessQualitySubfactorWeights(_PolicyModel):
    roce_level: Decimal
    roe_level: Decimal
    margin_level: Decimal

    @model_validator(mode="after")
    def validate_weights(self) -> BusinessQualitySubfactorWeights:
        weights = tuple(self.__dict__.values())
        if any(weight < 0 for weight in weights):
            raise ValueError("business-quality subfactor weights must not be negative")
        if sum(weights, Decimal("0")) != Decimal("1"):
            raise ValueError("business-quality subfactor weights must sum exactly to 1")
        return self


class BusinessQualityScoringPolicy(_PolicyModel):
    subfactor_weights: BusinessQualitySubfactorWeights
    minimum_weight_coverage: Decimal
    margin_code: str
    roce_level_curve: PiecewiseLinearScoringCurve
    roe_level_curve: PiecewiseLinearScoringCurve
    margin_level_curve: PiecewiseLinearScoringCurve

    @model_validator(mode="after")
    def validate_scoring_policy(self) -> BusinessQualityScoringPolicy:
        if not Decimal("0") < self.minimum_weight_coverage <= Decimal("1"):
            raise ValueError("minimum_weight_coverage must be in (0, 1]")
        if self.margin_code not in {"operating_margin", "ebitda_margin"}:
            raise ValueError("margin_code must be operating_margin or ebitda_margin")
        return self


class CashFlowQualitySubfactorWeights(_PolicyModel):
    cfo_conversion: Decimal
    cfo_to_ebitda: Decimal
    receivable_days: Decimal
    trade_working_capital_burden: Decimal

    @model_validator(mode="after")
    def validate_weights(self) -> CashFlowQualitySubfactorWeights:
        weights = tuple(self.__dict__.values())
        if any(weight < 0 for weight in weights):
            raise ValueError("cash-flow-quality subfactor weights must not be negative")
        if sum(weights, Decimal("0")) != Decimal("1"):
            raise ValueError("cash-flow-quality subfactor weights must sum exactly to 1")
        return self


class CashFlowQualityScoringPolicy(_PolicyModel):
    subfactor_weights: CashFlowQualitySubfactorWeights
    minimum_weight_coverage: Decimal
    cfo_conversion_curve: PiecewiseLinearScoringCurve
    cfo_to_ebitda_curve: PiecewiseLinearScoringCurve
    receivable_days_signal_curve: PiecewiseLinearScoringCurve
    trade_working_capital_signal_curve: PiecewiseLinearScoringCurve

    @model_validator(mode="after")
    def validate_scoring_policy(self) -> CashFlowQualityScoringPolicy:
        if not Decimal("0") < self.minimum_weight_coverage <= Decimal("1"):
            raise ValueError("minimum_weight_coverage must be in (0, 1]")
        return self


class InflectionScoringPolicy(_PolicyModel):
    financial_context: FinancialContextPolicy
    eligibility: EligibilityPolicy
    confidence: ConfidencePolicy
    component_weights: ComponentWeights
    financial_inflection: FinancialInflectionPolicy
    business_quality: BusinessQualityScoringPolicy | None = None
    cash_flow_quality: CashFlowQualityScoringPolicy | None = None


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    return value


def policy_to_canonical_mapping(policy: InflectionScoringPolicy) -> dict[str, object]:
    policy_value = policy.model_dump(mode="python")
    financial_inflection = policy_value["financial_inflection"]
    assert isinstance(financial_inflection, dict)
    if financial_inflection.get("scoring") is None:
        financial_inflection.pop("scoring", None)
    if policy_value.get("business_quality") is None:
        policy_value.pop("business_quality", None)
    if policy_value.get("cash_flow_quality") is None:
        policy_value.pop("cash_flow_quality", None)
    value = _canonical_value(policy_value)
    assert isinstance(value, dict)
    return value


def canonical_policy_json(policy: InflectionScoringPolicy) -> str:
    """Serialize policy deterministically without binary-float conversion."""

    return json.dumps(
        policy_to_canonical_mapping(policy),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def scoring_policy_checksum(policy: InflectionScoringPolicy) -> str:
    return hashlib.sha256(canonical_policy_json(policy).encode("utf-8")).hexdigest()


def scoring_policy_from_mapping(value: Mapping[str, Any]) -> InflectionScoringPolicy:
    return InflectionScoringPolicy.model_validate(value)


@dataclass(frozen=True, slots=True)
class FinancialContextSelection:
    provider_dataset_id: UUID
    filing_scope: str
    provider_priority_index: int
    scope_priority_index: int
    fallback_used: bool
    selection_reason: str


class FinancialContextPolicyResolver:
    """Select one complete context using provider-first lexicographic priority."""

    def resolve(
        self,
        policy: FinancialContextPolicy,
        candidates: Iterable[tuple[UUID, str]],
    ) -> FinancialContextSelection | None:
        available = {
            (provider_id, scope)
            for provider_id, scope in candidates
            if scope in VALID_FILING_SCOPES
        }
        for provider_index, provider_id in enumerate(policy.provider_dataset_priority):
            for scope_index, scope in enumerate(policy.filing_scope_priority):
                if (provider_id, scope) not in available:
                    continue
                return FinancialContextSelection(
                    provider_dataset_id=provider_id,
                    filing_scope=scope,
                    provider_priority_index=provider_index,
                    scope_priority_index=scope_index,
                    fallback_used=provider_index != 0 or scope_index != 0,
                    selection_reason=self._selection_reason(provider_index, scope_index),
                )
        return None

    @staticmethod
    def _selection_reason(provider_index: int, scope_index: int) -> str:
        if provider_index == 0 and scope_index == 0:
            return "preferred_provider_preferred_scope"
        if provider_index == 0:
            return "preferred_provider_scope_fallback"
        if scope_index == 0:
            return "provider_fallback_preferred_scope"
        return "provider_and_scope_fallback"


@dataclass(frozen=True, slots=True)
class EligibilityInputs:
    security_type: str
    security_status: str
    listing_status: str
    comparable_yoy_observations: int
    financial_core_available: int
    financial_core_required: int
    average_daily_traded_value_inr: Decimal | None
    has_critical_data_quality_issue: bool
    is_new_listing: bool


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    financial_core_coverage: Decimal


class EligibilityEvaluator:
    """Evaluate hard eligibility without fetching data or assigning score points."""

    def evaluate(self, inputs: EligibilityInputs, policy: EligibilityPolicy) -> EligibilityResult:
        if inputs.comparable_yoy_observations < 0:
            raise ValueError("comparable_yoy_observations must not be negative")
        if inputs.financial_core_required <= 0:
            raise ValueError("financial_core_required must be positive")
        if not 0 <= inputs.financial_core_available <= inputs.financial_core_required:
            raise ValueError("financial_core_available must be between 0 and required")
        if (
            inputs.average_daily_traded_value_inr is not None
            and inputs.average_daily_traded_value_inr < 0
        ):
            raise ValueError("average_daily_traded_value_inr must not be negative")

        coverage = Decimal(inputs.financial_core_available) / Decimal(
            inputs.financial_core_required
        )
        reasons: list[str] = []
        warnings: list[str] = []
        if inputs.security_type not in policy.allowed_security_types:
            reasons.append("unsupported_security_type")
        if inputs.security_status not in policy.allowed_security_statuses:
            reasons.append("inactive_security")
        if inputs.listing_status not in policy.allowed_listing_statuses:
            reasons.append("inactive_listing")
        if inputs.has_critical_data_quality_issue:
            reasons.append("critical_data_quality_issue")
        if inputs.comparable_yoy_observations < policy.minimum_comparable_yoy_observations:
            if inputs.is_new_listing and policy.new_listing_exception_enabled:
                warnings.append("new_listing_history_exception")
            else:
                reasons.append("insufficient_comparable_history")
        if coverage < policy.minimum_financial_core_coverage:
            reasons.append("insufficient_financial_core_coverage")
        floor = policy.minimum_average_daily_traded_value_inr
        if floor is not None:
            if inputs.average_daily_traded_value_inr is None:
                reasons.append("missing_liquidity_evidence")
            elif inputs.average_daily_traded_value_inr < floor:
                reasons.append("below_liquidity_floor")
        return EligibilityResult(
            eligible=not reasons,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            financial_core_coverage=coverage,
        )


@dataclass(frozen=True, slots=True)
class ConfidenceInputs:
    required_feature_count: int
    available_feature_count: int
    source_reliability: Decimal
    freshest_required_evidence_at: datetime | None
    knowledge_cutoff: datetime
    comparable_history_observations: int
    evidence_confidence: Decimal | None


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    confidence: Decimal
    completeness: Decimal
    source_reliability: Decimal
    recency: Decimal
    history: Decimal
    evidence: Decimal
    warnings: tuple[str, ...]
    knowledge_cutoff: datetime
    freshest_required_evidence_at: datetime | None
    algorithm_version: str


class ConfidenceEvaluator:
    """Calculate exact policy-weighted confidence independently of eligibility."""

    def evaluate(self, inputs: ConfidenceInputs, policy: ConfidencePolicy) -> ConfidenceResult:
        if inputs.required_feature_count < 1:
            raise ValueError("required_feature_count must be at least 1")
        if not 0 <= inputs.available_feature_count <= inputs.required_feature_count:
            raise ValueError("available_feature_count must be between 0 and required")
        self._require_unit_interval(inputs.source_reliability, "source_reliability")
        if inputs.comparable_history_observations < 0:
            raise ValueError("comparable_history_observations must not be negative")
        if inputs.evidence_confidence is not None:
            self._require_unit_interval(inputs.evidence_confidence, "evidence_confidence")

        cutoff = self._aware_utc(inputs.knowledge_cutoff, "knowledge_cutoff")
        evidence_at = (
            self._aware_utc(
                inputs.freshest_required_evidence_at,
                "freshest_required_evidence_at",
            )
            if inputs.freshest_required_evidence_at is not None
            else None
        )
        warnings: list[str] = []
        if evidence_at is None:
            recency = Decimal("0")
            warnings.append("missing_recency_evidence")
        else:
            if evidence_at > cutoff:
                raise ValueError("freshest evidence must not be after knowledge cutoff")
            age = cutoff - evidence_at
            age_days = (
                Decimal(age.days)
                + Decimal(age.seconds) / Decimal("86400")
                + Decimal(age.microseconds) / Decimal("86400000000")
            )
            recency = max(
                Decimal("0"),
                Decimal("1") - age_days / Decimal(policy.stale_after_days),
            )
        evidence = inputs.evidence_confidence or Decimal("0")
        if inputs.evidence_confidence is None:
            warnings.append("missing_evidence_confidence")
        completeness = Decimal(inputs.available_feature_count) / Decimal(
            inputs.required_feature_count
        )
        history = min(
            Decimal("1"),
            Decimal(inputs.comparable_history_observations)
            / Decimal(policy.maximum_history_observations),
        )
        confidence = (
            completeness * policy.completeness_weight
            + inputs.source_reliability * policy.source_reliability_weight
            + recency * policy.recency_weight
            + history * policy.history_weight
            + evidence * policy.evidence_weight
        )
        return ConfidenceResult(
            confidence=confidence,
            completeness=completeness,
            source_reliability=inputs.source_reliability,
            recency=recency,
            history=history,
            evidence=evidence,
            warnings=tuple(warnings),
            knowledge_cutoff=cutoff,
            freshest_required_evidence_at=evidence_at,
            algorithm_version=CONFIDENCE_VERSION,
        )

    @staticmethod
    def _require_unit_interval(value: Decimal, field_name: str) -> None:
        if not Decimal("0") <= value <= Decimal("1"):
            raise ValueError(f"{field_name} must be in [0, 1]")

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)
