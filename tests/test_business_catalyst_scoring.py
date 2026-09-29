"""Phase 4D-H Business Catalyst pure component-scoring acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from inflector_core.business_catalyst_scoring import (
    BUSINESS_CATALYST_AGGREGATION_VERSION,
    BUSINESS_CATALYST_COMPONENT_VERSION,
    BUSINESS_CATALYST_EVENT_SCORE_VERSION,
    BUSINESS_CATALYST_V1_SCOREABLE_EVENT_TYPES,
    BusinessCatalystComponentScorer,
)
from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_RULESET_CODE,
    BUSINESS_EVENT_QUANT_RULESET_VERSION,
)
from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
)
from inflector_core.component_scoring import PIECEWISE_LINEAR_CURVE_VERSION
from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    canonical_policy_json,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
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
    BusinessEventFeatureBundle,
    BusinessEventFeatureValue,
)
from inflector_data.business_event_pit import PointInTimeBusinessEvent
from inflector_data.pit import SourceRecordView

FIXTURES = Path(__file__).parent / "fixtures"
POLICY_PATH = FIXTURES / "inflection_model_v1_business_catalyst_development.json"
POLICY_CHECKSUM = "2d66b9f31ce7b0a8fbe850e798e9a7636521c974fef3735e7e4b3ef17df3db8f"
LEGACY_POLICIES = (
    (
        "inflection_model_v1_development.json",
        "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf",
    ),
    (
        "inflection_model_v1_scoring_development.json",
        "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9",
    ),
    (
        "inflection_model_v1_business_quality_development.json",
        "c0a967e75aa30418ef030950032186ca6c0762f1fa67b5cf056ba06b8e8981c1",
    ),
    (
        "inflection_model_v1_cash_flow_quality_development.json",
        "d7e69165b3cba42dfebd01bd0117eba263eb76cd20bd66b5db4da3eeb37f2362",
    ),
    (
        "inflection_model_v1_balance_sheet_development.json",
        "d97590519f69dca71d26671a9266443c9beeee24e28d67d78424671d103979e6",
    ),
    (
        "inflection_model_v1_valuation_development.json",
        "fab4e33bdbcb3b108a36c7d1e4c9e7e92ccbdf0db0abe8eb085adfaff45e5632",
    ),
    (
        "inflection_model_v1_market_structure_development.json",
        "9235f7d9cf09066edc0c27269776b958240c0a0ec52a36353da47dba4e9df381",
    ),
)

COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EVENT_PROVIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
FINANCIAL_PROVIDER_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SECURITY_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
AS_OF = datetime(2026, 9, 29, 12, tzinfo=UTC)


def _mapping(path: Path = POLICY_PATH) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _policy(mapping: dict[str, object] | None = None) -> InflectionScoringPolicy:
    return scoring_policy_from_mapping(mapping or _mapping())


def _source() -> SourceRecordView:
    return SourceRecordView(
        id=uuid4(),
        external_record_id="CATALYST-EVENT",
        source_uri="synthetic://business-catalyst",
        raw_object_key="sha256/aa/business-catalyst",
        raw_payload_reference="record:CATALYST-EVENT",
        content_sha256="a" * 64,
        validation_status="accepted",
    )


def _event(
    *,
    event_id: UUID,
    announcement_id: UUID,
    event_type: str,
    source_time: datetime,
    company_id: UUID,
    event_provider_id: UUID,
    security_id: UUID | None,
) -> PointInTimeBusinessEvent:
    announcement = PointInTimeAnnouncement(
        id=announcement_id,
        company_id=company_id,
        security_id=security_id,
        provider_dataset_id=event_provider_id,
        external_record_id=f"CATALYST-{event_id}",
        provider_category=None,
        headline="Fictional deterministic business event",
        announcement_date=date(2026, 9, 1),
        exchange=None,
        available_at=source_time,
        revision_at=None,
        ingested_at=source_time + timedelta(hours=1),
        source_record=_source(),
        documents=(),
    )
    return PointInTimeBusinessEvent(
        id=event_id,
        announcement=announcement,
        company_id=company_id,
        security_id=security_id,
        provider_dataset_id=event_provider_id,
        event_type=event_type,
        source_event_date=announcement.announcement_date,
        source_available_at=source_time,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
        matched_rule_codes=("synthetic_business_event_rule_v1",),
        status="detected",
        warnings=(),
        detection_fingerprint_sha256=sha256(str(event_id).encode()).hexdigest(),
        derived_at=source_time + timedelta(hours=2),
        evidence=(),
    )


def _source_time(age_days: Decimal, as_of: datetime) -> datetime:
    total_microseconds = age_days * Decimal("86400000000")
    assert total_microseconds == total_microseconds.to_integral_value()
    return as_of - timedelta(microseconds=int(total_microseconds))


def _feature(
    *,
    code: str,
    version: str,
    unit: str,
    source_time: datetime,
    as_of: datetime,
    value: Decimal | None,
    warnings: tuple[str, ...],
    evidence: tuple[object, ...],
    applicable: bool,
) -> BusinessEventFeatureValue:
    return BusinessEventFeatureValue(
        code=code,
        value=value,
        unit=unit,
        warnings=warnings,
        as_of=as_of,
        available_at=source_time if applicable else None,
        algorithm_version=version,
        evidence=evidence,  # type: ignore[arg-type]
    )


def _bundle(
    event_type: str = "order_award",
    *,
    age_days: Decimal = Decimal("30"),
    order_ratio: Decimal | None = Decimal("0.25"),
    capacity_ratio: Decimal | None = Decimal("0.50"),
    capex_ratio: Decimal | None = Decimal("1"),
    acquisition_ratio: Decimal | None = Decimal("1"),
    stake_fraction: Decimal | None = Decimal("1"),
    event_id: UUID | None = None,
    announcement_id: UUID | None = None,
    company_id: UUID = COMPANY_ID,
    event_provider_id: UUID = EVENT_PROVIDER_ID,
    financial_provider_id: UUID = FINANCIAL_PROVIDER_ID,
    filing_scope: str = "consolidated",
    security_id: UUID | None = SECURITY_ID,
    as_of: datetime = AS_OF,
) -> BusinessEventFeatureBundle:
    event_id = event_id or uuid4()
    announcement_id = announcement_id or uuid4()
    source_time = _source_time(age_days, as_of)
    event = _event(
        event_id=event_id,
        announcement_id=announcement_id,
        event_type=event_type,
        source_time=source_time,
        company_id=company_id,
        event_provider_id=event_provider_id,
        security_id=security_id,
    )
    relevant = {
        "order_award": {"order_value_to_ttm_revenue": order_ratio},
        "capacity_expansion": {"capacity_change_ratio": capacity_ratio},
        "commercial_commencement": {},
        "capex_announcement": {"capex_value_to_ttm_revenue": capex_ratio},
        "acquisition_agreement": {
            "acquisition_consideration_to_ttm_revenue": acquisition_ratio,
            "acquisition_stake_fraction": stake_fraction,
        },
        "regulatory_approval": {},
    }[event_type]
    specs = {
        "order_value_to_ttm_revenue": (ORDER_VALUE_TO_TTM_REVENUE_VERSION, "ratio"),
        "capex_value_to_ttm_revenue": (CAPEX_VALUE_TO_TTM_REVENUE_VERSION, "ratio"),
        "capacity_change_ratio": (CAPACITY_CHANGE_RATIO_VERSION, "ratio"),
        "acquisition_consideration_to_ttm_revenue": (
            ACQUISITION_CONSIDERATION_TO_TTM_REVENUE_VERSION,
            "ratio",
        ),
        "acquisition_stake_fraction": (ACQUISITION_STAKE_FRACTION_VERSION, "fraction"),
    }
    missing_warnings = {
        "order_value_to_ttm_revenue": "missing_order_value",
        "capex_value_to_ttm_revenue": "missing_capex_value",
        "capacity_change_ratio": "missing_capacity_baseline",
        "acquisition_consideration_to_ttm_revenue": "missing_acquisition_consideration",
        "acquisition_stake_fraction": "missing_acquisition_stake",
    }
    features: dict[str, BusinessEventFeatureValue] = {}
    for code, (version, unit) in specs.items():
        applicable = code in relevant
        value = relevant.get(code)
        if not applicable:
            warnings = ("not_applicable_for_event_type",)
        elif value is None:
            warnings = (missing_warnings[code],)
        else:
            warnings = ()
        features[code] = _feature(
            code=code,
            version=version,
            unit=unit,
            source_time=source_time,
            as_of=as_of,
            value=value,
            warnings=warnings,
            evidence=(event,),
            applicable=applicable,
        )
    return BusinessEventFeatureBundle(
        company_id=company_id,
        security_id=security_id,
        provider_dataset_id=event_provider_id,
        announcement_id=announcement_id,
        business_event_id=event_id,
        event_type=event_type,
        business_event_ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        business_event_ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
        quantitative_ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        quantitative_ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
        financial_provider_dataset_id=financial_provider_id,
        filing_scope=filing_scope,
        source_event_date=event.source_event_date,
        source_available_at=source_time,
        materiality_financial_cutoff=source_time,
        as_of=as_of,
        event_age_days=BusinessEventFeatureValue(
            code="event_age_days",
            value=age_days,
            unit="days",
            warnings=(),
            as_of=as_of,
            available_at=source_time,
            algorithm_version=BUSINESS_EVENT_AGE_DAYS_VERSION,
            evidence=(event,),
        ),
        order_value_to_ttm_revenue=features["order_value_to_ttm_revenue"],
        capex_value_to_ttm_revenue=features["capex_value_to_ttm_revenue"],
        capacity_change_ratio=features["capacity_change_ratio"],
        acquisition_consideration_to_ttm_revenue=features[
            "acquisition_consideration_to_ttm_revenue"
        ],
        acquisition_stake_fraction=features["acquisition_stake_fraction"],
        event_time_ttm_revenue=None,
        algorithm_version=BUSINESS_EVENT_FEATURE_BUNDLE_VERSION,
    )


def _score(*bundles: BusinessEventFeatureBundle):
    return BusinessCatalystComponentScorer().score(evidence=tuple(bundles), policy=_policy())


def test_reference_policy_and_all_historical_checksums_are_stable() -> None:
    for filename, expected in LEGACY_POLICIES:
        policy = scoring_policy_from_mapping(_mapping(FIXTURES / filename))
        assert scoring_policy_checksum(policy) == expected
        assert policy.business_catalyst is None
        assert '"business_catalyst":null' not in canonical_policy_json(policy)

    policy = _policy()
    catalyst = policy.business_catalyst
    assert catalyst is not None
    assert scoring_policy_checksum(policy) == POLICY_CHECKSUM
    assert catalyst.maximum_event_age_days == 365
    assert catalyst.commercial_commencement_base_score == Decimal("80")
    assert catalyst.regulatory_approval_base_score == Decimal("90")
    assert catalyst.aggregation_method == BUSINESS_CATALYST_AGGREGATION_VERSION


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"maximum_event_age_days": 0}, "maximum_event_age_days"),
        ({"commercial_commencement_base_score": 80.0}, "binary floats"),
        ({"regulatory_approval_base_score": "101"}, "base scores"),
        ({"aggregation_method": "average_event_score_v1"}, "aggregation_method"),
    ),
)
def test_business_catalyst_policy_rejects_invalid_values(
    mutation: dict[str, object], message: str
) -> None:
    mapping = _mapping()
    section = mapping["business_catalyst"]
    assert isinstance(section, dict)
    section.update(mutation)
    with pytest.raises(ValidationError, match=message):
        scoring_policy_from_mapping(mapping)


@pytest.mark.parametrize(
    ("event_type", "expected_strength", "expected_score", "strength_code"),
    (
        ("order_award", Decimal("80"), Decimal("68"), "order_value_to_ttm_revenue"),
        ("capacity_expansion", Decimal("90"), Decimal("76.5"), "capacity_change_ratio"),
        (
            "commercial_commencement",
            Decimal("80"),
            Decimal("68"),
            "commercial_commencement_base_score",
        ),
        (
            "regulatory_approval",
            Decimal("90"),
            Decimal("76.5"),
            "regulatory_approval_base_score",
        ),
    ),
)
def test_reference_event_scores_are_exact(
    event_type: str,
    expected_strength: Decimal,
    expected_score: Decimal,
    strength_code: str,
) -> None:
    result = _score(_bundle(event_type))
    event = result.event_scores[0]
    assert BUSINESS_CATALYST_V1_SCOREABLE_EVENT_TYPES == (
        "order_award",
        "capacity_expansion",
        "commercial_commencement",
        "regulatory_approval",
    )
    assert event.strength_score == expected_strength
    assert event.strength_code == strength_code
    assert event.event_age_days == Decimal("30")
    assert event.recency_scoring_value == Decimal("-30")
    assert event.recency_transform_code == "negate_event_age_days"
    assert event.recency_score == Decimal("85")
    assert event.recency_curve_algorithm_version == PIECEWISE_LINEAR_CURVE_VERSION
    assert event.score == expected_score
    assert result.score == expected_score
    assert event.algorithm_version == BUSINESS_CATALYST_EVENT_SCORE_VERSION
    assert result.algorithm_version == BUSINESS_CATALYST_COMPONENT_VERSION
    assert event.evidence is result.event_scores[0].evidence
    assert result.unit == "score_0_100"


@pytest.mark.parametrize("event_type", ("capex_announcement", "acquisition_agreement"))
def test_neutral_event_types_remain_auditable_but_unscoreable(event_type: str) -> None:
    result = _score(_bundle(event_type))
    event = result.event_scores[0]
    assert event.score is None
    assert event.strength_score is None
    assert event.warnings == ("event_type_not_scoreable_in_business_catalyst_v1",)
    assert result.score is None
    assert result.warnings == ("no_scoreable_business_catalyst_event",)


@pytest.mark.parametrize(
    ("event_type", "kwargs", "underlying_warning"),
    (
        ("order_award", {"order_ratio": None}, "missing_order_value"),
        (
            "capacity_expansion",
            {"capacity_ratio": None},
            "missing_capacity_baseline",
        ),
    ),
)
def test_unquantified_event_cannot_score_from_recency_alone(
    event_type: str, kwargs: dict[str, object], underlying_warning: str
) -> None:
    event = _score(_bundle(event_type, **kwargs)).event_scores[0]  # type: ignore[arg-type]
    assert event.score is None
    assert event.recency_score == Decimal("85")
    assert event.warnings == (underlying_warning, "event_strength_unavailable")


def test_event_window_boundary_distinguishes_zero_score_from_unavailable() -> None:
    boundary = _score(_bundle(age_days=Decimal("365"))).event_scores[0]
    assert boundary.recency_score == Decimal("0")
    assert boundary.score == Decimal("0")
    assert boundary.warnings == ()

    outside = _score(_bundle(age_days=Decimal("365.000001"))).event_scores[0]
    assert outside.score is None
    assert outside.recency_score is None
    assert outside.warnings == ("outside_business_catalyst_window",)


def test_multi_event_aggregation_uses_max_and_canonical_tie_break() -> None:
    source_id = uuid4()
    order = _bundle("order_award", event_id=source_id)
    capacity = _bundle("capacity_expansion")
    commercial = _bundle("commercial_commencement")
    regulatory = _bundle("regulatory_approval")
    capex = _bundle("capex_announcement")
    acquisition = _bundle("acquisition_agreement")
    result = _score(order, capacity, commercial, regulatory, capex, acquisition)

    assert tuple(item.event_type for item in result.event_scores) == (
        "order_award",
        "capacity_expansion",
        "commercial_commencement",
        "capex_announcement",
        "acquisition_agreement",
        "regulatory_approval",
    )
    assert tuple(item.score for item in result.event_scores) == (
        Decimal("68"),
        Decimal("76.5"),
        Decimal("68"),
        None,
        None,
        Decimal("76.5"),
    )
    assert result.score == Decimal("76.5")
    assert result.selected_event_type == "capacity_expansion"
    assert result.aggregation_method == "max_event_score_v1"


def test_duplicate_disclosures_do_not_inflate_and_duplicate_input_is_rejected() -> None:
    first = _bundle()
    second = _bundle()
    result = _score(first, second)
    assert result.score == Decimal("68")
    assert len(result.event_scores) == 2
    assert result.selected_event_id == min(
        (first.business_event_id, second.business_event_id), key=str
    )
    with pytest.raises(ValueError, match="business_event_id must be unique"):
        _score(first, first)


def test_newer_equal_score_wins_without_changing_component_score_or_available_at() -> None:
    older = _bundle("order_award", age_days=Decimal("30"), order_ratio=Decimal("0.25"))
    newer = _bundle("order_award", age_days=Decimal("0"), order_ratio=Decimal("0.16"))
    result = _score(older, newer)
    assert tuple(item.score for item in result.event_scores) == (Decimal("68"), Decimal("68"))
    assert result.score == Decimal("68")
    assert result.selected_event_id == newer.business_event_id
    assert result.available_at == newer.source_available_at


def test_weaker_newer_event_sets_component_availability_but_does_not_dilute_score() -> None:
    older = _bundle("capacity_expansion", age_days=Decimal("30"))
    newer = _bundle("order_award", age_days=Decimal("0"), order_ratio=Decimal("0.02"))
    result = _score(older, newer)
    assert result.score == Decimal("76.5")
    assert result.selected_event_id == older.business_event_id
    assert result.available_at == newer.source_available_at


@pytest.mark.parametrize(
    "second",
    (
        _bundle(company_id=uuid4()),
        _bundle(event_provider_id=uuid4()),
        _bundle(financial_provider_id=uuid4()),
        _bundle(filing_scope="standalone"),
        _bundle(as_of=AS_OF + timedelta(seconds=1)),
        _bundle(security_id=uuid4()),
    ),
)
def test_mixed_component_context_is_rejected(second: BusinessEventFeatureBundle) -> None:
    with pytest.raises(ValueError):
        _score(_bundle(), second)


def test_company_level_and_one_security_are_coherent() -> None:
    result = _score(_bundle(security_id=None), _bundle(security_id=SECURITY_ID))
    assert result.security_id == SECURITY_ID


@pytest.mark.parametrize(
    "tampered",
    (
        replace(_bundle(), algorithm_version="business_event_feature_bundle_v2"),
        replace(_bundle(), business_event_ruleset_semantic_version="business_event_rules_v2"),
        replace(_bundle(), quantitative_ruleset_code=None),
        replace(
            _bundle(),
            materiality_financial_cutoff=AS_OF - timedelta(days=29),
        ),
        replace(
            _bundle(),
            event_age_days=replace(
                _bundle().event_age_days,
                algorithm_version="business_event_age_days_v2",
            ),
        ),
        replace(
            _bundle(),
            order_value_to_ttm_revenue=replace(
                _bundle().order_value_to_ttm_revenue,
                algorithm_version="order_value_to_ttm_revenue_v2",
            ),
        ),
    ),
)
def test_feature_and_upstream_version_tampering_fails_closed(
    tampered: BusinessEventFeatureBundle,
) -> None:
    with pytest.raises(ValueError):
        _score(tampered)


def test_policy_is_required_and_empty_evidence_is_not_zero() -> None:
    legacy = scoring_policy_from_mapping(
        _mapping(FIXTURES / "inflection_model_v1_market_structure_development.json")
    )
    with pytest.raises(ValueError, match="policy is not configured"):
        BusinessCatalystComponentScorer().score(evidence=(_bundle(),), policy=legacy)
    with pytest.raises(ValueError, match="must not be empty"):
        BusinessCatalystComponentScorer().score(evidence=(), policy=_policy())


def test_component_score_is_decimal_and_excludes_confidence_and_top_level_weight() -> None:
    result = _score(_bundle())
    assert isinstance(result.score, Decimal)
    assert result.score == Decimal("68")
    assert result.score != Decimal("68") * Decimal("0.20")
