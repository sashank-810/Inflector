"""Pure Phase 4D-E Valuation component scoring primitives."""

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
    InflectionScoringPolicy,
    PiecewiseLinearScoringCurve,
)

if TYPE_CHECKING:
    from inflector_data.valuation_features import (
        InstantFinancialSnapshot,
        PointInTimeMarketBar,
        TrailingTwelveMonthValue,
        ValuationFeatureBundle,
        ValuationMultipleValue,
    )

VALUATION_COMPONENT_VERSION = "valuation_component_v1"

VALUATION_SUBFACTOR_ORDER = (
    "market_cap_to_ttm_pat",
    "market_cap_to_total_equity",
    "market_cap_to_ttm_revenue",
    "simplified_ev_to_ttm_ebitda",
    "simplified_ev_to_ttm_revenue",
)

_FEATURE_VERSIONS = {
    "market_cap_to_ttm_pat": "market_cap_to_ttm_pat_v1",
    "market_cap_to_total_equity": "market_cap_to_total_equity_v1",
    "market_cap_to_ttm_revenue": "market_cap_to_ttm_revenue_v1",
    "simplified_ev_to_ttm_ebitda": "simplified_ev_to_ttm_ebitda_v1",
    "simplified_ev_to_ttm_revenue": "simplified_ev_to_ttm_revenue_v1",
}

_TRANSFORM_CODES = {
    "market_cap_to_ttm_pat": "negate_market_cap_to_ttm_pat",
    "market_cap_to_total_equity": "negate_market_cap_to_total_equity",
    "market_cap_to_ttm_revenue": "negate_market_cap_to_ttm_revenue",
    "simplified_ev_to_ttm_ebitda": "negate_simplified_ev_to_ttm_ebitda",
    "simplified_ev_to_ttm_revenue": "negate_simplified_ev_to_ttm_revenue",
}


@dataclass(frozen=True, slots=True)
class ValuationUnavailableSubfactor:
    code: str
    configured_weight: Decimal
    warnings: tuple[str, ...]
    evidence_type: str
    evidence: object


@dataclass(frozen=True, slots=True)
class ValuationSubfactorScore:
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
class ValuationComponentScore:
    company_id: UUID
    security_id: UUID
    market_provider_dataset_id: UUID
    financial_provider_dataset_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[ValuationSubfactorScore, ...]
    unavailable_subfactors: tuple[ValuationUnavailableSubfactor, ...]
    missing_subfactors: tuple[str, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class _RawSubfactor:
    code: str
    raw_value: Decimal
    scoring_value: Decimal
    weight: Decimal
    available_at: datetime
    evidence: object
    transform_code: str
    curve: PiecewiseLinearScoringCurve


class ValuationComponentScorer:
    """Score approved valuation ratios without recomputing financial features."""

    def score(
        self,
        *,
        evidence: ValuationFeatureBundle,
        policy: InflectionScoringPolicy,
    ) -> ValuationComponentScore:
        cutoff = self._aware_utc(evidence.as_of, "bundle as_of")
        scoring = policy.valuation
        if scoring is None:
            raise ValueError("valuation scoring policy is not configured")
        self._validate_bundle(evidence, cutoff)

        weights = scoring.subfactor_weights
        raw: dict[str, _RawSubfactor] = {}
        unavailable: list[ValuationUnavailableSubfactor] = []
        for code in VALUATION_SUBFACTOR_ORDER:
            weight = getattr(weights, code)
            if weight == 0:
                continue
            feature = getattr(evidence, code)
            curve = getattr(scoring, f"{code}_signal_curve")
            if feature.value is None:
                unavailable.append(
                    ValuationUnavailableSubfactor(
                        code=code,
                        configured_weight=weight,
                        warnings=feature.warnings,
                        evidence_type=type(feature).__name__,
                        evidence=feature,
                    )
                )
                continue
            assert feature.available_at is not None
            raw[code] = _RawSubfactor(
                code=code,
                raw_value=feature.value,
                scoring_value=-feature.value,
                weight=weight,
                available_at=self._aware_utc(feature.available_at, f"{code} available_at"),
                evidence=feature,
                transform_code=_TRANSFORM_CODES[code],
                curve=curve,
            )

        available_weight = sum((item.weight for item in raw.values()), Decimal("0"))
        sufficient = available_weight >= scoring.minimum_weight_coverage
        subfactors = tuple(
            self._score_subfactor(raw[code], available_weight, sufficient)
            for code in VALUATION_SUBFACTOR_ORDER
            if code in raw
        )
        score = (
            sum((item.contribution for item in subfactors), Decimal("0")) if sufficient else None
        )
        if score is not None and not Decimal("0") <= score <= Decimal("100"):
            raise AssertionError("component score escaped the validated 0-100 range")
        missing = tuple(item.code for item in unavailable)
        return ValuationComponentScore(
            company_id=evidence.company_id,
            security_id=evidence.security_id,
            market_provider_dataset_id=evidence.market_provider_dataset_id,
            financial_provider_dataset_id=evidence.financial_provider_dataset_id,
            filing_scope=evidence.filing_scope,
            ending_fiscal_year=evidence.ending_fiscal_year,
            ending_fiscal_quarter=evidence.ending_fiscal_quarter,
            score=score,
            unit="score_0_100",
            weight_coverage=available_weight,
            available_weight=available_weight,
            subfactors=subfactors,
            unavailable_subfactors=tuple(unavailable),
            missing_subfactors=missing,
            warnings=() if sufficient else ("insufficient_subfactor_coverage",),
            as_of=cutoff,
            available_at=max(
                (item.input_available_at for item in subfactors),
                default=None,
            ),
            algorithm_version=VALUATION_COMPONENT_VERSION,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> ValuationSubfactorScore:
        normalized_score = score_piecewise_linear(raw.scoring_value, raw.curve)
        effective_weight = raw.weight / available_weight if sufficient else Decimal("0")
        return ValuationSubfactorScore(
            code=raw.code,
            raw_value=raw.raw_value,
            raw_unit="ratio",
            normalized_raw_value=None,
            normalized_raw_unit=None,
            scoring_value=raw.scoring_value,
            scoring_unit="ratio",
            transform_code=raw.transform_code,
            normalized_score=normalized_score,
            configured_weight=raw.weight,
            effective_weight=effective_weight,
            contribution=normalized_score * effective_weight,
            input_available_at=raw.available_at,
            evidence_type=type(raw.evidence).__name__,
            evidence=raw.evidence,
            curve_algorithm_version=PIECEWISE_LINEAR_CURVE_VERSION,
        )

    def _validate_bundle(self, bundle: ValuationFeatureBundle, cutoff: datetime) -> None:
        if bundle.algorithm_version != "valuation_feature_bundle_v1":
            raise ValueError("unsupported valuation feature bundle version")
        if bundle.ending_fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("ending_fiscal_quarter must be between 1 and 4")
        if not bundle.filing_scope:
            raise ValueError("filing_scope must not be empty")

        self._validate_market_bar(bundle, bundle.market_bar, cutoff)
        for ttm in (bundle.ttm_revenue, bundle.ttm_pat, bundle.ttm_ebitda):
            self._validate_ttm(bundle, ttm, cutoff)
        for snapshot in (bundle.equity_snapshot, bundle.net_debt_snapshot):
            self._validate_snapshot(bundle, snapshot, cutoff)
        self._validate_simplified_ev(bundle, cutoff)

        for code in VALUATION_SUBFACTOR_ORDER:
            feature = getattr(bundle, code)
            self._validate_feature(bundle, feature, code, cutoff)

    def _validate_feature(
        self,
        bundle: ValuationFeatureBundle,
        feature: ValuationMultipleValue,
        code: str,
        cutoff: datetime,
    ) -> None:
        if feature.code != code:
            raise ValueError(f"valuation feature under {code} has the wrong code")
        if feature.algorithm_version != _FEATURE_VERSIONS[code]:
            raise ValueError(f"unsupported valuation feature version for {code}")
        if feature.unit != "ratio":
            raise ValueError(f"{code} unit must be ratio")
        if self._aware_utc(feature.as_of, f"{code} as_of") != cutoff:
            raise ValueError(f"{code} as_of does not match bundle cutoff")
        if feature.available_at is not None:
            available_at = self._aware_utc(feature.available_at, f"{code} available_at")
            if available_at > cutoff:
                raise ValueError(f"{code} available_at exceeds bundle cutoff")
        for nested in feature.evidence:
            self._validate_nested_evidence(bundle, nested, cutoff)
        if feature.value is None:
            return
        if not isinstance(feature.value, Decimal):
            raise ValueError(f"{code} value must be Decimal")
        if feature.value <= 0:
            raise ValueError(f"{code} defined value must be positive")
        if feature.available_at is None:
            raise ValueError(f"{code} defined value requires available_at")
        if feature.warnings:
            raise ValueError(f"{code} defined value must not carry warnings")

    def _validate_nested_evidence(
        self,
        bundle: ValuationFeatureBundle,
        item: object,
        cutoff: datetime,
    ) -> None:
        if hasattr(item, "market_bar") and hasattr(item, "balance_sheet_snapshot"):
            market_bar = getattr(item, "market_bar")
            snapshot = getattr(item, "balance_sheet_snapshot")
            self._validate_market_bar(bundle, market_bar, cutoff)
            self._validate_snapshot(bundle, snapshot, cutoff)
            if self._aware_utc(getattr(item, "as_of"), "simplified EV as_of") != cutoff:
                raise ValueError("simplified EV as_of does not match bundle cutoff")
        elif hasattr(item, "metric_code") and hasattr(item, "ending_fiscal_year"):
            self._validate_ttm(bundle, item, cutoff)
        elif hasattr(item, "fiscal_period") and hasattr(item, "components"):
            self._validate_snapshot(bundle, item, cutoff)
        elif hasattr(item, "trading_date") and hasattr(item, "security_id"):
            self._validate_market_bar(bundle, item, cutoff)

    @staticmethod
    def _validate_market_bar(
        bundle: ValuationFeatureBundle,
        market_bar: PointInTimeMarketBar | object | None,
        cutoff: datetime,
    ) -> None:
        if market_bar is None:
            return
        if (
            getattr(market_bar, "provider_dataset_id") != bundle.market_provider_dataset_id
            or getattr(market_bar, "security_id") != bundle.security_id
        ):
            raise ValueError("market evidence does not match valuation bundle context")
        available_at = ValuationComponentScorer._aware_utc(
            getattr(market_bar, "available_at"), "market bar available_at"
        )
        if available_at > cutoff:
            raise ValueError("market bar available_at exceeds bundle cutoff")

    @staticmethod
    def _validate_ttm(
        bundle: ValuationFeatureBundle,
        ttm: TrailingTwelveMonthValue | object | None,
        cutoff: datetime,
    ) -> None:
        if ttm is None:
            return
        if (
            getattr(ttm, "provider_dataset_id") != bundle.financial_provider_dataset_id
            or getattr(ttm, "company_id") != bundle.company_id
            or getattr(ttm, "filing_scope") != bundle.filing_scope
            or getattr(ttm, "ending_fiscal_year") != bundle.ending_fiscal_year
            or getattr(ttm, "ending_fiscal_quarter") != bundle.ending_fiscal_quarter
        ):
            raise ValueError("TTM evidence does not match valuation bundle context")
        if ValuationComponentScorer._aware_utc(getattr(ttm, "as_of"), "TTM as_of") != cutoff:
            raise ValueError("TTM as_of does not match bundle cutoff")
        if getattr(ttm, "unit") != "INR" or not isinstance(getattr(ttm, "value"), Decimal):
            raise ValueError("TTM evidence must be an exact INR Decimal")
        if (
            ValuationComponentScorer._aware_utc(getattr(ttm, "available_at"), "TTM available_at")
            > cutoff
        ):
            raise ValueError("TTM available_at exceeds bundle cutoff")

    @staticmethod
    def _validate_snapshot(
        bundle: ValuationFeatureBundle,
        snapshot: InstantFinancialSnapshot | object | None,
        cutoff: datetime,
    ) -> None:
        if snapshot is None:
            return
        period = getattr(snapshot, "fiscal_period")
        if (
            getattr(snapshot, "provider_dataset_id") != bundle.financial_provider_dataset_id
            or getattr(snapshot, "company_id") != bundle.company_id
            or getattr(snapshot, "filing_scope") != bundle.filing_scope
            or period.fiscal_year != bundle.ending_fiscal_year
            or period.fiscal_quarter != bundle.ending_fiscal_quarter
        ):
            raise ValueError("snapshot evidence does not match valuation bundle context")
        if (
            ValuationComponentScorer._aware_utc(getattr(snapshot, "as_of"), "snapshot as_of")
            != cutoff
        ):
            raise ValueError("snapshot as_of does not match bundle cutoff")
        if (
            ValuationComponentScorer._aware_utc(
                getattr(snapshot, "available_at"), "snapshot available_at"
            )
            > cutoff
        ):
            raise ValueError("snapshot available_at exceeds bundle cutoff")
        if any(
            component.unit != "INR" or not isinstance(component.value, Decimal)
            for component in getattr(snapshot, "components")
        ):
            raise ValueError("snapshot evidence must be exact INR Decimals")

    def _validate_simplified_ev(self, bundle: ValuationFeatureBundle, cutoff: datetime) -> None:
        simplified_ev = bundle.simplified_enterprise_value
        if simplified_ev.algorithm_version != "simplified_enterprise_value_v1":
            raise ValueError("unsupported simplified enterprise value version")
        if self._aware_utc(simplified_ev.as_of, "simplified EV as_of") != cutoff:
            raise ValueError("simplified EV as_of does not match bundle cutoff")
        if simplified_ev.unit != "INR":
            raise ValueError("simplified enterprise value unit must be INR")
        if simplified_ev.available_at is not None:
            available_at = self._aware_utc(simplified_ev.available_at, "simplified EV available_at")
            if available_at > cutoff:
                raise ValueError("simplified EV available_at exceeds bundle cutoff")
        self._validate_market_bar(bundle, simplified_ev.market_bar, cutoff)
        self._validate_snapshot(bundle, simplified_ev.balance_sheet_snapshot, cutoff)

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)


def score_valuation_component(
    *,
    evidence: ValuationFeatureBundle,
    policy: InflectionScoringPolicy,
) -> ValuationComponentScore:
    return ValuationComponentScorer().score(evidence=evidence, policy=policy)
