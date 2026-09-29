"""Pure Phase 4D-H Business Catalyst component scoring primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
    BUSINESS_EVENT_TYPE_ORDER,
)
from inflector_core.component_scoring import (
    PIECEWISE_LINEAR_CURVE_VERSION,
    score_piecewise_linear,
)
from inflector_core.scoring_policy import InflectionScoringPolicy

if TYPE_CHECKING:
    from inflector_data.business_event_features import (
        BusinessEventFeatureBundle,
        BusinessEventFeatureValue,
    )

BUSINESS_CATALYST_COMPONENT_VERSION = "business_catalyst_component_v1"
BUSINESS_CATALYST_EVENT_SCORE_VERSION = "business_catalyst_event_score_v1"
BUSINESS_CATALYST_AGGREGATION_VERSION = "max_event_score_v1"

BUSINESS_CATALYST_V1_SCOREABLE_EVENT_TYPES = (
    "order_award",
    "capacity_expansion",
    "commercial_commencement",
    "regulatory_approval",
)

_BUSINESS_EVENT_FEATURE_BUNDLE_VERSION = "business_event_feature_bundle_v1"
_BUSINESS_EVENT_AGE_DAYS_VERSION = "business_event_age_days_v1"
_BUSINESS_EVENT_QUANT_RULESET_CODE = "business_event_quantitative_rules"
_BUSINESS_EVENT_QUANT_RULESET_VERSION = "business_event_quantitative_rules_v1"
_FEATURE_SPECS = {
    "order_value_to_ttm_revenue": ("order_value_to_ttm_revenue_v1", "ratio"),
    "capex_value_to_ttm_revenue": ("capex_value_to_ttm_revenue_v1", "ratio"),
    "capacity_change_ratio": ("capacity_change_ratio_v1", "ratio"),
    "acquisition_consideration_to_ttm_revenue": (
        "acquisition_consideration_to_ttm_revenue_v1",
        "ratio",
    ),
    "acquisition_stake_fraction": (
        "acquisition_stake_fraction_feature_v1",
        "fraction",
    ),
}
_APPLICABLE_FEATURES = {
    "order_award": frozenset({"order_value_to_ttm_revenue"}),
    "capacity_expansion": frozenset({"capacity_change_ratio"}),
    "commercial_commencement": frozenset(),
    "capex_announcement": frozenset({"capex_value_to_ttm_revenue"}),
    "acquisition_agreement": frozenset(
        {
            "acquisition_consideration_to_ttm_revenue",
            "acquisition_stake_fraction",
        }
    ),
    "regulatory_approval": frozenset(),
}
_EVENT_TYPE_RANK = {
    event_type: rank for rank, event_type in enumerate(BUSINESS_EVENT_TYPE_ORDER)
}


@dataclass(frozen=True, slots=True)
class BusinessCatalystEventScore:
    """One event's auditable strength/recency calculation."""

    business_event_id: UUID
    announcement_id: UUID
    event_type: str
    source_available_at: datetime
    score: Decimal | None
    unit: str
    strength_score: Decimal | None
    strength_code: str | None
    strength_raw_value: Decimal | None
    strength_raw_unit: str | None
    strength_transform_code: str | None
    strength_curve_algorithm_version: str | None
    event_age_days: Decimal
    recency_scoring_value: Decimal | None
    recency_transform_code: str | None
    recency_score: Decimal | None
    recency_curve_algorithm_version: str | None
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    evidence: BusinessEventFeatureBundle
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class BusinessCatalystComponentScore:
    """Maximum score across one explicit coherent event set."""

    company_id: UUID
    security_id: UUID | None
    event_provider_dataset_id: UUID
    financial_provider_dataset_id: UUID
    filing_scope: str
    score: Decimal | None
    unit: str
    selected_event_id: UUID | None
    selected_event_type: str | None
    aggregation_method: str
    event_scores: tuple[BusinessCatalystEventScore, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str


class BusinessCatalystComponentScorer:
    """Score explicit Phase 6C-C event bundles without reads or recomputation."""

    def score(
        self,
        *,
        evidence: tuple[BusinessEventFeatureBundle, ...],
        policy: InflectionScoringPolicy,
    ) -> BusinessCatalystComponentScore:
        if not evidence:
            raise ValueError("business catalyst evidence must not be empty")
        scoring = policy.business_catalyst
        if scoring is None:
            raise ValueError("business catalyst scoring policy is not configured")
        if scoring.aggregation_method != BUSINESS_CATALYST_AGGREGATION_VERSION:
            raise ValueError("unsupported business catalyst aggregation method")

        first = evidence[0]
        cutoff = _aware_utc(first.as_of, "bundle as_of")
        company_id = first.company_id
        event_provider_dataset_id = first.provider_dataset_id
        financial_provider_dataset_id = first.financial_provider_dataset_id
        filing_scope = first.filing_scope
        if not filing_scope.strip():
            raise ValueError("filing_scope must be non-empty")

        event_ids: set[UUID] = set()
        security_ids: set[UUID] = set()
        event_scores: list[BusinessCatalystEventScore] = []
        for bundle in evidence:
            if bundle.business_event_id in event_ids:
                raise ValueError("business_event_id must be unique across evidence")
            event_ids.add(bundle.business_event_id)
            if bundle.security_id is not None:
                security_ids.add(bundle.security_id)
            if len(security_ids) > 1:
                raise ValueError("business catalyst evidence has mixed security identities")
            if bundle.company_id != company_id:
                raise ValueError("business catalyst evidence has mixed companies")
            if bundle.provider_dataset_id != event_provider_dataset_id:
                raise ValueError("business catalyst evidence has mixed event providers")
            if bundle.financial_provider_dataset_id != financial_provider_dataset_id:
                raise ValueError("business catalyst evidence has mixed financial providers")
            if bundle.filing_scope != filing_scope:
                raise ValueError("business catalyst evidence has mixed filing scopes")
            if _aware_utc(bundle.as_of, "bundle as_of") != cutoff:
                raise ValueError("business catalyst evidence has mixed as_of cutoffs")
            self._validate_bundle(bundle, cutoff)
            event_scores.append(self._score_event(bundle, scoring, cutoff))

        ordered_scores = tuple(
            sorted(
                event_scores,
                key=lambda item: (
                    _EVENT_TYPE_RANK[item.event_type],
                    item.source_available_at,
                    str(item.business_event_id),
                ),
            )
        )
        selected = self._select_maximum(ordered_scores)
        return BusinessCatalystComponentScore(
            company_id=company_id,
            security_id=next(iter(security_ids), None),
            event_provider_dataset_id=event_provider_dataset_id,
            financial_provider_dataset_id=financial_provider_dataset_id,
            filing_scope=filing_scope,
            score=selected.score if selected is not None else None,
            unit="score_0_100",
            selected_event_id=selected.business_event_id if selected is not None else None,
            selected_event_type=selected.event_type if selected is not None else None,
            aggregation_method=BUSINESS_CATALYST_AGGREGATION_VERSION,
            event_scores=ordered_scores,
            warnings=() if selected is not None else ("no_scoreable_business_catalyst_event",),
            as_of=cutoff,
            available_at=max(item.source_available_at for item in ordered_scores),
            algorithm_version=BUSINESS_CATALYST_COMPONENT_VERSION,
        )

    @staticmethod
    def _select_maximum(
        event_scores: tuple[BusinessCatalystEventScore, ...],
    ) -> BusinessCatalystEventScore | None:
        candidates = tuple(item for item in event_scores if item.score is not None)
        if not candidates:
            return None
        maximum = max(item.score for item in candidates if item.score is not None)
        tied = tuple(item for item in candidates if item.score == maximum)
        newest = max(item.source_available_at for item in tied)
        newest_tied = tuple(item for item in tied if item.source_available_at == newest)
        return min(
            newest_tied,
            key=lambda item: (_EVENT_TYPE_RANK[item.event_type], str(item.business_event_id)),
        )

    @staticmethod
    def _score_event(
        bundle: BusinessEventFeatureBundle,
        scoring: object,
        cutoff: datetime,
    ) -> BusinessCatalystEventScore:
        maximum_age = Decimal(getattr(scoring, "maximum_event_age_days"))
        age = bundle.event_age_days.value
        assert age is not None
        source_available_at = _aware_utc(bundle.source_available_at, "source_available_at")
        warnings: list[str] = []
        recency_scoring_value: Decimal | None = None
        recency_score: Decimal | None = None
        recency_transform_code: str | None = None
        recency_curve_version: str | None = None

        if age > maximum_age:
            warnings.append("outside_business_catalyst_window")
        else:
            recency_scoring_value = -age
            recency_transform_code = "negate_event_age_days"
            recency_score = score_piecewise_linear(
                recency_scoring_value,
                getattr(scoring, "event_recency_signal_curve"),
            )
            recency_curve_version = PIECEWISE_LINEAR_CURVE_VERSION

        strength_score: Decimal | None = None
        strength_code: str | None = None
        strength_raw_value: Decimal | None = None
        strength_raw_unit: str | None = None
        strength_transform_code: str | None = None
        strength_curve_version: str | None = None

        if bundle.event_type not in BUSINESS_CATALYST_V1_SCOREABLE_EVENT_TYPES:
            warnings.append("event_type_not_scoreable_in_business_catalyst_v1")
        elif age <= maximum_age:
            if bundle.event_type == "order_award":
                feature = bundle.order_value_to_ttm_revenue
                strength_code = "order_value_to_ttm_revenue"
                strength_score, strength_raw_value = _curve_strength(
                    feature,
                    getattr(scoring, "order_value_to_ttm_revenue_curve"),
                )
                strength_raw_unit = "ratio"
                strength_transform_code = "identity"
                strength_curve_version = PIECEWISE_LINEAR_CURVE_VERSION
                if strength_score is None:
                    warnings.extend(feature.warnings)
                    warnings.append("event_strength_unavailable")
            elif bundle.event_type == "capacity_expansion":
                feature = bundle.capacity_change_ratio
                strength_code = "capacity_change_ratio"
                strength_score, strength_raw_value = _curve_strength(
                    feature,
                    getattr(scoring, "capacity_change_ratio_curve"),
                )
                strength_raw_unit = "ratio"
                strength_transform_code = "identity"
                strength_curve_version = PIECEWISE_LINEAR_CURVE_VERSION
                if strength_score is None:
                    warnings.extend(feature.warnings)
                    warnings.append("event_strength_unavailable")
            elif bundle.event_type == "commercial_commencement":
                strength_code = "commercial_commencement_base_score"
                strength_raw_value = getattr(scoring, "commercial_commencement_base_score")
                strength_raw_unit = "score_0_100"
                strength_transform_code = "identity"
                strength_score = strength_raw_value
            else:
                strength_code = "regulatory_approval_base_score"
                strength_raw_value = getattr(scoring, "regulatory_approval_base_score")
                strength_raw_unit = "score_0_100"
                strength_transform_code = "identity"
                strength_score = strength_raw_value

        event_score = (
            strength_score * recency_score / Decimal("100")
            if strength_score is not None and recency_score is not None
            else None
        )
        if event_score is not None and not Decimal("0") <= event_score <= Decimal("100"):
            raise AssertionError("business catalyst event score escaped the 0-100 range")
        return BusinessCatalystEventScore(
            business_event_id=bundle.business_event_id,
            announcement_id=bundle.announcement_id,
            event_type=bundle.event_type,
            source_available_at=source_available_at,
            score=event_score,
            unit="score_0_100",
            strength_score=strength_score,
            strength_code=strength_code,
            strength_raw_value=strength_raw_value,
            strength_raw_unit=strength_raw_unit,
            strength_transform_code=strength_transform_code,
            strength_curve_algorithm_version=strength_curve_version,
            event_age_days=age,
            recency_scoring_value=recency_scoring_value,
            recency_transform_code=recency_transform_code,
            recency_score=recency_score,
            recency_curve_algorithm_version=recency_curve_version,
            warnings=_unique(warnings),
            as_of=cutoff,
            available_at=source_available_at,
            evidence=bundle,
            algorithm_version=BUSINESS_CATALYST_EVENT_SCORE_VERSION,
        )

    @staticmethod
    def _validate_bundle(bundle: BusinessEventFeatureBundle, cutoff: datetime) -> None:
        if bundle.algorithm_version != _BUSINESS_EVENT_FEATURE_BUNDLE_VERSION:
            raise ValueError("unsupported business event feature bundle version")
        if bundle.event_type not in _APPLICABLE_FEATURES:
            raise ValueError("unsupported business event type")
        if (
            bundle.business_event_ruleset_code != BUSINESS_EVENT_RULESET_CODE
            or bundle.business_event_ruleset_semantic_version
            != BUSINESS_EVENT_RULESET_VERSION
        ):
            raise ValueError("unsupported business event ruleset identity")
        quant_identity = (
            bundle.quantitative_ruleset_code,
            bundle.quantitative_ruleset_semantic_version,
        )
        if quant_identity not in {
            (None, None),
            (_BUSINESS_EVENT_QUANT_RULESET_CODE, _BUSINESS_EVENT_QUANT_RULESET_VERSION),
        }:
            raise ValueError("unsupported quantitative ruleset identity")
        source_available_at = _aware_utc(bundle.source_available_at, "source_available_at")
        if source_available_at > cutoff:
            raise ValueError("business event was not public at the scorer cutoff")
        if (
            _aware_utc(
                bundle.materiality_financial_cutoff,
                "materiality_financial_cutoff",
            )
            != source_available_at
        ):
            raise ValueError("materiality cutoff must equal event source_available_at")

        age = bundle.event_age_days
        if (
            age.code != "event_age_days"
            or age.algorithm_version != _BUSINESS_EVENT_AGE_DAYS_VERSION
            or age.unit != "days"
            or not isinstance(age.value, Decimal)
            or age.value < 0
            or age.warnings
            or age.available_at is None
            or _aware_utc(age.available_at, "event_age_days available_at")
            != source_available_at
            or _aware_utc(age.as_of, "event_age_days as_of") != cutoff
        ):
            raise ValueError("event_age_days feature is incoherent")
        _validate_event_age_evidence(bundle)

        applicable = _APPLICABLE_FEATURES[bundle.event_type]
        for code, (version, unit) in _FEATURE_SPECS.items():
            feature = getattr(bundle, code)
            _validate_feature(
                feature,
                code=code,
                algorithm_version=version,
                unit=unit,
                applicable=code in applicable,
                source_available_at=source_available_at,
                cutoff=cutoff,
            )


def _curve_strength(
    feature: BusinessEventFeatureValue,
    curve: object,
) -> tuple[Decimal | None, Decimal | None]:
    if feature.value is None:
        return None, None
    return score_piecewise_linear(feature.value, curve), feature.value  # type: ignore[arg-type]


def _validate_event_age_evidence(bundle: BusinessEventFeatureBundle) -> None:
    evidence = bundle.event_age_days.evidence
    if len(evidence) != 1:
        raise ValueError("event_age_days must retain exactly one event evidence object")
    event = evidence[0]
    announcement = getattr(event, "announcement", None)
    if (
        getattr(event, "id", None) != bundle.business_event_id
        or getattr(event, "company_id", None) != bundle.company_id
        or getattr(event, "security_id", None) != bundle.security_id
        or getattr(event, "provider_dataset_id", None) != bundle.provider_dataset_id
        or getattr(event, "event_type", None) != bundle.event_type
        or getattr(event, "ruleset_code", None) != bundle.business_event_ruleset_code
        or getattr(event, "ruleset_semantic_version", None)
        != bundle.business_event_ruleset_semantic_version
        or getattr(announcement, "id", None) != bundle.announcement_id
        or _aware_utc(
            getattr(event, "source_available_at", None),
            "event evidence source_available_at",
        )
        != _aware_utc(bundle.source_available_at, "source_available_at")
    ):
        raise ValueError("event_age_days evidence does not match the feature bundle")


def _validate_feature(
    feature: BusinessEventFeatureValue,
    *,
    code: str,
    algorithm_version: str,
    unit: str,
    applicable: bool,
    source_available_at: datetime,
    cutoff: datetime,
) -> None:
    if (
        feature.code != code
        or feature.algorithm_version != algorithm_version
        or feature.unit != unit
        or _aware_utc(feature.as_of, f"{code} as_of") != cutoff
    ):
        raise ValueError(f"{code} feature identity is incoherent")
    if not applicable:
        if (
            feature.value is not None
            or feature.warnings != ("not_applicable_for_event_type",)
            or feature.available_at is not None
        ):
            raise ValueError(f"{code} must be not-applicable for this event type")
        return
    if feature.value is None:
        if not feature.warnings:
            raise ValueError(f"unavailable {code} must retain warnings")
    else:
        if not isinstance(feature.value, Decimal) or feature.warnings:
            raise ValueError(f"available {code} must be a warning-free Decimal")
        if code == "capacity_change_ratio":
            if feature.value <= 0:
                raise ValueError("capacity_change_ratio must be positive")
        elif code == "acquisition_stake_fraction":
            if not Decimal("0") < feature.value <= Decimal("1"):
                raise ValueError("acquisition_stake_fraction must be in (0, 1]")
        elif feature.value < 0:
            raise ValueError(f"{code} must not be negative")
    if feature.available_at is not None:
        if _aware_utc(feature.available_at, f"{code} available_at") > source_available_at:
            raise ValueError(f"{code} uses evidence unavailable at event time")
    elif feature.value is not None:
        raise ValueError(f"available {code} must have available_at")


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
