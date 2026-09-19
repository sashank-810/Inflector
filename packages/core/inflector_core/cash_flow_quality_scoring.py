"""Pure Phase 4D-B Cash-Flow Quality component scoring primitives."""

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
    CashFlowQualityScoringPolicy,
    FinancialContextSelection,
    InflectionScoringPolicy,
    PiecewiseLinearScoringCurve,
)

if TYPE_CHECKING:
    from inflector_data.cash_flow_features import (
        CashFlowConversionValue,
        CashFlowToEbitdaValue,
        ReceivableDaysValue,
        TradeWorkingCapitalChangeValue,
    )

CASH_FLOW_QUALITY_COMPONENT_VERSION = "cash_flow_quality_component_v1"

CASH_FLOW_QUALITY_SUBFACTOR_ORDER = (
    "cfo_conversion",
    "cfo_to_ebitda",
    "receivable_days",
    "trade_working_capital_burden",
)


@dataclass(frozen=True, slots=True)
class CashFlowQualityEvidence:
    company_id: UUID
    context: FinancialContextSelection
    fiscal_year: int
    fiscal_quarter: int
    as_of: datetime
    cfo_conversion: CashFlowConversionValue | None = None
    cfo_to_ebitda: CashFlowToEbitdaValue | None = None
    receivable_days: ReceivableDaysValue | None = None
    trade_working_capital_change: TradeWorkingCapitalChangeValue | None = None


@dataclass(frozen=True, slots=True)
class CashFlowQualitySubfactorScore:
    code: str
    raw_value: Decimal
    raw_unit: str
    normalized_raw_value: Decimal | None
    normalized_raw_unit: str | None
    scoring_value: Decimal
    scoring_unit: str
    transform_code: str
    normalized_score: Decimal
    configured_weight: Decimal
    effective_weight: Decimal
    contribution: Decimal
    input_available_at: datetime
    evidence_type: str
    evidence: object
    curve_algorithm_version: str


@dataclass(frozen=True, slots=True)
class CashFlowQualityComponentScore:
    company_id: UUID
    provider_dataset_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[CashFlowQualitySubfactorScore, ...]
    missing_subfactors: tuple[str, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class _RawSubfactor:
    code: str
    raw_value: Decimal
    raw_unit: str
    normalized_raw_value: Decimal | None
    normalized_raw_unit: str | None
    scoring_value: Decimal
    scoring_unit: str
    transform_code: str
    weight: Decimal
    available_at: datetime
    evidence: object
    curve: PiecewiseLinearScoringCurve


class CashFlowQualityComponentScorer:
    """Score coherent cash-flow evidence through explicit, auditable transforms."""

    def score(
        self,
        *,
        evidence: CashFlowQualityEvidence,
        policy: InflectionScoringPolicy,
    ) -> CashFlowQualityComponentScore:
        cutoff = self._aware_utc(evidence.as_of, "as_of")
        scoring = policy.cash_flow_quality
        if scoring is None:
            raise ValueError("cash-flow-quality scoring policy is not configured")

        self._validate_evidence(evidence, cutoff)
        raw_by_code = self._extract_raw_subfactors(evidence, scoring)
        weights = scoring.subfactor_weights
        missing = tuple(
            code
            for code in CASH_FLOW_QUALITY_SUBFACTOR_ORDER
            if getattr(weights, code) > 0 and code not in raw_by_code
        )
        available_weight = sum(
            (
                raw_by_code[code].weight
                for code in CASH_FLOW_QUALITY_SUBFACTOR_ORDER
                if code in raw_by_code
            ),
            Decimal("0"),
        )
        sufficient = available_weight >= scoring.minimum_weight_coverage
        subfactors = tuple(
            self._score_subfactor(raw_by_code[code], available_weight, sufficient)
            for code in CASH_FLOW_QUALITY_SUBFACTOR_ORDER
            if code in raw_by_code
        )
        component_score = (
            sum((item.contribution for item in subfactors), Decimal("0")) if sufficient else None
        )
        if component_score is not None and not Decimal("0") <= component_score <= Decimal("100"):
            raise AssertionError("component score escaped the validated 0-100 range")

        return CashFlowQualityComponentScore(
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
            algorithm_version=CASH_FLOW_QUALITY_COMPONENT_VERSION,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> CashFlowQualitySubfactorScore:
        normalized = score_piecewise_linear(raw.scoring_value, raw.curve)
        effective = raw.weight / available_weight if sufficient else Decimal("0")
        return CashFlowQualitySubfactorScore(
            code=raw.code,
            raw_value=raw.raw_value,
            raw_unit=raw.raw_unit,
            normalized_raw_value=raw.normalized_raw_value,
            normalized_raw_unit=raw.normalized_raw_unit,
            scoring_value=raw.scoring_value,
            scoring_unit=raw.scoring_unit,
            transform_code=raw.transform_code,
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
        bundle: CashFlowQualityEvidence,
        cutoff: datetime,
    ) -> None:
        items = (
            bundle.cfo_conversion,
            bundle.cfo_to_ebitda,
            bundle.receivable_days,
            bundle.trade_working_capital_change,
        )
        for item in items:
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
        if bundle.receivable_days is not None:
            value = bundle.receivable_days.value
            if value is not None and value < 0:
                raise ValueError("receivable days must not be negative")

    def _extract_raw_subfactors(
        self,
        bundle: CashFlowQualityEvidence,
        scoring: CashFlowQualityScoringPolicy,
    ) -> dict[str, _RawSubfactor]:
        weights = scoring.subfactor_weights
        raw: dict[str, _RawSubfactor] = {}

        def add(
            *,
            code: str,
            raw_value: Decimal | None,
            raw_unit: str,
            normalized_raw_value: Decimal | None,
            normalized_raw_unit: str | None,
            scoring_value: Decimal | None,
            scoring_unit: str,
            transform_code: str,
            available_at: datetime,
            source: object,
            curve: PiecewiseLinearScoringCurve,
        ) -> None:
            weight = getattr(weights, code)
            if weight == 0 or raw_value is None or scoring_value is None:
                return
            raw[code] = _RawSubfactor(
                code=code,
                raw_value=raw_value,
                raw_unit=raw_unit,
                normalized_raw_value=normalized_raw_value,
                normalized_raw_unit=normalized_raw_unit,
                scoring_value=scoring_value,
                scoring_unit=scoring_unit,
                transform_code=transform_code,
                weight=weight,
                available_at=self._aware_utc(available_at, f"{code} available_at"),
                evidence=source,
                curve=curve,
            )

        if bundle.cfo_conversion is not None:
            value = bundle.cfo_conversion.value
            add(
                code="cfo_conversion",
                raw_value=value,
                raw_unit="ratio",
                normalized_raw_value=None,
                normalized_raw_unit=None,
                scoring_value=value,
                scoring_unit="ratio",
                transform_code="identity",
                available_at=bundle.cfo_conversion.available_at,
                source=bundle.cfo_conversion,
                curve=scoring.cfo_conversion_curve,
            )
        if bundle.cfo_to_ebitda is not None:
            value = bundle.cfo_to_ebitda.value
            add(
                code="cfo_to_ebitda",
                raw_value=value,
                raw_unit="ratio",
                normalized_raw_value=None,
                normalized_raw_unit=None,
                scoring_value=value,
                scoring_unit="ratio",
                transform_code="identity",
                available_at=bundle.cfo_to_ebitda.available_at,
                source=bundle.cfo_to_ebitda,
                curve=scoring.cfo_to_ebitda_curve,
            )
        if bundle.receivable_days is not None:
            value = bundle.receivable_days.value
            add(
                code="receivable_days",
                raw_value=value,
                raw_unit="days",
                normalized_raw_value=None,
                normalized_raw_unit=None,
                scoring_value=-value if value is not None else None,
                scoring_unit="negative_days",
                transform_code="negate_receivable_days",
                available_at=bundle.receivable_days.available_at,
                source=bundle.receivable_days,
                curve=scoring.receivable_days_signal_curve,
            )
        if bundle.trade_working_capital_change is not None:
            item = bundle.trade_working_capital_change
            revenue = item.ttm_revenue.value
            ratio = item.value / revenue if item.value is not None and revenue > 0 else None
            add(
                code="trade_working_capital_burden",
                raw_value=item.value,
                raw_unit="INR",
                normalized_raw_value=ratio,
                normalized_raw_unit="ratio" if ratio is not None else None,
                scoring_value=-ratio if ratio is not None else None,
                scoring_unit="ratio",
                transform_code="negate_twc_change_over_ttm_revenue",
                available_at=item.available_at,
                source=item,
                curve=scoring.trade_working_capital_signal_curve,
            )
        return raw

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)


def score_cash_flow_quality_component(
    *,
    evidence: CashFlowQualityEvidence,
    policy: InflectionScoringPolicy,
) -> CashFlowQualityComponentScore:
    return CashFlowQualityComponentScorer().score(evidence=evidence, policy=policy)
