"""Pure Phase 4B financial-inflection component scoring primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from inflector_core.scoring_policy import (
    FinancialContextSelection,
    InflectionScoringPolicy,
    PiecewiseLinearScoringCurve,
)

if TYPE_CHECKING:
    from inflector_data.capital_features import ReturnOnCapitalEmployedValue
    from inflector_data.financial_features import (
        GrowthAccelerationValue,
        MarginExpansionValue,
    )
    from inflector_data.growth_history_features import (
        GrowthConsistencyValue,
        GrowthPersistenceValue,
    )

FINANCIAL_INFLECTION_COMPONENT_VERSION = "financial_inflection_component_v1"
PIECEWISE_LINEAR_CURVE_VERSION = "piecewise_linear_v1"

SUBFACTOR_ORDER = (
    "revenue_acceleration",
    "pat_acceleration",
    "margin_expansion",
    "roce_improvement",
    "growth_consistency",
    "growth_persistence",
)


def score_piecewise_linear(
    raw_value: Decimal,
    curve: PiecewiseLinearScoringCurve,
) -> Decimal:
    """Normalize a Decimal value using exact interpolation and endpoint clamping."""

    if not isinstance(raw_value, Decimal):
        raise TypeError("raw_value must be Decimal")
    points = curve.breakpoints
    if raw_value <= points[0].raw_value:
        return points[0].score
    if raw_value >= points[-1].raw_value:
        return points[-1].score
    for lower, upper in zip(points, points[1:], strict=False):
        if raw_value <= upper.raw_value:
            return lower.score + (
                (raw_value - lower.raw_value)
                / (upper.raw_value - lower.raw_value)
                * (upper.score - lower.score)
            )
    raise AssertionError("validated curve did not contain raw_value")


@dataclass(frozen=True, slots=True)
class FinancialInflectionEvidence:
    company_id: UUID
    context: FinancialContextSelection
    fiscal_year: int
    fiscal_quarter: int
    as_of: datetime
    revenue_acceleration: GrowthAccelerationValue | None = None
    pat_acceleration: GrowthAccelerationValue | None = None
    margin_expansion: MarginExpansionValue | None = None
    current_roce: ReturnOnCapitalEmployedValue | None = None
    prior_year_roce: ReturnOnCapitalEmployedValue | None = None
    growth_consistency: GrowthConsistencyValue | None = None
    growth_persistence: GrowthPersistenceValue | None = None


@dataclass(frozen=True, slots=True)
class RoceImprovementEvidence:
    current_roce: ReturnOnCapitalEmployedValue
    prior_year_roce: ReturnOnCapitalEmployedValue


@dataclass(frozen=True, slots=True)
class FinancialInflectionSubfactorScore:
    code: str
    raw_value: Decimal
    raw_unit: str
    normalized_score: Decimal
    configured_weight: Decimal
    effective_weight: Decimal
    contribution: Decimal
    input_available_at: datetime
    evidence_type: str
    evidence: object
    curve_algorithm_version: str


@dataclass(frozen=True, slots=True)
class FinancialInflectionComponentScore:
    company_id: UUID
    provider_dataset_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[FinancialInflectionSubfactorScore, ...]
    missing_subfactors: tuple[str, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class _RawSubfactor:
    code: str
    value: Decimal
    unit: str
    weight: Decimal
    available_at: datetime
    evidence_type: str
    evidence: object
    curve: PiecewiseLinearScoringCurve


class FinancialInflectionComponentScorer:
    """Transform one coherent Phase 3 evidence bundle into a component score."""

    def score(
        self,
        *,
        evidence: FinancialInflectionEvidence,
        policy: InflectionScoringPolicy,
    ) -> FinancialInflectionComponentScore:
        cutoff = self._aware_utc(evidence.as_of, "as_of")
        scoring = policy.financial_inflection.scoring
        if scoring is None:
            raise ValueError("financial-inflection scoring policy is not configured")

        self._validate_evidence(evidence, policy, cutoff)
        raw_by_code = self._extract_raw_subfactors(evidence, policy)
        weights = scoring.subfactor_weights
        missing = tuple(
            code
            for code in SUBFACTOR_ORDER
            if code not in raw_by_code and getattr(weights, code) > 0
        )
        available_weight = sum(
            (raw_by_code[code].weight for code in SUBFACTOR_ORDER if code in raw_by_code),
            Decimal("0"),
        )
        sufficient = available_weight >= scoring.minimum_weight_coverage

        subfactors = tuple(
            self._score_subfactor(raw_by_code[code], available_weight, sufficient)
            for code in SUBFACTOR_ORDER
            if code in raw_by_code
        )
        component_score = (
            sum((item.contribution for item in subfactors), Decimal("0"))
            if sufficient
            else None
        )
        if component_score is not None and not Decimal("0") <= component_score <= Decimal(
            "100"
        ):
            raise AssertionError("component score escaped the validated 0-100 range")
        available_at = max(
            (item.input_available_at for item in subfactors),
            default=None,
        )
        return FinancialInflectionComponentScore(
            company_id=evidence.company_id,
            provider_dataset_id=evidence.context.provider_dataset_id,
            filing_scope=evidence.context.filing_scope,
            ending_fiscal_year=evidence.fiscal_year,
            ending_fiscal_quarter=evidence.fiscal_quarter,
            score=component_score,
            unit="score_0_100",
            weight_coverage=available_weight,
            available_weight=available_weight,
            subfactors=subfactors,
            missing_subfactors=missing,
            warnings=() if sufficient else ("insufficient_subfactor_coverage",),
            as_of=cutoff,
            available_at=available_at,
            algorithm_version=FINANCIAL_INFLECTION_COMPONENT_VERSION,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> FinancialInflectionSubfactorScore:
        normalized = score_piecewise_linear(raw.value, raw.curve)
        effective = raw.weight / available_weight if sufficient else Decimal("0")
        return FinancialInflectionSubfactorScore(
            code=raw.code,
            raw_value=raw.value,
            raw_unit=raw.unit,
            normalized_score=normalized,
            configured_weight=raw.weight,
            effective_weight=effective,
            contribution=normalized * effective,
            input_available_at=raw.available_at,
            evidence_type=raw.evidence_type,
            evidence=raw.evidence,
            curve_algorithm_version=PIECEWISE_LINEAR_CURVE_VERSION,
        )

    def _validate_evidence(
        self,
        bundle: FinancialInflectionEvidence,
        policy: InflectionScoringPolicy,
        cutoff: datetime,
    ) -> None:
        current_items = (
            bundle.revenue_acceleration,
            bundle.pat_acceleration,
            bundle.margin_expansion,
            bundle.current_roce,
            bundle.growth_consistency,
            bundle.growth_persistence,
        )
        for item in current_items:
            if item is not None:
                self._validate_common(item, bundle, cutoff)
                if (item.ending_fiscal_year, item.ending_fiscal_quarter) != (
                    bundle.fiscal_year,
                    bundle.fiscal_quarter,
                ):
                    raise ValueError("evidence endpoint does not match requested endpoint")
        if bundle.prior_year_roce is not None:
            self._validate_common(bundle.prior_year_roce, bundle, cutoff)
            if (
                bundle.prior_year_roce.ending_fiscal_year,
                bundle.prior_year_roce.ending_fiscal_quarter,
            ) != (bundle.fiscal_year - 1, bundle.fiscal_quarter):
                raise ValueError("prior ROCE must be the prior-year same-quarter endpoint")

        scoring = policy.financial_inflection.scoring
        assert scoring is not None
        if (
            bundle.revenue_acceleration is not None
            and bundle.revenue_acceleration.metric_code != "revenue"
        ):
            raise ValueError("revenue acceleration evidence must use revenue")
        if bundle.pat_acceleration is not None and bundle.pat_acceleration.metric_code != "pat":
            raise ValueError("PAT acceleration evidence must use pat")
        if (
            bundle.margin_expansion is not None
            and bundle.margin_expansion.margin_code != scoring.margin_code
        ):
            raise ValueError("margin expansion does not match configured margin_code")
        if bundle.growth_consistency is not None:
            if bundle.growth_consistency.metric_code != scoring.growth_history_metric_code:
                raise ValueError("growth consistency does not match configured metric")
            if (
                bundle.growth_consistency.window_size
                != policy.financial_inflection.consistency_window_size
            ):
                raise ValueError("growth consistency window does not match policy")
        if bundle.growth_persistence is not None:
            if bundle.growth_persistence.metric_code != scoring.growth_history_metric_code:
                raise ValueError("growth persistence does not match configured metric")
            if (
                bundle.growth_persistence.window_size
                != policy.financial_inflection.persistence_window_size
            ):
                raise ValueError("growth persistence window does not match policy")
            if (
                bundle.growth_persistence.threshold
                != policy.financial_inflection.persistence_threshold
            ):
                raise ValueError("growth persistence threshold does not match policy")

    def _validate_common(
        self,
        item: object,
        bundle: FinancialInflectionEvidence,
        cutoff: datetime,
    ) -> None:
        if getattr(item, "provider_dataset_id") != bundle.context.provider_dataset_id:
            raise ValueError("evidence provider does not match selected context")
        if getattr(item, "filing_scope") != bundle.context.filing_scope:
            raise ValueError("evidence filing scope does not match selected context")
        if getattr(item, "company_id") != bundle.company_id:
            raise ValueError("evidence company does not match scoring company")
        if self._aware_utc(getattr(item, "as_of"), "evidence as_of") != cutoff:
            raise ValueError("evidence as_of does not match scoring cutoff")

    def _extract_raw_subfactors(
        self,
        bundle: FinancialInflectionEvidence,
        policy: InflectionScoringPolicy,
    ) -> dict[str, _RawSubfactor]:
        scoring = policy.financial_inflection.scoring
        assert scoring is not None
        weights = scoring.subfactor_weights
        raw: dict[str, _RawSubfactor] = {}

        def add(
            code: str,
            value: Decimal,
            unit: str,
            available_at: datetime,
            source: object,
            curve: PiecewiseLinearScoringCurve,
        ) -> None:
            raw[code] = _RawSubfactor(
                code=code,
                value=value,
                unit=unit,
                weight=getattr(weights, code),
                available_at=self._aware_utc(available_at, f"{code} available_at"),
                evidence_type=type(source).__name__,
                evidence=source,
                curve=curve,
            )

        if bundle.revenue_acceleration is not None:
            add(
                "revenue_acceleration",
                bundle.revenue_acceleration.acceleration,
                "ratio",
                bundle.revenue_acceleration.available_at,
                bundle.revenue_acceleration,
                scoring.revenue_acceleration_curve,
            )
        if bundle.pat_acceleration is not None:
            add(
                "pat_acceleration",
                bundle.pat_acceleration.acceleration,
                "ratio",
                bundle.pat_acceleration.available_at,
                bundle.pat_acceleration,
                scoring.pat_acceleration_curve,
            )
        if bundle.margin_expansion is not None:
            add(
                "margin_expansion",
                bundle.margin_expansion.basis_points,
                "basis_points",
                bundle.margin_expansion.available_at,
                bundle.margin_expansion,
                scoring.margin_expansion_bps_curve,
            )
        if (
            bundle.current_roce is not None
            and bundle.prior_year_roce is not None
            and bundle.current_roce.value is not None
            and bundle.prior_year_roce.value is not None
        ):
            roce_evidence = RoceImprovementEvidence(
                current_roce=bundle.current_roce,
                prior_year_roce=bundle.prior_year_roce,
            )
            add(
                "roce_improvement",
                bundle.current_roce.value - bundle.prior_year_roce.value,
                "ratio_delta",
                max(bundle.current_roce.available_at, bundle.prior_year_roce.available_at),
                roce_evidence,
                scoring.roce_improvement_curve,
            )
        if bundle.growth_consistency is not None:
            add(
                "growth_consistency",
                bundle.growth_consistency.positive_share,
                "ratio",
                bundle.growth_consistency.available_at,
                bundle.growth_consistency,
                scoring.growth_consistency_curve,
            )
        if bundle.growth_persistence is not None:
            add(
                "growth_persistence",
                Decimal(bundle.growth_persistence.streak_count)
                / Decimal(bundle.growth_persistence.window_size),
                "ratio",
                bundle.growth_persistence.available_at,
                bundle.growth_persistence,
                scoring.growth_persistence_ratio_curve,
            )
        return raw

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)


def score_financial_inflection_component(
    *,
    evidence: FinancialInflectionEvidence,
    policy: InflectionScoringPolicy,
) -> FinancialInflectionComponentScore:
    return FinancialInflectionComponentScorer().score(evidence=evidence, policy=policy)
