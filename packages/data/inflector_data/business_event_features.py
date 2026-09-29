"""PIT-safe, non-persisted materiality and recency primitives for one event."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_FACT_ORDER,
    BUSINESS_EVENT_QUANT_RULESET_CODE,
    BUSINESS_EVENT_QUANT_RULESET_VERSION,
    EVENT_FACT_COMPATIBILITY,
)
from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
)
from inflector_data.business_event_pit import PointInTimeBusinessEvent
from inflector_data.business_event_quantitative_pit import (
    BusinessEventQuantitativeFactView,
    PointInTimeBusinessEventQuantitativeDerivation,
)
from inflector_data.ttm import (
    TTM_ALGORITHM_VERSION,
    TrailingTwelveMonthNormalizer,
    TrailingTwelveMonthValue,
)

BUSINESS_EVENT_FEATURE_BUNDLE_VERSION = "business_event_feature_bundle_v1"
BUSINESS_EVENT_AGE_DAYS_VERSION = "business_event_age_days_v1"
ORDER_VALUE_TO_TTM_REVENUE_VERSION = "order_value_to_ttm_revenue_v1"
CAPEX_VALUE_TO_TTM_REVENUE_VERSION = "capex_value_to_ttm_revenue_v1"
CAPACITY_CHANGE_RATIO_VERSION = "capacity_change_ratio_v1"
ACQUISITION_CONSIDERATION_TO_TTM_REVENUE_VERSION = (
    "acquisition_consideration_to_ttm_revenue_v1"
)
ACQUISITION_STAKE_FRACTION_VERSION = "acquisition_stake_fraction_feature_v1"
QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION = (
    "quantitative_observation_resolution_v1"
)

_FEATURE_CODES = (
    "event_age_days",
    "order_value_to_ttm_revenue",
    "capex_value_to_ttm_revenue",
    "capacity_change_ratio",
    "acquisition_consideration_to_ttm_revenue",
    "acquisition_stake_fraction",
)

_EXPECTED_FACT_KIND = {
    "order_value": "monetary",
    "capex_value": "monetary",
    "capacity_before": "capacity",
    "capacity_after": "capacity",
    "additional_capacity": "capacity",
    "acquisition_consideration": "monetary",
    "acquisition_stake_fraction": "fraction",
    "commercial_commencement_date": "date",
}

_MISSING_FACT_WARNING = {
    "order_value": "missing_order_value",
    "capex_value": "missing_capex_value",
    "capacity_before": "missing_capacity_baseline",
    "capacity_after": "missing_capacity_after",
    "additional_capacity": "missing_additional_capacity",
    "acquisition_consideration": "missing_acquisition_consideration",
    "acquisition_stake_fraction": "missing_acquisition_stake",
    "commercial_commencement_date": "missing_commercial_commencement_date",
}


@dataclass(frozen=True, slots=True)
class ResolvedQuantitativeObservation:
    """One semantic value resolved without summing its source observations."""

    fact_code: str
    value: Decimal | None
    unit: str | None
    currency: str | None
    date_value: date | None
    facts: tuple[BusinessEventQuantitativeFactView, ...]
    warnings: tuple[str, ...]
    algorithm_version: str


type BusinessEventFeatureEvidence = (
    PointInTimeBusinessEvent
    | ResolvedQuantitativeObservation
    | TrailingTwelveMonthValue
)


@dataclass(frozen=True, slots=True)
class BusinessEventFeatureValue:
    """One auditable event primitive without score, direction, or confidence."""

    code: str
    value: Decimal | None
    unit: str
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str
    evidence: tuple[BusinessEventFeatureEvidence, ...]


@dataclass(frozen=True, slots=True)
class BusinessEventFeatureBundle:
    """Non-persisted primitives for exactly one already PIT-selected event."""

    company_id: UUID
    security_id: UUID | None
    provider_dataset_id: UUID
    announcement_id: UUID
    business_event_id: UUID
    event_type: str
    business_event_ruleset_code: str
    business_event_ruleset_semantic_version: str
    quantitative_ruleset_code: str | None
    quantitative_ruleset_semantic_version: str | None
    financial_provider_dataset_id: UUID
    filing_scope: str
    source_event_date: date | None
    source_available_at: datetime
    materiality_financial_cutoff: datetime
    as_of: datetime
    event_age_days: BusinessEventFeatureValue
    order_value_to_ttm_revenue: BusinessEventFeatureValue
    capex_value_to_ttm_revenue: BusinessEventFeatureValue
    capacity_change_ratio: BusinessEventFeatureValue
    acquisition_consideration_to_ttm_revenue: BusinessEventFeatureValue
    acquisition_stake_fraction: BusinessEventFeatureValue
    event_time_ttm_revenue: TrailingTwelveMonthValue | None
    algorithm_version: str


class BusinessEventFeaturePrimitives:
    """Build event-time materiality and request-time recency without persistence."""

    def __init__(self, ttm_normalizer: TrailingTwelveMonthNormalizer) -> None:
        self._ttm_normalizer = ttm_normalizer

    def features_as_of(
        self,
        *,
        event: PointInTimeBusinessEvent,
        quantitative_derivation: PointInTimeBusinessEventQuantitativeDerivation | None,
        financial_provider_dataset_id: UUID,
        filing_scope: str,
        as_of: datetime,
    ) -> BusinessEventFeatureBundle:
        cutoff = _aware_utc(as_of, field="as_of")
        source_cutoff = self._validate_event(event)
        if source_cutoff > cutoff:
            raise ValueError("event was not public at the requested as_of cutoff")
        if financial_provider_dataset_id.int == 0:
            raise ValueError("financial_provider_dataset_id must be non-empty")
        if not filing_scope.strip():
            raise ValueError("filing_scope must be non-empty")
        if quantitative_derivation is not None:
            self._validate_derivation(event, quantitative_derivation, source_cutoff)

        event_age = self._event_age(event, source_cutoff, cutoff)
        monetary_event = event.event_type in {
            "order_award",
            "capex_announcement",
            "acquisition_agreement",
        }
        ttm_revenue: TrailingTwelveMonthValue | None = None
        ttm_warning: str | None = None
        if monetary_event:
            ttm_revenue, ttm_warning = self._event_time_revenue(
                event=event,
                financial_provider_dataset_id=financial_provider_dataset_id,
                filing_scope=filing_scope,
                source_cutoff=source_cutoff,
            )

        order = self._not_applicable(
            "order_value_to_ttm_revenue",
            "ratio",
            ORDER_VALUE_TO_TTM_REVENUE_VERSION,
            event,
            cutoff,
        )
        capex = self._not_applicable(
            "capex_value_to_ttm_revenue",
            "ratio",
            CAPEX_VALUE_TO_TTM_REVENUE_VERSION,
            event,
            cutoff,
        )
        capacity = self._not_applicable(
            "capacity_change_ratio",
            "ratio",
            CAPACITY_CHANGE_RATIO_VERSION,
            event,
            cutoff,
        )
        acquisition_consideration = self._not_applicable(
            "acquisition_consideration_to_ttm_revenue",
            "ratio",
            ACQUISITION_CONSIDERATION_TO_TTM_REVENUE_VERSION,
            event,
            cutoff,
        )
        acquisition_stake = self._not_applicable(
            "acquisition_stake_fraction",
            "fraction",
            ACQUISITION_STAKE_FRACTION_VERSION,
            event,
            cutoff,
        )

        if event.event_type == "order_award":
            order = self._monetary_ratio(
                event=event,
                derivation=quantitative_derivation,
                fact_code="order_value",
                code="order_value_to_ttm_revenue",
                non_inr_warning="non_inr_order_value",
                algorithm_version=ORDER_VALUE_TO_TTM_REVENUE_VERSION,
                ttm_revenue=ttm_revenue,
                ttm_warning=ttm_warning,
                as_of=cutoff,
            )
        elif event.event_type == "capex_announcement":
            capex = self._monetary_ratio(
                event=event,
                derivation=quantitative_derivation,
                fact_code="capex_value",
                code="capex_value_to_ttm_revenue",
                non_inr_warning="non_inr_capex_value",
                algorithm_version=CAPEX_VALUE_TO_TTM_REVENUE_VERSION,
                ttm_revenue=ttm_revenue,
                ttm_warning=ttm_warning,
                as_of=cutoff,
            )
        elif event.event_type == "capacity_expansion":
            capacity = self._capacity_ratio(event, quantitative_derivation, cutoff)
        elif event.event_type == "acquisition_agreement":
            acquisition_consideration = self._monetary_ratio(
                event=event,
                derivation=quantitative_derivation,
                fact_code="acquisition_consideration",
                code="acquisition_consideration_to_ttm_revenue",
                non_inr_warning="non_inr_acquisition_consideration",
                algorithm_version=ACQUISITION_CONSIDERATION_TO_TTM_REVENUE_VERSION,
                ttm_revenue=ttm_revenue,
                ttm_warning=ttm_warning,
                as_of=cutoff,
            )
            acquisition_stake = self._stake(event, quantitative_derivation, cutoff)

        return BusinessEventFeatureBundle(
            company_id=event.company_id,
            security_id=event.security_id,
            provider_dataset_id=event.provider_dataset_id,
            announcement_id=event.announcement.id,
            business_event_id=event.id,
            event_type=event.event_type,
            business_event_ruleset_code=event.ruleset_code,
            business_event_ruleset_semantic_version=event.ruleset_semantic_version,
            quantitative_ruleset_code=(
                None if quantitative_derivation is None else quantitative_derivation.ruleset_code
            ),
            quantitative_ruleset_semantic_version=(
                None
                if quantitative_derivation is None
                else quantitative_derivation.ruleset_semantic_version
            ),
            financial_provider_dataset_id=financial_provider_dataset_id,
            filing_scope=filing_scope,
            source_event_date=event.source_event_date,
            source_available_at=source_cutoff,
            materiality_financial_cutoff=source_cutoff,
            as_of=cutoff,
            event_age_days=event_age,
            order_value_to_ttm_revenue=order,
            capex_value_to_ttm_revenue=capex,
            capacity_change_ratio=capacity,
            acquisition_consideration_to_ttm_revenue=acquisition_consideration,
            acquisition_stake_fraction=acquisition_stake,
            event_time_ttm_revenue=ttm_revenue,
            algorithm_version=BUSINESS_EVENT_FEATURE_BUNDLE_VERSION,
        )

    @staticmethod
    def _validate_event(event: PointInTimeBusinessEvent) -> datetime:
        source_cutoff = _aware_utc(event.source_available_at, field="event.source_available_at")
        if (
            event.ruleset_code != BUSINESS_EVENT_RULESET_CODE
            or event.ruleset_semantic_version != BUSINESS_EVENT_RULESET_VERSION
        ):
            raise ValueError("unsupported business event ruleset identity")
        if event.event_type not in EVENT_FACT_COMPATIBILITY:
            raise ValueError("unsupported business event type")
        if event.status != "detected":
            raise ValueError("business event must have detected status")
        if (
            event.announcement.id.int == 0
            or event.company_id.int == 0
            or event.provider_dataset_id.int == 0
            or event.announcement.company_id != event.company_id
            or event.announcement.security_id != event.security_id
            or event.announcement.provider_dataset_id != event.provider_dataset_id
            or _aware_utc(
                event.announcement.available_at,
                field="event.announcement.available_at",
            )
            != source_cutoff
        ):
            raise ValueError("business event identity is incoherent")
        return source_cutoff

    @staticmethod
    def _validate_derivation(
        event: PointInTimeBusinessEvent,
        derivation: PointInTimeBusinessEventQuantitativeDerivation,
        source_cutoff: datetime,
    ) -> None:
        if (
            derivation.event != event
            or derivation.event.id != event.id
            or derivation.event.detection_fingerprint_sha256
            != event.detection_fingerprint_sha256
            or _aware_utc(
                derivation.source_available_at,
                field="quantitative_derivation.source_available_at",
            )
            != source_cutoff
            or derivation.ruleset_code != BUSINESS_EVENT_QUANT_RULESET_CODE
            or derivation.ruleset_semantic_version != BUSINESS_EVENT_QUANT_RULESET_VERSION
        ):
            raise ValueError("quantitative derivation does not match the business event")
        evidence_ids = {value.id for value in event.evidence}
        for fact in derivation.facts:
            if (
                fact.business_event_evidence_id not in evidence_ids
                or fact.fact_code not in EVENT_FACT_COMPATIBILITY[event.event_type]
                or fact.fact_kind != _EXPECTED_FACT_KIND.get(fact.fact_code)
                or _aware_utc(fact.source_available_at, field="fact.source_available_at")
                != source_cutoff
            ):
                raise ValueError("quantitative fact is incoherent with event evidence")
        expected_codes = tuple(
            code
            for code in BUSINESS_EVENT_QUANT_FACT_ORDER
            if any(fact.fact_code == code for fact in derivation.facts)
        )
        if derivation.available_fact_codes != expected_codes:
            raise ValueError("quantitative derivation fact codes are not canonical")

    def _event_time_revenue(
        self,
        *,
        event: PointInTimeBusinessEvent,
        financial_provider_dataset_id: UUID,
        filing_scope: str,
        source_cutoff: datetime,
    ) -> tuple[TrailingTwelveMonthValue | None, str | None]:
        values = self._ttm_normalizer.ttm_series_as_of(
            provider_dataset_id=financial_provider_dataset_id,
            company_id=event.company_id,
            filing_scope=filing_scope,
            metric_code="revenue",
            as_of=source_cutoff,
        )
        if not values:
            return None, "missing_event_time_ttm_revenue"
        latest_end = max(value.period_end for value in values)
        latest = tuple(value for value in values if value.period_end == latest_end)
        if len(latest) != 1:
            return None, "ambiguous_latest_event_time_ttm_revenue"
        selected = latest[0]
        if (
            selected.provider_dataset_id != financial_provider_dataset_id
            or selected.company_id != event.company_id
            or selected.filing_scope != filing_scope
            or selected.metric_code != "revenue"
            or selected.unit != "INR"
            or _aware_utc(selected.as_of, field="ttm_revenue.as_of") != source_cutoff
            or _aware_utc(selected.available_at, field="ttm_revenue.available_at")
            > source_cutoff
            or selected.algorithm_version != TTM_ALGORITHM_VERSION
        ):
            raise ValueError("event-time TTM revenue evidence is incoherent")
        if selected.value <= 0:
            return selected, "non_positive_event_time_ttm_revenue"
        return selected, None

    @staticmethod
    def _event_age(
        event: PointInTimeBusinessEvent,
        source_cutoff: datetime,
        as_of: datetime,
    ) -> BusinessEventFeatureValue:
        elapsed = as_of - source_cutoff
        value = (
            Decimal(elapsed.days)
            + Decimal(elapsed.seconds) / Decimal("86400")
            + Decimal(elapsed.microseconds) / Decimal("86400000000")
        )
        return BusinessEventFeatureValue(
            code="event_age_days",
            value=value,
            unit="days",
            warnings=(),
            as_of=as_of,
            available_at=source_cutoff,
            algorithm_version=BUSINESS_EVENT_AGE_DAYS_VERSION,
            evidence=(event,),
        )

    def _monetary_ratio(
        self,
        *,
        event: PointInTimeBusinessEvent,
        derivation: PointInTimeBusinessEventQuantitativeDerivation | None,
        fact_code: str,
        code: str,
        non_inr_warning: str,
        algorithm_version: str,
        ttm_revenue: TrailingTwelveMonthValue | None,
        ttm_warning: str | None,
        as_of: datetime,
    ) -> BusinessEventFeatureValue:
        observation = self._resolve(derivation, fact_code)
        evidence: tuple[BusinessEventFeatureEvidence, ...] = (observation,)
        if ttm_revenue is not None:
            evidence += (ttm_revenue,)
        warning = observation.warnings[0] if observation.warnings else None
        if warning is None and (
            observation.currency != "INR" or observation.unit != "currency_major"
        ):
            warning = non_inr_warning
        if warning is None and observation.value is not None and observation.value < 0:
            raise ValueError("monetary event observation must be non-negative")
        if warning is None:
            warning = ttm_warning
        value = (
            observation.value / ttm_revenue.value
            if warning is None
            and observation.value is not None
            and ttm_revenue is not None
            else None
        )
        available_at = _evidence_available_at(event, observation, ttm_revenue)
        return BusinessEventFeatureValue(
            code=code,
            value=value,
            unit="ratio",
            warnings=() if warning is None else (warning,),
            as_of=as_of,
            available_at=available_at,
            algorithm_version=algorithm_version,
            evidence=evidence,
        )

    def _capacity_ratio(
        self,
        event: PointInTimeBusinessEvent,
        derivation: PointInTimeBusinessEventQuantitativeDerivation | None,
        as_of: datetime,
    ) -> BusinessEventFeatureValue:
        before = self._resolve(derivation, "capacity_before")
        after = self._resolve(derivation, "capacity_after")
        additional = self._resolve(derivation, "additional_capacity")
        evidence: tuple[BusinessEventFeatureEvidence, ...] = (before, after, additional)
        if before.warnings:
            warning = before.warnings[0]
            value = None
        elif before.value is None or before.value <= 0:
            warning = "non_positive_capacity_baseline"
            value = None
        else:
            calculations: list[Decimal] = []
            warning = None
            for observation in (after, additional):
                if observation.warnings == ("conflicting_quantitative_observations",):
                    warning = observation.warnings[0]
                    break
            if warning is None and not after.warnings and after.value is not None:
                if after.unit != before.unit:
                    warning = "incompatible_capacity_units"
                elif after.value <= before.value:
                    warning = "non_expanding_capacity_pair"
                else:
                    calculations.append(after.value / before.value - Decimal("1"))
            if warning is None and not additional.warnings and additional.value is not None:
                if additional.unit != before.unit:
                    warning = "incompatible_capacity_units"
                elif additional.value <= 0:
                    warning = "non_positive_additional_capacity"
                else:
                    calculations.append(additional.value / before.value)
            if warning is None and not calculations:
                warning = "missing_capacity_change"
            if warning is None and len(set(calculations)) > 1:
                warning = "conflicting_capacity_change_calculations"
            value = calculations[0] if warning is None else None
        return BusinessEventFeatureValue(
            code="capacity_change_ratio",
            value=value,
            unit="ratio",
            warnings=() if warning is None else (warning,),
            as_of=as_of,
            available_at=_evidence_available_at(event, before, after, additional),
            algorithm_version=CAPACITY_CHANGE_RATIO_VERSION,
            evidence=evidence,
        )

    def _stake(
        self,
        event: PointInTimeBusinessEvent,
        derivation: PointInTimeBusinessEventQuantitativeDerivation | None,
        as_of: datetime,
    ) -> BusinessEventFeatureValue:
        observation = self._resolve(derivation, "acquisition_stake_fraction")
        warning = observation.warnings[0] if observation.warnings else None
        if warning is None and (
            observation.unit != "fraction"
            or observation.value is None
            or not Decimal("0") < observation.value <= Decimal("1")
        ):
            raise ValueError("acquisition stake observation is outside its semantic domain")
        return BusinessEventFeatureValue(
            code="acquisition_stake_fraction",
            value=observation.value if warning is None else None,
            unit="fraction",
            warnings=() if warning is None else (warning,),
            as_of=as_of,
            available_at=_evidence_available_at(event, observation),
            algorithm_version=ACQUISITION_STAKE_FRACTION_VERSION,
            evidence=(observation,),
        )

    @staticmethod
    def _resolve(
        derivation: PointInTimeBusinessEventQuantitativeDerivation | None,
        fact_code: str,
    ) -> ResolvedQuantitativeObservation:
        if fact_code not in _EXPECTED_FACT_KIND:
            raise ValueError(f"unsupported quantitative fact code: {fact_code}")
        if derivation is None:
            return ResolvedQuantitativeObservation(
                fact_code=fact_code,
                value=None,
                unit=None,
                currency=None,
                date_value=None,
                facts=(),
                warnings=("quantitative_derivation_unavailable",),
                algorithm_version=QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
            )
        facts = tuple(fact for fact in derivation.facts if fact.fact_code == fact_code)
        if not facts:
            return ResolvedQuantitativeObservation(
                fact_code=fact_code,
                value=None,
                unit=None,
                currency=None,
                date_value=None,
                facts=(),
                warnings=(_MISSING_FACT_WARNING[fact_code],),
                algorithm_version=QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
            )
        semantic = {_semantic_key(fact) for fact in facts}
        if len(semantic) != 1:
            return ResolvedQuantitativeObservation(
                fact_code=fact_code,
                value=None,
                unit=None,
                currency=None,
                date_value=None,
                facts=facts,
                warnings=("conflicting_quantitative_observations",),
                algorithm_version=QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
            )
        first = facts[0]
        return ResolvedQuantitativeObservation(
            fact_code=fact_code,
            value=first.normalized_value,
            unit=first.normalized_unit,
            currency=first.reported_currency,
            date_value=first.date_value,
            facts=facts,
            warnings=(),
            algorithm_version=QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
        )

    @staticmethod
    def _not_applicable(
        code: str,
        unit: str,
        algorithm_version: str,
        event: PointInTimeBusinessEvent,
        as_of: datetime,
    ) -> BusinessEventFeatureValue:
        if code not in _FEATURE_CODES:
            raise ValueError(f"unknown business event feature code: {code}")
        return BusinessEventFeatureValue(
            code=code,
            value=None,
            unit=unit,
            warnings=("not_applicable_for_event_type",),
            as_of=as_of,
            available_at=None,
            algorithm_version=algorithm_version,
            evidence=(event,),
        )


def _semantic_key(fact: BusinessEventQuantitativeFactView) -> tuple[object, ...]:
    expected_kind = _EXPECTED_FACT_KIND.get(fact.fact_code)
    if fact.fact_kind != expected_kind:
        raise ValueError("quantitative fact kind does not match its code")
    if fact.fact_kind == "monetary":
        if (
            fact.reported_currency not in {"INR", "USD", "EUR"}
            or fact.normalized_unit != "currency_major"
            or fact.normalized_value is None
        ):
            raise ValueError("monetary observation is semantically incoherent")
        return (fact.reported_currency, fact.normalized_unit, fact.normalized_value)
    if fact.fact_kind in {"capacity", "fraction"}:
        if fact.normalized_unit is None or fact.normalized_value is None:
            raise ValueError("normalized quantitative observation is incomplete")
        return (fact.normalized_unit, fact.normalized_value)
    if fact.fact_kind == "date":
        if fact.date_value is None:
            raise ValueError("date observation is incomplete")
        return (fact.date_value,)
    raise ValueError("unsupported quantitative fact kind")


def _evidence_available_at(
    event: PointInTimeBusinessEvent,
    *evidence: ResolvedQuantitativeObservation | TrailingTwelveMonthValue | None,
) -> datetime | None:
    if not any(value is not None for value in evidence):
        return None
    timestamps = [_aware_utc(event.source_available_at, field="event.source_available_at")]
    timestamps.extend(
        _aware_utc(value.available_at, field="evidence.available_at")
        for value in evidence
        if isinstance(value, TrailingTwelveMonthValue)
    )
    return max(timestamps)


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
