"""Phase 4D-J Low Market Attention pure component-scoring acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from inflector_core.component_scoring import PIECEWISE_LINEAR_CURVE_VERSION
from inflector_core.low_market_attention_scoring import (
    LOW_MARKET_ATTENTION_COMPONENT_VERSION,
    LOW_MARKET_ATTENTION_SUBFACTOR_ORDER,
    LowMarketAttentionComponentScorer,
)
from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_data.attention_features import (
    ANALYST_COVERAGE_COUNT_FEATURE_VERSION,
    ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION,
    ATTENTION_FEATURE_BUNDLE_VERSION,
    NEWS_MENTIONS_COUNT_FEATURE_VERSION,
    NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION,
    NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION,
    AttentionFeatureBundle,
    AttentionFeaturePrimitives,
    AttentionSeriesIdentity,
)
from inflector_data.attention_pit import (
    PointInTimeAttentionObservation,
    PointInTimeAttentionReader,
)
from inflector_data.pit import SourceRecordView

FIXTURES = Path(__file__).parent / "fixtures"
BASE_POLICY = FIXTURES / "inflection_model_v1_business_catalyst_development.json"
ATTENTION_POLICY = (
    FIXTURES / "inflection_model_v1_low_market_attention_development.json"
)
REFERENCE_CHECKSUM = "203e42443dfb0380acc450feddcfe71779bd7afdb3914756ae7c8e520f456740"
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
    (
        "inflection_model_v1_business_catalyst_development.json",
        "2d66b9f31ce7b0a8fbe850e798e9a7636521c974fef3735e7e4b3ef17df3db8f",
    ),
)

COMPANY_ID = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
SECURITY_ID = UUID("22222222-aaaa-4aaa-8aaa-222222222222")
NEWS_PROVIDER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ANALYST_PROVIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NEWS_DEFINITION = "a" * 64
ANALYST_DEFINITION = "b" * 64
AS_OF = datetime(2026, 10, 1, 12, tzinfo=UTC)
WINDOW_START = datetime(2026, 9, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 1, tzinfo=UTC)

NEWS_SERIES = AttentionSeriesIdentity(
    provider_dataset_id=NEWS_PROVIDER_ID,
    scope_code="synthetic_indexed_business_news",
    methodology_version="synthetic_news_search_v1",
    measurement_definition_sha256=NEWS_DEFINITION,
)
ANALYST_SERIES = AttentionSeriesIdentity(
    provider_dataset_id=ANALYST_PROVIDER_ID,
    scope_code="synthetic_active_equity_analysts",
    methodology_version="synthetic_analyst_coverage_v1",
    measurement_definition_sha256=ANALYST_DEFINITION,
)


class _Reader:
    def __init__(
        self,
        news: PointInTimeAttentionObservation | None,
        analyst: PointInTimeAttentionObservation | None,
    ) -> None:
        self.news = news
        self.analyst = analyst

    def news_count_for_exact_window_as_of(
        self, **_: object
    ) -> PointInTimeAttentionObservation | None:
        return self.news

    def latest_analyst_coverage_as_of(
        self, **_: object
    ) -> PointInTimeAttentionObservation | None:
        return self.analyst


def _mapping(*, include_attention: bool = True) -> dict[str, object]:
    value = json.loads(BASE_POLICY.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    if include_attention:
        extension = json.loads(ATTENTION_POLICY.read_text(encoding="utf-8"))
        assert isinstance(extension, dict)
        value.update(extension)
    return value


def _policy(mapping: dict[str, object] | None = None) -> InflectionScoringPolicy:
    return scoring_policy_from_mapping(mapping or _mapping())


def _source(code: str) -> SourceRecordView:
    digest = sha256(code.encode()).hexdigest()
    return SourceRecordView(
        id=UUID(digest[:32]),
        external_record_id=code,
        source_uri=f"synthetic://attention/{code}",
        raw_object_key=f"sha256/{digest}",
        raw_payload_reference=f"record:{code}",
        content_sha256=sha256(f"content:{code}".encode()).hexdigest(),
        validation_status="accepted",
    )


def _news(
    count: int = 5,
    *,
    coverage: str = "complete",
    available_at: datetime = datetime(2026, 10, 1, 8, tzinfo=UTC),
    security_id: UUID | None = None,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
) -> PointInTimeAttentionObservation:
    return PointInTimeAttentionObservation(
        id=UUID("33333333-aaaa-4aaa-8aaa-333333333333"),
        company_id=COMPANY_ID,
        security_id=security_id,
        provider_dataset_id=NEWS_PROVIDER_ID,
        metric_code="news_mentions_count",
        reported_count=count,
        reported_unit="count",
        scope_code=NEWS_SERIES.scope_code,
        methodology_version=NEWS_SERIES.methodology_version,
        measurement_definition_sha256=NEWS_DEFINITION,
        coverage_status=coverage,
        observation_date=None,
        window_start_at=window_start,
        window_end_at=window_end,
        available_at=available_at,
        revision_at=None,
        ingested_at=available_at + timedelta(minutes=5),
        source_record=_source(f"NEWS-{coverage}-{count}"),
    )


def _analyst(
    count: int = 2,
    *,
    coverage: str = "complete",
    available_at: datetime = datetime(2026, 10, 1, 9, tzinfo=UTC),
    observation_date: date = date(2026, 9, 30),
    security_id: UUID | None = None,
) -> PointInTimeAttentionObservation:
    return PointInTimeAttentionObservation(
        id=UUID("44444444-aaaa-4aaa-8aaa-444444444444"),
        company_id=COMPANY_ID,
        security_id=security_id,
        provider_dataset_id=ANALYST_PROVIDER_ID,
        metric_code="analyst_coverage_count",
        reported_count=count,
        reported_unit="count",
        scope_code=ANALYST_SERIES.scope_code,
        methodology_version=ANALYST_SERIES.methodology_version,
        measurement_definition_sha256=ANALYST_DEFINITION,
        coverage_status=coverage,
        observation_date=observation_date,
        window_start_at=None,
        window_end_at=None,
        available_at=available_at,
        revision_at=None,
        ingested_at=available_at + timedelta(minutes=5),
        source_record=_source(f"ANALYST-{coverage}-{count}-{observation_date}"),
    )


def _bundle(
    *,
    news: PointInTimeAttentionObservation | None = None,
    analyst: PointInTimeAttentionObservation | None = None,
    security_id: UUID | None = None,
    company_level_only: bool = True,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
    analyst_bound: date | None = None,
) -> AttentionFeatureBundle:
    if news is None and analyst is None:
        news = _news(security_id=security_id)
        analyst = _analyst(security_id=security_id)
    reader = _Reader(news, analyst)
    return AttentionFeaturePrimitives(cast(PointInTimeAttentionReader, reader)).features_as_of(
        company_id=COMPANY_ID,
        security_id=security_id,
        company_level_only=company_level_only,
        as_of=AS_OF,
        news_series=NEWS_SERIES,
        news_window_start_at=window_start,
        news_window_end_at=window_end,
        analyst_series=ANALYST_SERIES,
        analyst_observation_on_or_before=analyst_bound,
    )


def _score(
    bundle: AttentionFeatureBundle | None = None,
    policy: InflectionScoringPolicy | None = None,
):
    return LowMarketAttentionComponentScorer().score(
        evidence=bundle or _bundle(),
        policy=policy or _policy(),
    )


def test_policy_omission_preserves_all_historical_checksums() -> None:
    for filename, checksum in LEGACY_POLICIES:
        mapping = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
        assert scoring_policy_checksum(scoring_policy_from_mapping(mapping)) == checksum
    assert _policy(_mapping(include_attention=False)).low_market_attention is None


def test_reference_policy_checksum_is_pinned() -> None:
    assert scoring_policy_checksum(_policy()) == REFERENCE_CHECKSUM


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("low_market_attention", "minimum_weight_coverage"), 1.0),
        (("low_market_attention", "required_news_window_duration_days"), 30.0),
        (("low_market_attention", "maximum_news_window_age_days"), 1.0),
        (
            (
                "low_market_attention",
                "subfactor_weights",
                "news_mentions_count",
            ),
            0.5,
        ),
    ),
)
def test_binary_float_policy_values_are_rejected(
    path: tuple[str, ...], value: float
) -> None:
    mapping = _mapping()
    target: dict[str, object] = mapping
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value
    with pytest.raises(ValidationError, match="binary float"):
        _policy(mapping)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("scope_code", " scope"),
        ("methodology_version", ""),
        ("measurement_definition_sha256", "not-a-hash"),
    ),
)
def test_invalid_policy_series_identities_are_rejected(field: str, value: str) -> None:
    mapping = _mapping()
    attention = mapping["low_market_attention"]
    assert isinstance(attention, dict)
    series = attention["news_series"]
    assert isinstance(series, dict)
    series[field] = value
    with pytest.raises(ValidationError):
        _policy(mapping)


def test_reference_score_has_exact_transform_weights_and_lineage() -> None:
    bundle = _bundle()
    result = _score(bundle)
    assert LOW_MARKET_ATTENTION_SUBFACTOR_ORDER == (
        "news_mentions_count",
        "analyst_coverage_count",
    )
    assert result.algorithm_version == LOW_MARKET_ATTENTION_COMPONENT_VERSION
    assert result.score == Decimal("77.5")
    assert result.unit == "score_0_100"
    assert result.weight_coverage == result.available_weight == Decimal("1")
    assert tuple(item.normalized_score for item in result.subfactors) == (
        Decimal("75"),
        Decimal("80"),
    )
    assert tuple(item.scoring_value for item in result.subfactors) == (
        Decimal("-5"),
        Decimal("-2"),
    )
    assert tuple(item.transform_code for item in result.subfactors) == (
        "negate_news_mentions_count",
        "negate_analyst_coverage_count",
    )
    assert all(
        item.curve_algorithm_version == PIECEWISE_LINEAR_CURVE_VERSION
        for item in result.subfactors
    )
    assert result.available_at == datetime(2026, 10, 1, 9, tzinfo=UTC)
    assert result.evidence is bundle


@pytest.mark.parametrize(
    ("news_count", "analyst_count", "expected"),
    ((0, 0, Decimal("100")), (80, 30, Decimal("0"))),
)
def test_complete_zero_and_high_attention_zero_score_are_available(
    news_count: int, analyst_count: int, expected: Decimal
) -> None:
    result = _score(_bundle(news=_news(news_count), analyst=_analyst(analyst_count)))
    assert result.score == expected
    assert result.warnings == ()
    assert result.available_weight == Decimal("1")


@pytest.mark.parametrize("coverage", ("partial", "unknown"))
def test_blocked_news_count_is_unavailable_without_older_fallback(
    coverage: str,
) -> None:
    bundle = _bundle(news=_news(1, coverage=coverage), analyst=_analyst())
    result = _score(bundle)
    assert result.score is None
    assert result.warnings == ("insufficient_subfactor_coverage",)
    unavailable = result.unavailable_subfactors[0]
    assert unavailable.code == "news_mentions_count"
    assert unavailable.evidence is bundle.news_mentions_count
    assert unavailable.warnings == bundle.news_mentions_count.warnings


@pytest.mark.parametrize("coverage", ("partial", "unknown"))
def test_blocked_analyst_count_is_unavailable_without_older_fallback(
    coverage: str,
) -> None:
    bundle = _bundle(news=_news(), analyst=_analyst(1, coverage=coverage))
    result = _score(bundle)
    assert result.score is None
    unavailable = result.unavailable_subfactors[0]
    assert unavailable.code == "analyst_coverage_count"
    assert unavailable.evidence is bundle.analyst_coverage_count
    assert unavailable.warnings == bundle.analyst_coverage_count.warnings


def test_missing_news_is_not_zero_and_partial_observation_controls_available_at() -> None:
    missing = _bundle(news=None, analyst=_analyst())
    missing_result = _score(missing)
    assert missing_result.score is None
    assert missing.analyst_observation is not None
    assert missing_result.available_at == missing.analyst_observation.available_at

    late_partial = _news(
        0,
        coverage="partial",
        available_at=datetime(2026, 10, 1, 11, tzinfo=UTC),
    )
    blocked = _bundle(news=late_partial, analyst=_analyst())
    blocked_result = _score(blocked)
    assert blocked_result.score is None
    assert blocked_result.available_at == late_partial.available_at

    missing_analyst = _bundle(news=_news(), analyst=None)
    missing_analyst_result = _score(missing_analyst)
    assert missing_analyst_result.score is None
    assert missing_analyst_result.missing_subfactors == ("analyst_coverage_count",)


def test_stale_and_duration_gates_make_counts_unavailable() -> None:
    bundle = _bundle()
    stale_news = replace(
        bundle,
        news_window_age_days=replace(
            bundle.news_window_age_days, value=Decimal("1.000001")
        ),
    )
    stale_news_result = _score(stale_news)
    assert stale_news_result.score is None
    assert stale_news_result.unavailable_subfactors[0].warnings == (
        "news_attention_stale",
    )

    stale_analyst = replace(
        bundle,
        analyst_snapshot_age_days=replace(
            bundle.analyst_snapshot_age_days, value=Decimal("181")
        ),
    )
    stale_analyst_result = _score(stale_analyst)
    assert stale_analyst_result.score is None
    assert stale_analyst_result.unavailable_subfactors[0].warnings == (
        "analyst_attention_stale",
    )

    duration_mismatch = replace(
        bundle,
        news_window_duration_days=replace(
            bundle.news_window_duration_days, value=Decimal("29")
        ),
    )
    duration_result = _score(duration_mismatch)
    assert duration_result.score is None
    assert duration_result.unavailable_subfactors[0].warnings == (
        "news_window_duration_mismatch",
    )


def test_arbitrary_news_window_and_analyst_bound_are_rejected() -> None:
    historical_start = datetime(2026, 8, 31, tzinfo=UTC)
    historical_end = datetime(2026, 9, 30, tzinfo=UTC)
    historical = _bundle(
        news=_news(window_start=historical_start, window_end=historical_end),
        analyst=_analyst(),
        window_start=historical_start,
        window_end=historical_end,
    )
    with pytest.raises(ValueError, match="deterministic policy alignment"):
        _score(historical)
    bounded = replace(_bundle(), analyst_observation_on_or_before=date(2026, 9, 1))
    with pytest.raises(ValueError, match="analyst date bound"):
        _score(bounded)


@pytest.mark.parametrize(
    ("series_name", "field", "value"),
    (
        ("news_series", "provider_dataset_id", UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")),
        ("analyst_series", "scope_code", "other_scope"),
        ("news_series", "methodology_version", "synthetic_news_search_v2"),
        ("analyst_series", "measurement_definition_sha256", "c" * 64),
    ),
)
def test_policy_series_binding_rejects_shopping(
    series_name: str, field: str, value: object
) -> None:
    bundle = _bundle()
    series = getattr(bundle, series_name)
    tampered = replace(bundle, **{series_name: replace(series, **{field: value})})
    with pytest.raises(ValueError, match="series does not match"):
        _score(tampered)


def test_company_and_security_evidence_levels_are_not_blended() -> None:
    security_bundle = _bundle(
        news=_news(security_id=SECURITY_ID),
        analyst=_analyst(security_id=SECURITY_ID),
        security_id=SECURITY_ID,
        company_level_only=False,
    )
    with pytest.raises(ValueError, match="evidence level"):
        _score(security_bundle)


@pytest.mark.parametrize(
    ("feature_name", "field", "value"),
    (
        ("news_mentions_count", "code", "wrong"),
        ("news_window_duration_days", "unit", "count"),
        ("news_window_age_days", "algorithm_version", "future_v2"),
        ("analyst_coverage_count", "value", Decimal("2.5")),
        ("analyst_snapshot_age_days", "value", Decimal("-1")),
    ),
)
def test_feature_identity_version_unit_and_domains_fail_closed(
    feature_name: str, field: str, value: object
) -> None:
    bundle = _bundle()
    feature = getattr(bundle, feature_name)
    tampered = replace(bundle, **{feature_name: replace(feature, **{field: value})})
    with pytest.raises(ValueError):
        _score(tampered)


def test_exact_bundle_and_feature_versions_are_consumed() -> None:
    result = _score()
    bundle = result.evidence
    assert bundle.algorithm_version == ATTENTION_FEATURE_BUNDLE_VERSION
    assert bundle.news_mentions_count.algorithm_version == NEWS_MENTIONS_COUNT_FEATURE_VERSION
    assert (
        bundle.news_window_duration_days.algorithm_version
        == NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION
    )
    assert bundle.news_window_age_days.algorithm_version == NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION
    assert (
        bundle.analyst_coverage_count.algorithm_version
        == ANALYST_COVERAGE_COUNT_FEATURE_VERSION
    )
    assert (
        bundle.analyst_snapshot_age_days.algorithm_version
        == ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION
    )


def test_future_observation_fails_closed() -> None:
    bundle = _bundle()
    future_time = AS_OF + timedelta(seconds=1)
    assert bundle.news_observation is not None
    future_observation = replace(bundle.news_observation, available_at=future_time)
    tampered = replace(
        bundle,
        news_observation=future_observation,
        news_mentions_count=replace(
            bundle.news_mentions_count,
            available_at=future_time,
            evidence=(future_observation,),
        ),
        news_window_duration_days=replace(
            bundle.news_window_duration_days,
            available_at=future_time,
            evidence=(future_observation,),
        ),
        news_window_age_days=replace(
            bundle.news_window_age_days,
            available_at=future_time,
            evidence=(future_observation,),
        ),
    )
    with pytest.raises(ValueError, match="future evidence"):
        _score(tampered)


def test_minimum_coverage_renormalizes_only_when_policy_allows() -> None:
    mapping = _mapping()
    attention = mapping["low_market_attention"]
    assert isinstance(attention, dict)
    attention["minimum_weight_coverage"] = "0.50"
    bundle = _bundle(news=None, analyst=_analyst())
    result = _score(bundle, _policy(mapping))
    assert result.available_weight == Decimal("0.5")
    assert result.score == Decimal("80")
    assert result.subfactors[0].effective_weight == Decimal("1")


def test_policy_absence_and_unsupported_bundle_are_rejected() -> None:
    with pytest.raises(ValueError, match="not configured"):
        _score(policy=_policy(_mapping(include_attention=False)))
    with pytest.raises(ValueError, match="bundle version"):
        _score(replace(_bundle(), algorithm_version="attention_feature_bundle_v2"))


def test_no_forbidden_domain_or_persistence_dependency() -> None:
    source = (
        Path(__file__).parents[1]
        / "packages/core/inflector_core/low_market_attention_scoring.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "PriceBar",
        "MarketStructureFeatureBundle",
        "BusinessEvent",
        "BusinessCatalystComponentScore",
        "SQLAlchemy",
        "Session",
        "ScoreSnapshotRepository",
    ):
        assert forbidden not in source
    operations_migrations = tuple(
        (Path(__file__).parents[1] / "migrations/versions").glob("*0016*")
    )
    assert [item.name for item in operations_migrations] == [
        "20261002_0016_operational_runs.py"
    ]
