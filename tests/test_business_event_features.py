"""Phase 6C-C non-persisted business-event feature acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import cast
from uuid import uuid4

import pytest

from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_RULESET_CODE,
    BUSINESS_EVENT_QUANT_RULESET_VERSION,
)
from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
)
from inflector_data.announcement_pit import PointInTimeAnnouncement
from inflector_data.business_event_features import (
    ACQUISITION_CONSIDERATION_TO_TTM_REVENUE_VERSION,
    ACQUISITION_STAKE_FRACTION_VERSION,
    BUSINESS_EVENT_AGE_DAYS_VERSION,
    BUSINESS_EVENT_FEATURE_BUNDLE_VERSION,
    CAPACITY_CHANGE_RATIO_VERSION,
    CAPEX_VALUE_TO_TTM_REVENUE_VERSION,
    ORDER_VALUE_TO_TTM_REVENUE_VERSION,
    QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
    BusinessEventFeaturePrimitives,
    ResolvedQuantitativeObservation,
)
from inflector_data.business_event_pit import (
    BusinessEventEvidenceView,
    PointInTimeBusinessEvent,
)
from inflector_data.business_event_quantitative_pit import (
    BusinessEventQuantitativeFactView,
    PointInTimeBusinessEventQuantitativeDerivation,
)
from inflector_data.pit import SourceRecordView
from inflector_data.ttm import (
    TTM_ALGORITHM_VERSION,
    TrailingTwelveMonthNormalizer,
    TrailingTwelveMonthValue,
)

SOURCE_TIME = datetime(2026, 9, 1, 12, tzinfo=UTC)
REQUEST_TIME = datetime(2026, 9, 21, 12, tzinfo=UTC)
FINANCIAL_PROVIDER = uuid4()


class _StubTTMNormalizer:
    def __init__(self, values: tuple[TrailingTwelveMonthValue, ...]) -> None:
        self.values = values
        self.calls: list[dict[str, object]] = []

    def ttm_series_as_of(self, **kwargs: object) -> list[TrailingTwelveMonthValue]:
        self.calls.append(kwargs)
        return list(self.values)


def _source() -> SourceRecordView:
    return SourceRecordView(
        id=uuid4(),
        external_record_id="FEATURE-EVENT",
        source_uri="synthetic://feature-event",
        raw_object_key="sha256/aa/source",
        raw_payload_reference="record:FEATURE-EVENT",
        content_sha256="a" * 64,
        validation_status="accepted",
    )


def _event(
    event_type: str = "order_award", *, source_time: datetime = SOURCE_TIME
) -> PointInTimeBusinessEvent:
    company_id = uuid4()
    provider_dataset_id = uuid4()
    announcement = PointInTimeAnnouncement(
        id=uuid4(),
        company_id=company_id,
        security_id=None,
        provider_dataset_id=provider_dataset_id,
        external_record_id="FEATURE-EVENT",
        provider_category=None,
        headline="Fictional deterministic event evidence",
        announcement_date=date(2026, 9, 1),
        exchange=None,
        available_at=source_time,
        revision_at=None,
        ingested_at=source_time + timedelta(hours=1),
        source_record=_source(),
        documents=(),
    )
    evidence = BusinessEventEvidenceView(
        id=uuid4(),
        evidence_kind="announcement_headline",
        document=None,
        document_asset=None,
        text_extraction=None,
        rule_code="synthetic_rule_v1",
        rule_semantic_version="business_event_rule_v1",
        start_offset=0,
        end_offset=len(announcement.headline),
        page_numbers=(),
        page_text_sha256s=(),
        excerpt_text=announcement.headline,
        excerpt_sha256=sha256(announcement.headline.encode()).hexdigest(),
        source_available_at=source_time,
        evidence_fingerprint_sha256="b" * 64,
    )
    return PointInTimeBusinessEvent(
        id=uuid4(),
        announcement=announcement,
        company_id=company_id,
        security_id=None,
        provider_dataset_id=provider_dataset_id,
        event_type=event_type,
        source_event_date=announcement.announcement_date,
        source_available_at=source_time,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
        matched_rule_codes=("synthetic_rule_v1",),
        status="detected",
        warnings=(),
        detection_fingerprint_sha256="c" * 64,
        derived_at=source_time + timedelta(days=3),
        evidence=(evidence,),
    )


def _fact(
    event: PointInTimeBusinessEvent,
    *,
    fact_code: str,
    fact_kind: str,
    normalized_value: Decimal | None,
    normalized_unit: str | None,
    currency: str | None = None,
    date_value: date | None = None,
    raw_text: str = "source observation",
) -> BusinessEventQuantitativeFactView:
    return BusinessEventQuantitativeFactView(
        id=uuid4(),
        business_event_evidence_id=event.evidence[0].id,
        fact_code=fact_code,
        fact_kind=fact_kind,
        rule_code=f"{fact_code}_rule_v1",
        rule_semantic_version="business_event_quantitative_rule_v1",
        start_offset=0,
        end_offset=len(raw_text),
        raw_text=raw_text,
        raw_text_sha256=sha256(raw_text.encode()).hexdigest(),
        reported_value=normalized_value,
        reported_scale=None,
        reported_unit=("percent" if fact_kind == "fraction" else normalized_unit),
        reported_currency=currency,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        date_value=date_value,
        source_available_at=event.source_available_at,
        warnings=(),
        fact_fingerprint_sha256=uuid4().hex * 2,
    )


def _derivation(
    event: PointInTimeBusinessEvent,
    facts: tuple[BusinessEventQuantitativeFactView, ...],
) -> PointInTimeBusinessEventQuantitativeDerivation:
    order = (
        "order_value",
        "capex_value",
        "capacity_before",
        "capacity_after",
        "additional_capacity",
        "acquisition_consideration",
        "acquisition_stake_fraction",
        "commercial_commencement_date",
    )
    return PointInTimeBusinessEventQuantitativeDerivation(
        id=uuid4(),
        event=event,
        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
        source_available_at=event.source_available_at,
        available_fact_codes=tuple(
            code for code in order if any(fact.fact_code == code for fact in facts)
        ),
        warnings=(),
        derivation_fingerprint_sha256="d" * 64,
        derived_at=event.source_available_at + timedelta(days=4),
        facts=facts,
    )


def _ttm(
    event: PointInTimeBusinessEvent,
    value: Decimal = Decimal("10000000000"),
    *,
    period_end: date = date(2026, 6, 30),
    available_at: datetime | None = None,
) -> TrailingTwelveMonthValue:
    actual_available_at = event.source_available_at - timedelta(days=1)
    if available_at is not None:
        actual_available_at = available_at
    return TrailingTwelveMonthValue(
        provider_dataset_id=FINANCIAL_PROVIDER,
        company_id=event.company_id,
        filing_scope="standalone",
        metric_code="revenue",
        ending_fiscal_year=2026,
        ending_fiscal_quarter=1,
        period_start=date(2025, 7, 1),
        period_end=period_end,
        value=value,
        unit="INR",
        construction_kind="four_quarters",
        operation="sum_four_quarters",
        algorithm_version=TTM_ALGORITHM_VERSION,
        as_of=event.source_available_at,
        available_at=actual_available_at,
        lineage=(),
    )


def _features(
    event: PointInTimeBusinessEvent,
    derivation: PointInTimeBusinessEventQuantitativeDerivation | None,
    values: tuple[TrailingTwelveMonthValue, ...] = (),
    *,
    as_of: datetime = REQUEST_TIME,
):
    stub = _StubTTMNormalizer(values)
    primitives = BusinessEventFeaturePrimitives(
        cast(TrailingTwelveMonthNormalizer, stub)
    )
    result = primitives.features_as_of(
        event=event,
        quantitative_derivation=derivation,
        financial_provider_dataset_id=FINANCIAL_PROVIDER,
        filing_scope="standalone",
        as_of=as_of,
    )
    return result, stub


def test_versions_event_age_and_distinct_cutoffs_are_exact() -> None:
    event = _event()
    result, stub = _features(event, _derivation(event, ()), (_ttm(event),))

    assert result.algorithm_version == BUSINESS_EVENT_FEATURE_BUNDLE_VERSION
    assert result.event_age_days.algorithm_version == BUSINESS_EVENT_AGE_DAYS_VERSION
    assert result.order_value_to_ttm_revenue.algorithm_version == (
        ORDER_VALUE_TO_TTM_REVENUE_VERSION
    )
    assert result.capex_value_to_ttm_revenue.algorithm_version == (
        CAPEX_VALUE_TO_TTM_REVENUE_VERSION
    )
    assert result.capacity_change_ratio.algorithm_version == CAPACITY_CHANGE_RATIO_VERSION
    assert result.acquisition_consideration_to_ttm_revenue.algorithm_version == (
        ACQUISITION_CONSIDERATION_TO_TTM_REVENUE_VERSION
    )
    assert result.acquisition_stake_fraction.algorithm_version == (
        ACQUISITION_STAKE_FRACTION_VERSION
    )
    assert result.event_age_days.value == Decimal("20")
    assert result.materiality_financial_cutoff == SOURCE_TIME
    assert result.as_of == REQUEST_TIME
    assert stub.calls[0]["as_of"] == SOURCE_TIME

    offset_cutoff = REQUEST_TIME.astimezone(timezone(timedelta(hours=5, minutes=30)))
    equivalent, _ = _features(event, _derivation(event, ()), (_ttm(event),), as_of=offset_cutoff)
    assert equivalent.event_age_days.value == Decimal("20")


def test_request_and_identity_validation_fail_closed() -> None:
    event = _event()
    with pytest.raises(ValueError, match="timezone-aware"):
        _features(event, None, as_of=datetime(2026, 9, 21))
    with pytest.raises(ValueError, match="not public"):
        _features(event, None, as_of=SOURCE_TIME - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="ruleset"):
        _features(replace(event, ruleset_semantic_version="future"), None)
    with pytest.raises(ValueError, match="filing_scope"):
        BusinessEventFeaturePrimitives(
            cast(TrailingTwelveMonthNormalizer, _StubTTMNormalizer(()))
        ).features_as_of(
            event=event,
            quantitative_derivation=None,
            financial_provider_dataset_id=FINANCIAL_PROVIDER,
            filing_scope=" ",
            as_of=REQUEST_TIME,
        )


def test_derivation_identity_mismatch_is_rejected() -> None:
    event = _event()
    other = _event()
    with pytest.raises(ValueError, match="does not match"):
        _features(event, _derivation(other, ()), (_ttm(event),))
    with pytest.raises(ValueError, match="does not match"):
        _features(
            event,
            replace(_derivation(event, ()), ruleset_semantic_version="future"),
            (_ttm(event),),
        )


def test_absent_and_empty_derivation_remain_distinct() -> None:
    event = _event()
    absent, _ = _features(event, None, (_ttm(event),))
    empty, _ = _features(event, _derivation(event, ()), (_ttm(event),))
    assert absent.quantitative_ruleset_code is None
    assert absent.order_value_to_ttm_revenue.warnings == (
        "quantitative_derivation_unavailable",
    )
    assert empty.quantitative_ruleset_code == BUSINESS_EVENT_QUANT_RULESET_CODE
    assert empty.order_value_to_ttm_revenue.warnings == ("missing_order_value",)


def test_later_first_revenue_does_not_backfill_event_materiality() -> None:
    event = _event()
    fact = _fact(
        event,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("2500000000"),
        normalized_unit="currency_major",
        currency="INR",
    )
    later_available = SOURCE_TIME + timedelta(days=30)
    later_ttm = _ttm(event, available_at=later_available)

    class TimeAwareNormalizer(_StubTTMNormalizer):
        def ttm_series_as_of(self, **kwargs: object) -> list[TrailingTwelveMonthValue]:
            self.calls.append(kwargs)
            cutoff = kwargs["as_of"]
            assert isinstance(cutoff, datetime)
            return [later_ttm] if cutoff >= later_available else []

    stub = TimeAwareNormalizer((later_ttm,))
    result = BusinessEventFeaturePrimitives(
        cast(TrailingTwelveMonthNormalizer, stub)
    ).features_as_of(
        event=event,
        quantitative_derivation=_derivation(event, (fact,)),
        financial_provider_dataset_id=FINANCIAL_PROVIDER,
        filing_scope="standalone",
        as_of=SOURCE_TIME + timedelta(days=90),
    )
    assert stub.calls[0]["as_of"] == SOURCE_TIME
    assert result.event_time_ttm_revenue is None
    assert result.order_value_to_ttm_revenue.warnings == (
        "missing_event_time_ttm_revenue",
    )


def test_corrected_value_and_value_removal_never_carry_forward() -> None:
    original = _event(source_time=SOURCE_TIME)
    corrected_time = SOURCE_TIME + timedelta(days=1)
    corrected = _event(source_time=corrected_time)
    original_fact = _fact(
        original,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("1000000000"),
        normalized_unit="currency_major",
        currency="INR",
    )
    corrected_fact = _fact(
        corrected,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("1200000000"),
        normalized_unit="currency_major",
        currency="INR",
    )
    original_bundle, _ = _features(
        original,
        _derivation(original, (original_fact,)),
        (_ttm(original),),
    )
    corrected_bundle, _ = _features(
        corrected,
        _derivation(corrected, (corrected_fact,)),
        (_ttm(corrected),),
    )
    removed_bundle, _ = _features(
        corrected,
        _derivation(corrected, ()),
        (_ttm(corrected),),
    )
    assert original_bundle.order_value_to_ttm_revenue.value == Decimal("0.1")
    assert corrected_bundle.order_value_to_ttm_revenue.value == Decimal("0.12")
    assert removed_bundle.event_age_days.value is not None
    assert removed_bundle.order_value_to_ttm_revenue.value is None
    assert removed_bundle.order_value_to_ttm_revenue.warnings == ("missing_order_value",)


def test_equivalent_order_observations_resolve_once_and_retain_both_facts() -> None:
    event = _event()
    first = _fact(
        event,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("2500000000"),
        normalized_unit="currency_major",
        currency="INR",
        raw_text="₹250 crore",
    )
    second = _fact(
        event,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("2500000000"),
        normalized_unit="currency_major",
        currency="INR",
        raw_text="INR 2,500 million",
    )
    result, _ = _features(event, _derivation(event, (first, second)), (_ttm(event),))
    feature = result.order_value_to_ttm_revenue
    assert feature.value == Decimal("0.25")
    observation = feature.evidence[0]
    assert isinstance(observation, ResolvedQuantitativeObservation)
    assert observation.value == Decimal("2500000000")
    assert observation.algorithm_version == QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION
    assert observation.facts == (first, second)


def test_conflicting_order_observations_fail_closed() -> None:
    event = _event()
    facts = tuple(
        _fact(
            event,
            fact_code="order_value",
            fact_kind="monetary",
            normalized_value=value,
            normalized_unit="currency_major",
            currency="INR",
        )
        for value in (Decimal("2500000000"), Decimal("3000000000"))
    )
    result, _ = _features(event, _derivation(event, facts), (_ttm(event),))
    assert result.order_value_to_ttm_revenue.value is None
    assert result.order_value_to_ttm_revenue.warnings == (
        "conflicting_quantitative_observations",
    )


@pytest.mark.parametrize(
    ("values", "warning"),
    (
        ((), "missing_event_time_ttm_revenue"),
        (
            (
                "non_positive",
            ),
            "non_positive_event_time_ttm_revenue",
        ),
        (("ambiguous",), "ambiguous_latest_event_time_ttm_revenue"),
    ),
)
def test_event_time_revenue_unavailability(values: tuple[object, ...], warning: str) -> None:
    event = _event()
    fact = _fact(
        event,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("250"),
        normalized_unit="currency_major",
        currency="INR",
    )
    ttms: tuple[TrailingTwelveMonthValue, ...]
    if values == ("non_positive",):
        ttms = (_ttm(event, Decimal("0")),)
    elif values == ("ambiguous",):
        ttms = (_ttm(event), _ttm(event, Decimal("9000000000")))
    else:
        ttms = ()
    result, _ = _features(event, _derivation(event, (fact,)), ttms)
    assert result.order_value_to_ttm_revenue.value is None
    assert result.order_value_to_ttm_revenue.warnings == (warning,)


def test_event_time_revenue_context_and_algorithm_are_validated() -> None:
    event = _event()
    fact = _fact(
        event,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("250"),
        normalized_unit="currency_major",
        currency="INR",
    )
    derivation = _derivation(event, (fact,))
    with pytest.raises(ValueError, match="TTM revenue evidence"):
        _features(event, derivation, (replace(_ttm(event), algorithm_version="ttm_v2"),))
    with pytest.raises(ValueError, match="TTM revenue evidence"):
        _features(event, derivation, (replace(_ttm(event), filing_scope="consolidated"),))


def test_foreign_currency_values_are_not_converted() -> None:
    event = _event()
    fact = _fact(
        event,
        fact_code="order_value",
        fact_kind="monetary",
        normalized_value=Decimal("10000000"),
        normalized_unit="currency_major",
        currency="USD",
    )
    result, _ = _features(event, _derivation(event, (fact,)), (_ttm(event),))
    assert result.order_value_to_ttm_revenue.value is None
    assert result.order_value_to_ttm_revenue.warnings == ("non_inr_order_value",)


def test_capex_and_acquisition_reference_features() -> None:
    capex_event = _event("capex_announcement")
    capex_fact = _fact(
        capex_event,
        fact_code="capex_value",
        fact_kind="monetary",
        normalized_value=Decimal("5000000000"),
        normalized_unit="currency_major",
        currency="INR",
    )
    capex, _ = _features(
        capex_event,
        _derivation(capex_event, (capex_fact,)),
        (_ttm(capex_event, Decimal("20000000000")),),
    )
    assert capex.capex_value_to_ttm_revenue.value == Decimal("0.25")

    acquisition_event = _event("acquisition_agreement")
    consideration = _fact(
        acquisition_event,
        fact_code="acquisition_consideration",
        fact_kind="monetary",
        normalized_value=Decimal("2500000000"),
        normalized_unit="currency_major",
        currency="INR",
    )
    stake = _fact(
        acquisition_event,
        fact_code="acquisition_stake_fraction",
        fact_kind="fraction",
        normalized_value=Decimal("0.51"),
        normalized_unit="fraction",
    )
    acquisition, _ = _features(
        acquisition_event,
        _derivation(acquisition_event, (consideration, stake)),
        (_ttm(acquisition_event, Decimal("5000000000")),),
    )
    assert acquisition.acquisition_consideration_to_ttm_revenue.value == Decimal("0.5")
    assert acquisition.acquisition_stake_fraction.value == Decimal("0.51")


def test_capacity_before_after_and_duplicate_unit_resolution() -> None:
    event = _event("capacity_expansion")
    facts = (
        _fact(
            event,
            fact_code="capacity_before",
            fact_kind="capacity",
            normalized_value=Decimal("1000000"),
            normalized_unit="tonnes_per_annum",
            raw_text="1 MTPA",
        ),
        _fact(
            event,
            fact_code="capacity_before",
            fact_kind="capacity",
            normalized_value=Decimal("1000000"),
            normalized_unit="tonnes_per_annum",
            raw_text="1000 KTPA",
        ),
        _fact(
            event,
            fact_code="capacity_after",
            fact_kind="capacity",
            normalized_value=Decimal("1500000"),
            normalized_unit="tonnes_per_annum",
        ),
    )
    result, _ = _features(event, _derivation(event, facts))
    assert result.capacity_change_ratio.value == Decimal("0.5")
    before = result.capacity_change_ratio.evidence[0]
    assert isinstance(before, ResolvedQuantitativeObservation)
    assert len(before.facts) == 2


def test_capacity_before_additional_and_dual_path_consistency() -> None:
    event = _event("capacity_expansion")
    base = _fact(
        event,
        fact_code="capacity_before",
        fact_kind="capacity",
        normalized_value=Decimal("1000"),
        normalized_unit="megawatt",
    )
    after = _fact(
        event,
        fact_code="capacity_after",
        fact_kind="capacity",
        normalized_value=Decimal("1500"),
        normalized_unit="megawatt",
    )
    additional = _fact(
        event,
        fact_code="additional_capacity",
        fact_kind="capacity",
        normalized_value=Decimal("500"),
        normalized_unit="megawatt",
    )
    consistent, _ = _features(event, _derivation(event, (base, after, additional)))
    assert consistent.capacity_change_ratio.value == Decimal("0.5")

    conflicting = replace(additional, normalized_value=Decimal("400"))
    conflict, _ = _features(event, _derivation(event, (base, after, conflicting)))
    assert conflict.capacity_change_ratio.value is None
    assert conflict.capacity_change_ratio.warnings == (
        "conflicting_capacity_change_calculations",
    )


def test_capacity_fail_closed_cases() -> None:
    event = _event("capacity_expansion")
    additional = _fact(
        event,
        fact_code="additional_capacity",
        fact_kind="capacity",
        normalized_value=Decimal("500"),
        normalized_unit="megawatt",
    )
    no_baseline, _ = _features(event, _derivation(event, (additional,)))
    assert no_baseline.capacity_change_ratio.warnings == ("missing_capacity_baseline",)

    base = replace(additional, id=uuid4(), fact_code="capacity_before")
    incompatible = replace(
        additional,
        id=uuid4(),
        fact_code="capacity_after",
        normalized_value=Decimal("1000"),
        normalized_unit="tonnes_per_annum",
    )
    mismatch, _ = _features(event, _derivation(event, (base, incompatible)))
    assert mismatch.capacity_change_ratio.warnings == ("incompatible_capacity_units",)

    lower = replace(
        additional,
        id=uuid4(),
        fact_code="capacity_after",
        normalized_value=Decimal("400"),
    )
    contraction, _ = _features(event, _derivation(event, (base, lower)))
    assert contraction.capacity_change_ratio.warnings == ("non_expanding_capacity_pair",)

    conflicting_base = replace(base, id=uuid4(), normalized_value=Decimal("600"))
    conflict, _ = _features(
        event, _derivation(event, (base, conflicting_base, additional))
    )
    assert conflict.capacity_change_ratio.warnings == (
        "conflicting_quantitative_observations",
    )


@pytest.mark.parametrize("event_type", ("commercial_commencement", "regulatory_approval"))
def test_non_quantitative_v1_event_types_expose_age_only(event_type: str) -> None:
    event = _event(event_type)
    result, stub = _features(event, _derivation(event, ()))
    assert result.event_age_days.value == Decimal("20")
    assert not stub.calls
    for value in (
        result.order_value_to_ttm_revenue,
        result.capex_value_to_ttm_revenue,
        result.capacity_change_ratio,
        result.acquisition_consideration_to_ttm_revenue,
        result.acquisition_stake_fraction,
    ):
        assert value.warnings == ("not_applicable_for_event_type",)
