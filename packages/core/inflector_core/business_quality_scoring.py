"""Pure Phase 4D-A Business Quality component scoring primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from inflector_core.component_scoring import (
    PIECEWISE_LINEAR_CURVE_VERSION,
    score_piecewise_linear,
)
from inflector_core.scoring_policy import (
    BusinessQualityScoringPolicy,
    FinancialContextSelection,
    InflectionScoringPolicy,
    PiecewiseLinearScoringCurve,
)

if TYPE_CHECKING:
    from inflector_data.capital_features import (
        ReturnOnCapitalEmployedValue,
        ReturnOnEquityValue,
    )
    from inflector_data.financial_features import QuarterMarginValue

BUSINESS_QUALITY_COMPONENT_VERSION = "business_quality_component_v1"

BUSINESS_QUALITY_SUBFACTOR_ORDER = (
    "roce_level",
    "roe_level",
    "margin_level",
)


@dataclass(frozen=True, slots=True)
class BusinessQualityEvidence:
    company_id: UUID
    context: FinancialContextSelection
    fiscal_year: int
    fiscal_quarter: int
    as_of: datetime
    roce: ReturnOnCapitalEmployedValue | None = None
    roe: ReturnOnEquityValue | None = None
    margin: QuarterMarginValue | None = None


@dataclass(frozen=True, slots=True)
class BusinessQualitySubfactorScore:
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
class BusinessQualityComponentScore:
    company_id: UUID
    provider_dataset_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[BusinessQualitySubfactorScore, ...]
    missing_subfactors: tuple[str, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class _RawSubfactor:
    code: str
    value: Decimal
    weight: Decimal
    available_at: datetime
    evidence: object
    curve: PiecewiseLinearScoringCurve


class BusinessQualityComponentScorer:
    """Score current ROCE, ROE, and configured margin levels."""

    def score(
        self,
        *,
        evidence: BusinessQualityEvidence,
        policy: InflectionScoringPolicy,
    ) -> BusinessQualityComponentScore:
        cutoff = self._aware_utc(evidence.as_of, "as_of")
        scoring = policy.business_quality
        if scoring is None:
            raise ValueError("business-quality scoring policy is not configured")

        self._validate_evidence(evidence, scoring, cutoff)
        raw_by_code = self._extract_raw_subfactors(evidence, scoring)
        weights = scoring.subfactor_weights
        missing = tuple(
            code
            for code in BUSINESS_QUALITY_SUBFACTOR_ORDER
            if getattr(weights, code) > 0 and code not in raw_by_code
        )
        available_weight = sum(
            (
                raw_by_code[code].weight
                for code in BUSINESS_QUALITY_SUBFACTOR_ORDER
                if code in raw_by_code
            ),
            Decimal("0"),
        )
        sufficient = available_weight >= scoring.minimum_weight_coverage
        subfactors = tuple(
            self._score_subfactor(raw_by_code[code], available_weight, sufficient)
            for code in BUSINESS_QUALITY_SUBFACTOR_ORDER
            if code in raw_by_code
        )
        component_score = (
            sum((item.contribution for item in subfactors), Decimal("0")) if sufficient else None
        )
        if component_score is not None and not Decimal("0") <= component_score <= Decimal("100"):
            raise AssertionError("component score escaped the validated 0-100 range")

        return BusinessQualityComponentScore(
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
            available_at=max(
                (item.input_available_at for item in subfactors),
                default=None,
            ),
            algorithm_version=BUSINESS_QUALITY_COMPONENT_VERSION,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> BusinessQualitySubfactorScore:
        normalized = score_piecewise_linear(raw.value, raw.curve)
        effective = raw.weight / available_weight if sufficient else Decimal("0")
        return BusinessQualitySubfactorScore(
            code=raw.code,
            raw_value=raw.value,
            raw_unit="ratio",
            normalized_score=normalized,
            configured_weight=raw.weight,
            effective_weight=effective,
            contribution=normalized * effective,
            input_available_at=raw.available_at,
            evidence_type=type(raw.evidence).__name__,
            evidence=raw.evidence,
            curve_algorithm_version=PIECEWISE_LINEAR_CURVE_VERSION,
        )

    def _validate_evidence(
        self,
        bundle: BusinessQualityEvidence,
        scoring: BusinessQualityScoringPolicy,
        cutoff: datetime,
    ) -> None:
        for item in (bundle.roce, bundle.roe, bundle.margin):
            if item is None:
                continue
            if item.provider_dataset_id != bundle.context.provider_dataset_id:
                raise ValueError("evidence provider does not match selected context")
            if item.filing_scope != bundle.context.filing_scope:
                raise ValueError("evidence filing scope does not match selected context")
            if item.company_id != bundle.company_id:
                raise ValueError("evidence company does not match scoring company")
            if self._aware_utc(item.as_of, "evidence as_of") != cutoff:
                raise ValueError("evidence as_of does not match scoring cutoff")
            if (item.ending_fiscal_year, item.ending_fiscal_quarter) != (
                bundle.fiscal_year,
                bundle.fiscal_quarter,
            ):
                raise ValueError("evidence endpoint does not match requested endpoint")
        if bundle.margin is not None and bundle.margin.margin_code != scoring.margin_code:
            raise ValueError("margin evidence does not match configured margin_code")

    def _extract_raw_subfactors(
        self,
        bundle: BusinessQualityEvidence,
        scoring: BusinessQualityScoringPolicy,
    ) -> dict[str, _RawSubfactor]:
        weights = scoring.subfactor_weights
        raw: dict[str, _RawSubfactor] = {}

        def add(
            code: str,
            value: Decimal | None,
            available_at: datetime,
            source: object,
            curve: PiecewiseLinearScoringCurve,
        ) -> None:
            weight = getattr(weights, code)
            if weight == 0 or value is None:
                return
            raw[code] = _RawSubfactor(
                code=code,
                value=value,
                weight=weight,
                available_at=self._aware_utc(available_at, f"{code} available_at"),
                evidence=source,
                curve=curve,
            )

        if bundle.roce is not None:
            add(
                "roce_level",
                bundle.roce.value,
                bundle.roce.available_at,
                bundle.roce,
                scoring.roce_level_curve,
            )
        if bundle.roe is not None:
            add(
                "roe_level",
                bundle.roe.value,
                bundle.roe.available_at,
                bundle.roe,
                scoring.roe_level_curve,
            )
        if bundle.margin is not None:
            add(
                "margin_level",
                bundle.margin.value,
                bundle.margin.available_at,
                bundle.margin,
                scoring.margin_level_curve,
            )
        return raw

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)


def score_business_quality_component(
    *,
    evidence: BusinessQualityEvidence,
    policy: InflectionScoringPolicy,
) -> BusinessQualityComponentScore:
    return BusinessQualityComponentScorer().score(evidence=evidence, policy=policy)
