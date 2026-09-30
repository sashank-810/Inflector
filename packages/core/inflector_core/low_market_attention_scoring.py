"""Pure Phase 4D-J Low Market Attention component scoring primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from inflector_core.component_scoring import (
    PIECEWISE_LINEAR_CURVE_VERSION,
    score_piecewise_linear,
)
from inflector_core.scoring_policy import (
    AttentionScoringSeriesIdentity,
    InflectionScoringPolicy,
    LowMarketAttentionScoringPolicy,
    PiecewiseLinearScoringCurve,
)

if TYPE_CHECKING:
    from inflector_data.attention_features import (
        AttentionFeatureBundle,
        AttentionFeatureValue,
        AttentionSeriesIdentity,
    )
    from inflector_data.attention_pit import PointInTimeAttentionObservation

LOW_MARKET_ATTENTION_COMPONENT_VERSION = "low_market_attention_component_v1"
LOW_MARKET_ATTENTION_SUBFACTOR_ORDER = (
    "news_mentions_count",
    "analyst_coverage_count",
)

_ATTENTION_FEATURE_BUNDLE_VERSION = "attention_feature_bundle_v1"
_FEATURE_SPECS = {
    "news_mentions_count": ("news_mentions_count_feature_v1", "count"),
    "news_window_duration_days": ("news_window_duration_days_v1", "days"),
    "news_window_age_days": ("news_window_age_days_v1", "days"),
    "analyst_coverage_count": ("analyst_coverage_count_feature_v1", "count"),
    "analyst_snapshot_age_days": ("analyst_snapshot_age_days_v1", "days"),
}
_MISSING_WARNINGS = {
    "news": "missing_news_mentions_observation",
    "analyst": "missing_analyst_coverage_observation",
}
_COVERAGE_WARNINGS = {
    "news": {
        "partial": "partial_news_attention_coverage",
        "unknown": "unknown_news_attention_coverage",
    },
    "analyst": {
        "partial": "partial_analyst_attention_coverage",
        "unknown": "unknown_analyst_attention_coverage",
    },
}


@dataclass(frozen=True, slots=True)
class LowMarketAttentionUnavailableSubfactor:
    code: str
    configured_weight: Decimal
    warnings: tuple[str, ...]
    evidence_type: str
    evidence: AttentionFeatureValue


@dataclass(frozen=True, slots=True)
class LowMarketAttentionSubfactorScore:
    code: str
    raw_value: Decimal
    raw_unit: str
    scoring_value: Decimal
    scoring_unit: str
    transform_code: str
    normalized_score: Decimal
    configured_weight: Decimal
    effective_weight: Decimal
    contribution: Decimal
    input_available_at: datetime
    evidence_type: str
    evidence: AttentionFeatureValue
    curve_algorithm_version: str


@dataclass(frozen=True, slots=True)
class LowMarketAttentionComponentScore:
    company_id: UUID
    security_id: UUID | None
    company_level_only: bool
    news_series: AttentionSeriesIdentity
    analyst_series: AttentionSeriesIdentity
    news_window_start_at: datetime
    news_window_end_at: datetime
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[LowMarketAttentionSubfactorScore, ...]
    unavailable_subfactors: tuple[LowMarketAttentionUnavailableSubfactor, ...]
    missing_subfactors: tuple[str, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    evidence: AttentionFeatureBundle
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class _RawSubfactor:
    code: str
    raw_value: Decimal
    scoring_value: Decimal
    weight: Decimal
    available_at: datetime
    evidence: AttentionFeatureValue
    transform_code: str
    curve: PiecewiseLinearScoringCurve


class LowMarketAttentionComponentScorer:
    """Score one explicit Phase 6D-B bundle without reads or recomputation."""

    def score(
        self,
        *,
        evidence: AttentionFeatureBundle,
        policy: InflectionScoringPolicy,
    ) -> LowMarketAttentionComponentScore:
        scoring = policy.low_market_attention
        if scoring is None:
            raise ValueError("low market attention scoring policy is not configured")
        cutoff = _aware_utc(evidence.as_of, "bundle as_of")
        self._validate_bundle(evidence, scoring, cutoff)

        raw: dict[str, _RawSubfactor] = {}
        unavailable: list[LowMarketAttentionUnavailableSubfactor] = []
        self._collect_news(evidence, scoring, raw, unavailable)
        self._collect_analyst(evidence, scoring, raw, unavailable)

        available_weight = sum((item.weight for item in raw.values()), Decimal("0"))
        sufficient = available_weight >= scoring.minimum_weight_coverage
        subfactors = tuple(
            self._score_subfactor(raw[code], available_weight, sufficient)
            for code in LOW_MARKET_ATTENTION_SUBFACTOR_ORDER
            if code in raw
        )
        score = (
            sum((item.contribution for item in subfactors), Decimal("0"))
            if sufficient
            else None
        )
        if score is not None and not Decimal("0") <= score <= Decimal("100"):
            raise AssertionError("component score escaped the validated 0-100 range")
        observation_times = tuple(
            _aware_utc(observation.available_at, "observation available_at")
            for observation in (evidence.news_observation, evidence.analyst_observation)
            if observation is not None
        )
        return LowMarketAttentionComponentScore(
            company_id=evidence.company_id,
            security_id=evidence.security_id,
            company_level_only=evidence.company_level_only,
            news_series=evidence.news_series,
            analyst_series=evidence.analyst_series,
            news_window_start_at=_aware_utc(
                evidence.news_window_start_at, "news_window_start_at"
            ),
            news_window_end_at=_aware_utc(
                evidence.news_window_end_at, "news_window_end_at"
            ),
            score=score,
            unit="score_0_100",
            weight_coverage=available_weight,
            available_weight=available_weight,
            subfactors=subfactors,
            unavailable_subfactors=tuple(unavailable),
            missing_subfactors=tuple(item.code for item in unavailable),
            warnings=() if sufficient else ("insufficient_subfactor_coverage",),
            as_of=cutoff,
            available_at=max(observation_times, default=None),
            evidence=evidence,
            algorithm_version=LOW_MARKET_ATTENTION_COMPONENT_VERSION,
        )

    @staticmethod
    def _collect_news(
        bundle: AttentionFeatureBundle,
        policy: LowMarketAttentionScoringPolicy,
        raw: dict[str, _RawSubfactor],
        unavailable: list[LowMarketAttentionUnavailableSubfactor],
    ) -> None:
        code = "news_mentions_count"
        weight = policy.subfactor_weights.news_mentions_count
        if weight == 0:
            return
        feature = bundle.news_mentions_count
        if feature.value is None:
            unavailable.append(_unavailable(code, weight, feature.warnings, feature))
            return
        warnings: list[str] = []
        gate_evidence = feature
        if (
            bundle.news_window_duration_days.value
            != policy.required_news_window_duration_days
        ):
            warnings.append("news_window_duration_mismatch")
            gate_evidence = bundle.news_window_duration_days
        if (
            bundle.news_window_age_days.value is not None
            and bundle.news_window_age_days.value > policy.maximum_news_window_age_days
        ):
            warnings.append("news_attention_stale")
            if not warnings[:-1]:
                gate_evidence = bundle.news_window_age_days
        if warnings:
            unavailable.append(_unavailable(code, weight, tuple(warnings), gate_evidence))
            return
        assert feature.available_at is not None
        raw[code] = _RawSubfactor(
            code=code,
            raw_value=feature.value,
            scoring_value=-feature.value,
            weight=weight,
            available_at=_aware_utc(feature.available_at, f"{code} available_at"),
            evidence=feature,
            transform_code="negate_news_mentions_count",
            curve=policy.news_mentions_signal_curve,
        )

    @staticmethod
    def _collect_analyst(
        bundle: AttentionFeatureBundle,
        policy: LowMarketAttentionScoringPolicy,
        raw: dict[str, _RawSubfactor],
        unavailable: list[LowMarketAttentionUnavailableSubfactor],
    ) -> None:
        code = "analyst_coverage_count"
        weight = policy.subfactor_weights.analyst_coverage_count
        if weight == 0:
            return
        feature = bundle.analyst_coverage_count
        if feature.value is None:
            unavailable.append(_unavailable(code, weight, feature.warnings, feature))
            return
        age = bundle.analyst_snapshot_age_days
        if (
            age.value is not None
            and age.value > Decimal(policy.maximum_analyst_snapshot_age_days)
        ):
            unavailable.append(
                _unavailable(code, weight, ("analyst_attention_stale",), age)
            )
            return
        assert feature.available_at is not None
        raw[code] = _RawSubfactor(
            code=code,
            raw_value=feature.value,
            scoring_value=-feature.value,
            weight=weight,
            available_at=_aware_utc(feature.available_at, f"{code} available_at"),
            evidence=feature,
            transform_code="negate_analyst_coverage_count",
            curve=policy.analyst_coverage_signal_curve,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> LowMarketAttentionSubfactorScore:
        normalized = score_piecewise_linear(raw.scoring_value, raw.curve)
        effective = raw.weight / available_weight if sufficient else Decimal("0")
        return LowMarketAttentionSubfactorScore(
            code=raw.code,
            raw_value=raw.raw_value,
            raw_unit="count",
            scoring_value=raw.scoring_value,
            scoring_unit="count",
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

    def _validate_bundle(
        self,
        bundle: AttentionFeatureBundle,
        policy: LowMarketAttentionScoringPolicy,
        cutoff: datetime,
    ) -> None:
        if bundle.algorithm_version != _ATTENTION_FEATURE_BUNDLE_VERSION:
            raise ValueError("unsupported attention feature bundle version")
        if bundle.analyst_observation_on_or_before is not None:
            raise ValueError("analyst date bound is not allowed for scoring")
        _validate_series(bundle.news_series, policy.news_series, "news")
        _validate_series(bundle.analyst_series, policy.analyst_series, "analyst")
        if policy.evidence_level == "company_level_v1":
            if not bundle.company_level_only or bundle.security_id is not None:
                raise ValueError("attention evidence level does not match company-level policy")
        elif bundle.company_level_only or bundle.security_id is None:
            raise ValueError("attention evidence level does not match security-level policy")

        expected_end = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=UTC)
        expected_start = expected_end - _duration(policy.required_news_window_duration_days)
        if (
            _aware_utc(bundle.news_window_start_at, "news_window_start_at")
            != expected_start
            or _aware_utc(bundle.news_window_end_at, "news_window_end_at")
            != expected_end
        ):
            raise ValueError("news window does not match deterministic policy alignment")

        for code, (version, unit) in _FEATURE_SPECS.items():
            feature = getattr(bundle, code)
            _validate_feature_identity(feature, code, version, unit, cutoff)
        _validate_news_evidence(bundle, cutoff)
        _validate_analyst_evidence(bundle, cutoff)


def _validate_series(
    actual: AttentionSeriesIdentity,
    expected: AttentionScoringSeriesIdentity,
    label: str,
) -> None:
    if (
        actual.provider_dataset_id != expected.provider_dataset_id
        or actual.scope_code != expected.scope_code
        or actual.methodology_version != expected.methodology_version
        or actual.measurement_definition_sha256.lower()
        != expected.measurement_definition_sha256
    ):
        raise ValueError(f"{label} attention series does not match scoring policy")


def _validate_feature_identity(
    feature: AttentionFeatureValue,
    code: str,
    version: str,
    unit: str,
    cutoff: datetime,
) -> None:
    if feature.code != code or feature.algorithm_version != version or feature.unit != unit:
        raise ValueError(f"{code} feature identity is incoherent")
    if feature.available_at is not None and _aware_utc(
        feature.available_at, f"{code} available_at"
    ) > cutoff:
        raise ValueError(f"{code} uses future evidence")
    if feature.value is not None:
        if not isinstance(feature.value, Decimal):
            raise ValueError(f"{code} must use Decimal")
        if code in LOW_MARKET_ATTENTION_SUBFACTOR_ORDER:
            if feature.value < 0 or feature.value != feature.value.to_integral_value():
                raise ValueError(f"{code} must be a non-negative integral Decimal")
        elif code == "news_window_duration_days":
            if feature.value <= 0:
                raise ValueError("news window duration must be positive")
        elif feature.value < 0:
            raise ValueError(f"{code} must not be negative")


def _validate_news_evidence(bundle: AttentionFeatureBundle, cutoff: datetime) -> None:
    observation = bundle.news_observation
    count = bundle.news_mentions_count
    duration = bundle.news_window_duration_days
    age = bundle.news_window_age_days
    if observation is None:
        for feature in (count, duration, age):
            _validate_missing(feature, _MISSING_WARNINGS["news"])
        return
    _validate_observation_common(
        observation,
        bundle=bundle,
        provider_dataset_id=bundle.news_series.provider_dataset_id,
        scope_code=bundle.news_series.scope_code,
        methodology_version=bundle.news_series.methodology_version,
        measurement_definition_sha256=bundle.news_series.measurement_definition_sha256,
        cutoff=cutoff,
    )
    if (
        observation.metric_code != "news_mentions_count"
        or observation.reported_unit != "count"
        or observation.observation_date is not None
        or observation.window_start_at is None
        or observation.window_end_at is None
        or _aware_utc(observation.window_start_at, "news observation window start")
        != _aware_utc(bundle.news_window_start_at, "bundle news window start")
        or _aware_utc(observation.window_end_at, "news observation window end")
        != _aware_utc(bundle.news_window_end_at, "bundle news window end")
    ):
        raise ValueError("news observation is incoherent with feature bundle")
    _validate_present_time_feature(duration, observation)
    _validate_present_time_feature(age, observation)
    _validate_count_coverage(count, observation, "news")


def _validate_analyst_evidence(bundle: AttentionFeatureBundle, cutoff: datetime) -> None:
    observation = bundle.analyst_observation
    count = bundle.analyst_coverage_count
    age = bundle.analyst_snapshot_age_days
    if observation is None:
        _validate_missing(count, _MISSING_WARNINGS["analyst"])
        _validate_missing(age, _MISSING_WARNINGS["analyst"])
        return
    _validate_observation_common(
        observation,
        bundle=bundle,
        provider_dataset_id=bundle.analyst_series.provider_dataset_id,
        scope_code=bundle.analyst_series.scope_code,
        methodology_version=bundle.analyst_series.methodology_version,
        measurement_definition_sha256=bundle.analyst_series.measurement_definition_sha256,
        cutoff=cutoff,
    )
    if (
        observation.metric_code != "analyst_coverage_count"
        or observation.reported_unit != "count"
        or observation.observation_date is None
        or observation.window_start_at is not None
        or observation.window_end_at is not None
    ):
        raise ValueError("analyst observation is incoherent with feature bundle")
    _validate_present_time_feature(age, observation)
    _validate_count_coverage(count, observation, "analyst")


def _validate_observation_common(
    observation: PointInTimeAttentionObservation,
    *,
    bundle: AttentionFeatureBundle,
    provider_dataset_id: UUID,
    scope_code: str,
    methodology_version: str,
    measurement_definition_sha256: str,
    cutoff: datetime,
) -> None:
    expected_security = None if bundle.company_level_only else bundle.security_id
    if (
        observation.company_id != bundle.company_id
        or observation.security_id != expected_security
        or observation.provider_dataset_id != provider_dataset_id
        or observation.scope_code != scope_code
        or observation.methodology_version != methodology_version
        or observation.measurement_definition_sha256.lower()
        != measurement_definition_sha256.lower()
        or observation.coverage_status not in {"complete", "partial", "unknown"}
        or type(observation.reported_count) is not int
        or observation.reported_count < 0
        or observation.source_record.validation_status != "accepted"
        or _aware_utc(observation.available_at, "observation available_at") > cutoff
    ):
        raise ValueError("attention observation is incoherent with feature bundle")


def _validate_count_coverage(
    feature: AttentionFeatureValue,
    observation: PointInTimeAttentionObservation,
    kind: str,
) -> None:
    if feature.evidence != (observation,) or feature.available_at != observation.available_at:
        raise ValueError(f"{kind} count feature does not retain selected observation")
    if observation.coverage_status == "complete":
        if (
            feature.value != Decimal(observation.reported_count)
            or feature.warnings
        ):
            raise ValueError(f"complete {kind} count feature is incoherent")
        return
    expected_warning = _COVERAGE_WARNINGS[kind][observation.coverage_status]
    if feature.value is not None or feature.warnings != (expected_warning,):
        raise ValueError(f"blocked {kind} count feature is incoherent")


def _validate_present_time_feature(
    feature: AttentionFeatureValue,
    observation: PointInTimeAttentionObservation,
) -> None:
    if (
        feature.value is None
        or feature.warnings
        or feature.available_at != observation.available_at
        or feature.evidence != (observation,)
    ):
        raise ValueError(f"{feature.code} does not retain selected observation")


def _validate_missing(feature: AttentionFeatureValue, warning: str) -> None:
    if (
        feature.value is not None
        or feature.available_at is not None
        or feature.evidence
        or feature.warnings != (warning,)
    ):
        raise ValueError(f"{feature.code} missing evidence state is incoherent")


def _unavailable(
    code: str,
    weight: Decimal,
    warnings: tuple[str, ...],
    evidence: AttentionFeatureValue,
) -> LowMarketAttentionUnavailableSubfactor:
    return LowMarketAttentionUnavailableSubfactor(
        code=code,
        configured_weight=weight,
        warnings=warnings,
        evidence_type=type(evidence).__name__,
        evidence=evidence,
    )


def _duration(days: Decimal) -> timedelta:
    microseconds = days * Decimal("86400000000")
    if microseconds != microseconds.to_integral_value():
        raise ValueError("required news duration must resolve to whole microseconds")
    try:
        return timedelta(microseconds=int(microseconds))
    except OverflowError as error:
        raise ValueError("required news duration is outside timedelta range") from error


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
