"""Pure Phase 4D-F Market Structure component scoring primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    from inflector_data.market_structure_features import (
        MarketStructureFeatureBundle,
        MarketStructureFeatureValue,
    )

MARKET_STRUCTURE_COMPONENT_VERSION = "market_structure_component_v1"

MARKET_STRUCTURE_SUBFACTOR_ORDER = (
    "relative_strength_60_to_benchmark",
    "close_to_sma20",
    "sma20_to_sma60",
    "volatility_ratio_20_to_60",
    "consolidation_range_20",
    "close_times_volume_ratio_20_to_60",
    "average_delivery_percentage_20",
)

_FEATURE_VERSIONS = {
    "relative_strength_60_to_benchmark": "relative_strength_60_to_benchmark_v1",
    "close_to_sma20": "close_to_sma20_v1",
    "sma20_to_sma60": "sma20_to_sma60_v1",
    "volatility_ratio_20_to_60": "volatility_ratio_20_to_60_v1",
    "consolidation_range_20": "consolidation_range_20_v1",
    "close_times_volume_ratio_20_to_60": "close_times_volume_ratio_20_to_60_v1",
    "average_delivery_percentage_20": "average_delivery_percentage_20_v1",
}

_CURVE_FIELDS = {
    "relative_strength_60_to_benchmark": "relative_strength_60_to_benchmark_curve",
    "close_to_sma20": "close_to_sma20_curve",
    "sma20_to_sma60": "sma20_to_sma60_curve",
    "volatility_ratio_20_to_60": "volatility_ratio_20_to_60_signal_curve",
    "consolidation_range_20": "consolidation_range_20_signal_curve",
    "close_times_volume_ratio_20_to_60": "close_times_volume_ratio_20_to_60_curve",
    "average_delivery_percentage_20": "average_delivery_percentage_20_curve",
}

_TRANSFORM_CODES = {
    "relative_strength_60_to_benchmark": "identity",
    "close_to_sma20": "identity",
    "sma20_to_sma60": "identity",
    "volatility_ratio_20_to_60": "negate_volatility_ratio_20_to_60",
    "consolidation_range_20": "negate_consolidation_range_20",
    "close_times_volume_ratio_20_to_60": "identity",
    "average_delivery_percentage_20": "identity",
}

_MINIMUM_VALUES = {
    "relative_strength_60_to_benchmark": Decimal("-1"),
    "close_to_sma20": Decimal("-1"),
    "sma20_to_sma60": Decimal("-1"),
    "volatility_ratio_20_to_60": Decimal("0"),
    "consolidation_range_20": Decimal("0"),
    "close_times_volume_ratio_20_to_60": Decimal("0"),
    "average_delivery_percentage_20": Decimal("0"),
}


@dataclass(frozen=True, slots=True)
class MarketStructureUnavailableSubfactor:
    code: str
    configured_weight: Decimal
    warnings: tuple[str, ...]
    evidence_type: str
    evidence: object


@dataclass(frozen=True, slots=True)
class MarketStructureSubfactorScore:
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
class MarketStructureComponentScore:
    security_id: UUID
    market_provider_dataset_id: UUID
    corporate_action_provider_dataset_id: UUID
    benchmark_provider_dataset_id: UUID
    benchmark_code: str
    interval: str
    basis_date: date | None
    market_on_or_before: date | None
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[MarketStructureSubfactorScore, ...]
    unavailable_subfactors: tuple[MarketStructureUnavailableSubfactor, ...]
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


class MarketStructureComponentScorer:
    """Score approved Phase 3G-C features without recomputing market evidence."""

    def score(
        self,
        *,
        evidence: MarketStructureFeatureBundle,
        policy: InflectionScoringPolicy,
    ) -> MarketStructureComponentScore:
        cutoff = self._aware_utc(evidence.as_of, "bundle as_of")
        scoring = policy.market_structure
        if scoring is None:
            raise ValueError("market structure scoring policy is not configured")
        self._validate_bundle(evidence, cutoff)

        raw: dict[str, _RawSubfactor] = {}
        unavailable: list[MarketStructureUnavailableSubfactor] = []
        for code in MARKET_STRUCTURE_SUBFACTOR_ORDER:
            weight = getattr(scoring.subfactor_weights, code)
            if weight == 0:
                continue
            feature = getattr(evidence, code)
            if feature.value is None:
                unavailable.append(
                    MarketStructureUnavailableSubfactor(
                        code=code,
                        configured_weight=weight,
                        warnings=feature.warnings,
                        evidence_type=type(feature).__name__,
                        evidence=feature,
                    )
                )
                continue
            assert feature.available_at is not None
            transform_code = _TRANSFORM_CODES[code]
            scoring_value = -feature.value if transform_code != "identity" else feature.value
            raw[code] = _RawSubfactor(
                code=code,
                raw_value=feature.value,
                scoring_value=scoring_value,
                weight=weight,
                available_at=self._aware_utc(feature.available_at, f"{code} available_at"),
                evidence=feature,
                transform_code=transform_code,
                curve=getattr(scoring, _CURVE_FIELDS[code]),
            )

        available_weight = sum((item.weight for item in raw.values()), Decimal("0"))
        sufficient = available_weight >= scoring.minimum_weight_coverage
        subfactors = tuple(
            self._score_subfactor(raw[code], available_weight, sufficient)
            for code in MARKET_STRUCTURE_SUBFACTOR_ORDER
            if code in raw
        )
        score = (
            sum((item.contribution for item in subfactors), Decimal("0")) if sufficient else None
        )
        if score is not None and not Decimal("0") <= score <= Decimal("100"):
            raise AssertionError("component score escaped the validated 0-100 range")
        return MarketStructureComponentScore(
            security_id=evidence.security_id,
            market_provider_dataset_id=evidence.market_provider_dataset_id,
            corporate_action_provider_dataset_id=evidence.corporate_action_provider_dataset_id,
            benchmark_provider_dataset_id=evidence.benchmark_provider_dataset_id,
            benchmark_code=evidence.benchmark_code,
            interval=evidence.interval,
            basis_date=evidence.basis_date,
            market_on_or_before=evidence.market_on_or_before,
            score=score,
            unit="score_0_100",
            weight_coverage=available_weight,
            available_weight=available_weight,
            subfactors=subfactors,
            unavailable_subfactors=tuple(unavailable),
            missing_subfactors=tuple(item.code for item in unavailable),
            warnings=() if sufficient else ("insufficient_subfactor_coverage",),
            as_of=cutoff,
            available_at=max(
                (item.input_available_at for item in subfactors),
                default=None,
            ),
            algorithm_version=MARKET_STRUCTURE_COMPONENT_VERSION,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> MarketStructureSubfactorScore:
        normalized_score = score_piecewise_linear(raw.scoring_value, raw.curve)
        effective_weight = raw.weight / available_weight if sufficient else Decimal("0")
        return MarketStructureSubfactorScore(
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

    def _validate_bundle(
        self,
        bundle: MarketStructureFeatureBundle,
        cutoff: datetime,
    ) -> None:
        if bundle.algorithm_version != "market_structure_feature_bundle_v1":
            raise ValueError("unsupported market structure feature bundle version")
        for field_name in (
            "market_provider_dataset_id",
            "corporate_action_provider_dataset_id",
            "benchmark_provider_dataset_id",
            "security_id",
        ):
            if not isinstance(getattr(bundle, field_name), UUID):
                raise ValueError(f"{field_name} must be a UUID")
        if not bundle.benchmark_code.strip():
            raise ValueError("benchmark_code must not be empty")
        if not bundle.interval.strip():
            raise ValueError("interval must not be empty")
        if bundle.basis_date is not None and not isinstance(bundle.basis_date, date):
            raise ValueError("basis_date must be a date or None")
        if bundle.market_on_or_before is not None and not isinstance(
            bundle.market_on_or_before, date
        ):
            raise ValueError("market_on_or_before must be a date or None")
        if (
            bundle.basis_date is not None
            and bundle.market_on_or_before is not None
            and bundle.basis_date > bundle.market_on_or_before
        ):
            raise ValueError("basis_date exceeds market_on_or_before")

        for code in MARKET_STRUCTURE_SUBFACTOR_ORDER:
            self._validate_feature(bundle, getattr(bundle, code), code, cutoff)

    def _validate_feature(
        self,
        bundle: MarketStructureFeatureBundle,
        feature: MarketStructureFeatureValue,
        code: str,
        cutoff: datetime,
    ) -> None:
        if feature.code != code:
            raise ValueError(f"market structure feature under {code} has the wrong code")
        if feature.algorithm_version != _FEATURE_VERSIONS[code]:
            raise ValueError(f"unsupported market structure feature version for {code}")
        if feature.unit != "ratio":
            raise ValueError(f"{code} unit must be ratio")
        if self._aware_utc(feature.as_of, f"{code} as_of") != cutoff:
            raise ValueError(f"{code} as_of does not match bundle cutoff")
        if feature.available_at is not None:
            available_at = self._aware_utc(feature.available_at, f"{code} available_at")
            if available_at > cutoff:
                raise ValueError(f"{code} available_at exceeds bundle cutoff")
        if feature.value is None:
            if not feature.warnings:
                raise ValueError(f"{code} unavailable value requires warnings")
            return
        if not isinstance(feature.value, Decimal):
            raise ValueError(f"{code} value must be Decimal")
        if feature.value < _MINIMUM_VALUES[code]:
            raise ValueError(f"{code} value is outside its semantic domain")
        if code == "average_delivery_percentage_20" and feature.value > Decimal("1"):
            raise ValueError(f"{code} value is outside its semantic domain")
        if feature.available_at is None:
            raise ValueError(f"{code} defined value requires available_at")
        if feature.warnings:
            raise ValueError(f"{code} defined value must not carry warnings")
        if bundle.basis_date is None:
            raise ValueError(f"{code} available value requires bundle basis_date")
        self._validate_feature_evidence(bundle, feature, code, cutoff)

    def _validate_feature_evidence(
        self,
        bundle: MarketStructureFeatureBundle,
        feature: MarketStructureFeatureValue,
        code: str,
        cutoff: datetime,
    ) -> None:
        evidence = feature.evidence
        if code == "relative_strength_60_to_benchmark":
            self._validate_relative_strength_evidence(bundle, evidence, cutoff)
        elif code in {"close_to_sma20", "sma20_to_sma60"}:
            self._validate_adjusted_window(bundle, getattr(evidence, "bars", ()), cutoff)
            self._validate_evidence_basis(bundle, evidence)
        elif code == "volatility_ratio_20_to_60":
            self._validate_volatility_ratio_evidence(bundle, evidence, cutoff)
        elif code == "consolidation_range_20":
            self._validate_adjusted_window(bundle, getattr(evidence, "bars", ()), cutoff)
            self._validate_evidence_basis(bundle, evidence)
        elif code in {
            "close_times_volume_ratio_20_to_60",
            "average_delivery_percentage_20",
        }:
            self._validate_raw_window(bundle, getattr(evidence, "bars", ()), cutoff)
            self._validate_evidence_basis(bundle, evidence)

    def _validate_relative_strength_evidence(
        self,
        bundle: MarketStructureFeatureBundle,
        evidence: object,
        cutoff: datetime,
    ) -> None:
        try:
            security_start = getattr(evidence, "security_start")
            security_end = getattr(evidence, "security_end")
            benchmark_start = getattr(evidence, "benchmark_start")
            benchmark_end = getattr(evidence, "benchmark_end")
        except AttributeError as error:
            raise ValueError("relative-strength evidence is incomplete") from error
        self._validate_adjusted_bar(bundle, security_start, cutoff)
        self._validate_adjusted_bar(bundle, security_end, cutoff)
        if getattr(security_end, "raw_bar").trading_date != bundle.basis_date:
            raise ValueError("relative-strength endpoint does not match bundle basis_date")
        if benchmark_start is None or benchmark_end is None:
            raise ValueError("available relative strength requires both benchmark endpoints")
        self._validate_benchmark_bar(bundle, benchmark_start, cutoff)
        self._validate_benchmark_bar(bundle, benchmark_end, cutoff)
        if benchmark_start.trading_date != security_start.raw_bar.trading_date:
            raise ValueError("relative-strength start dates do not match")
        if benchmark_end.trading_date != security_end.raw_bar.trading_date:
            raise ValueError("relative-strength end dates do not match")

    def _validate_volatility_ratio_evidence(
        self,
        bundle: MarketStructureFeatureBundle,
        evidence: object,
        cutoff: datetime,
    ) -> None:
        expected = (
            ("short_volatility", "return_volatility_20", "return_volatility_20_v1"),
            ("medium_volatility", "return_volatility_60", "return_volatility_60_v1"),
        )
        for field_name, code, version in expected:
            nested = getattr(evidence, field_name, None)
            if nested is None:
                raise ValueError("volatility-ratio evidence is incomplete")
            if nested.code != code or nested.algorithm_version != version:
                raise ValueError("volatility-ratio evidence has unsupported semantics")
            if nested.unit != "ratio":
                raise ValueError("nested volatility unit must be ratio")
            if self._aware_utc(nested.as_of, f"{code} as_of") != cutoff:
                raise ValueError("nested volatility cutoff does not match bundle cutoff")
            if nested.value is None or not isinstance(nested.value, Decimal):
                raise ValueError("available volatility ratio requires defined Decimal inputs")
            if nested.value < 0 or nested.warnings:
                raise ValueError("nested volatility evidence is contradictory")
            if nested.available_at is None:
                raise ValueError("nested volatility evidence requires available_at")
            if self._aware_utc(nested.available_at, f"{code} available_at") > cutoff:
                raise ValueError("nested volatility available_at exceeds bundle cutoff")
            returns = getattr(nested.evidence, "returns", ())
            if not returns:
                raise ValueError("nested volatility return evidence is empty")
            for item in returns:
                self._validate_return(bundle, item, cutoff)
            if returns[-1].current_bar.raw_bar.trading_date != bundle.basis_date:
                raise ValueError("volatility endpoint does not match bundle basis_date")

    def _validate_return(
        self,
        bundle: MarketStructureFeatureBundle,
        value: object,
        cutoff: datetime,
    ) -> None:
        if (
            getattr(value, "security_id", None) != bundle.security_id
            or getattr(value, "unit", None) != "ratio"
            or getattr(value, "algorithm_version", None) != "simple_price_return_v1"
        ):
            raise ValueError("return evidence does not match market structure context")
        if self._aware_utc(getattr(value, "as_of"), "return as_of") != cutoff:
            raise ValueError("return cutoff does not match bundle cutoff")
        if self._aware_utc(getattr(value, "available_at"), "return available_at") > cutoff:
            raise ValueError("return available_at exceeds bundle cutoff")
        self._validate_adjusted_bar(bundle, getattr(value, "previous_bar"), cutoff)
        self._validate_adjusted_bar(bundle, getattr(value, "current_bar"), cutoff)

    def _validate_adjusted_window(
        self,
        bundle: MarketStructureFeatureBundle,
        bars: object,
        cutoff: datetime,
    ) -> None:
        if not isinstance(bars, tuple) or not bars:
            raise ValueError("adjusted market evidence window must not be empty")
        for bar in bars:
            self._validate_adjusted_bar(bundle, bar, cutoff)
        if bars[-1].raw_bar.trading_date != bundle.basis_date:
            raise ValueError("adjusted market endpoint does not match bundle basis_date")

    def _validate_raw_window(
        self,
        bundle: MarketStructureFeatureBundle,
        bars: object,
        cutoff: datetime,
    ) -> None:
        if not isinstance(bars, tuple) or not bars:
            raise ValueError("raw market evidence window must not be empty")
        for bar in bars:
            self._validate_market_bar(bundle, bar, cutoff)
        if bars[-1].trading_date != bundle.basis_date:
            raise ValueError("raw market endpoint does not match bundle basis_date")

    @staticmethod
    def _validate_evidence_basis(bundle: MarketStructureFeatureBundle, evidence: object) -> None:
        if getattr(evidence, "basis_date", None) != bundle.basis_date:
            raise ValueError("feature evidence basis_date does not match bundle basis_date")

    def _validate_adjusted_bar(
        self,
        bundle: MarketStructureFeatureBundle,
        bar: object,
        cutoff: datetime,
    ) -> None:
        self._validate_market_bar(bundle, getattr(bar, "raw_bar", None), cutoff)
        if self._aware_utc(getattr(bar, "as_of"), "adjusted bar as_of") != cutoff:
            raise ValueError("adjusted bar cutoff does not match bundle cutoff")
        if getattr(bar, "algorithm_version", None) != "adjusted_market_price_v1":
            raise ValueError("unsupported adjusted market bar version")
        if getattr(bar, "adjustment_basis_date", None) != bundle.basis_date:
            raise ValueError("adjusted bar basis_date does not match bundle basis_date")
        if self._aware_utc(getattr(bar, "available_at"), "adjusted bar available_at") > cutoff:
            raise ValueError("adjusted bar available_at exceeds bundle cutoff")
        for adjustment in getattr(bar, "applied_adjustments", ()):
            action = getattr(adjustment, "action", None)
            if (
                action is None
                or action.provider_dataset_id != bundle.corporate_action_provider_dataset_id
                or action.security_id != bundle.security_id
            ):
                raise ValueError("corporate-action evidence does not match bundle context")
            if self._aware_utc(action.available_at, "corporate action available_at") > cutoff:
                raise ValueError("corporate action available_at exceeds bundle cutoff")
            self._validate_accepted_source(action)

    def _validate_market_bar(
        self,
        bundle: MarketStructureFeatureBundle,
        bar: object,
        cutoff: datetime,
    ) -> None:
        if bar is None or (
            getattr(bar, "provider_dataset_id", None) != bundle.market_provider_dataset_id
            or getattr(bar, "security_id", None) != bundle.security_id
            or getattr(bar, "interval", None) != bundle.interval
        ):
            raise ValueError("market evidence does not match market structure bundle context")
        if self._aware_utc(getattr(bar, "available_at"), "market bar available_at") > cutoff:
            raise ValueError("market bar available_at exceeds bundle cutoff")
        self._validate_accepted_source(bar)

    def _validate_benchmark_bar(
        self,
        bundle: MarketStructureFeatureBundle,
        bar: object,
        cutoff: datetime,
    ) -> None:
        series = getattr(bar, "benchmark_series", None)
        if series is None or (
            getattr(series, "provider_dataset_id", None) != bundle.benchmark_provider_dataset_id
            or getattr(series, "code", None) != bundle.benchmark_code
            or getattr(bar, "interval", None) != bundle.interval
        ):
            raise ValueError("benchmark evidence does not match market structure bundle context")
        if self._aware_utc(getattr(bar, "available_at"), "benchmark bar available_at") > cutoff:
            raise ValueError("benchmark bar available_at exceeds bundle cutoff")
        self._validate_accepted_source(bar)

    @staticmethod
    def _validate_accepted_source(value: object) -> None:
        source = getattr(value, "source_record", None)
        if source is None or getattr(source, "validation_status", None) != "accepted":
            raise ValueError("nested source lineage must be accepted")

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)


def score_market_structure_component(
    *,
    evidence: MarketStructureFeatureBundle,
    policy: InflectionScoringPolicy,
) -> MarketStructureComponentScore:
    return MarketStructureComponentScorer().score(evidence=evidence, policy=policy)
