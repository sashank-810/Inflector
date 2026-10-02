"""Phase 6D-B deterministic external-attention feature acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select

from inflector_core.providers import (
    AttentionObservationRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.attention_features import (
    ANALYST_COVERAGE_COUNT_FEATURE_VERSION,
    ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION,
    ATTENTION_FEATURE_BUNDLE_VERSION,
    NEWS_MENTIONS_COUNT_FEATURE_VERSION,
    NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION,
    NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION,
    AttentionFeaturePrimitives,
    AttentionSeriesIdentity,
)
from inflector_data.attention_pit import (
    PointInTimeAttentionObservation,
    PointInTimeAttentionReader,
)
from inflector_data.pit import SourceRecordView
from inflector_data.providers import MockAttentionDataProvider
from inflector_data.service import IngestionService
from inflector_database.models import Company, DataProvider, ProviderDataset, Security

COMPANY_ID = UUID("12345678-4321-4234-8234-123456789abc")
SECURITY_ID = UUID("22345678-4321-4234-8234-123456789abc")
NEWS_PROVIDER_ID = UUID("32345678-4321-4234-8234-123456789abc")
ANALYST_PROVIDER_ID = UUID("42345678-4321-4234-8234-123456789abc")
NEWS_DEFINITION = "a" * 64
ANALYST_DEFINITION = "b" * 64
WINDOW_START = datetime(2026, 8, 1, 12, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 1, tzinfo=UTC)
AS_OF = datetime(2026, 9, 2, 12, tzinfo=UTC)
AVAILABLE = datetime(2026, 9, 1, 8, tzinfo=UTC)

NEWS_SERIES = AttentionSeriesIdentity(
    provider_dataset_id=NEWS_PROVIDER_ID,
    scope_code="provider_indexed_business_news",
    methodology_version="synthetic_news_search_v1",
    measurement_definition_sha256=NEWS_DEFINITION,
)
ANALYST_SERIES = AttentionSeriesIdentity(
    provider_dataset_id=ANALYST_PROVIDER_ID,
    scope_code="provider_active_equity_analysts",
    methodology_version="synthetic_analyst_coverage_v1",
    measurement_definition_sha256=ANALYST_DEFINITION,
)


class _StubAttentionReader:
    def __init__(
        self,
        news: PointInTimeAttentionObservation | None,
        analyst: PointInTimeAttentionObservation | None,
    ) -> None:
        self.news = news
        self.analyst = analyst
        self.news_calls: list[dict[str, object]] = []
        self.analyst_calls: list[dict[str, object]] = []

    def news_count_for_exact_window_as_of(
        self, **kwargs: object
    ) -> PointInTimeAttentionObservation | None:
        self.news_calls.append(kwargs)
        return self.news

    def latest_analyst_coverage_as_of(
        self, **kwargs: object
    ) -> PointInTimeAttentionObservation | None:
        self.analyst_calls.append(kwargs)
        return self.analyst


def _source(external_id: str) -> SourceRecordView:
    return SourceRecordView(
        id=UUID(int=int(sha256(external_id.encode()).hexdigest()[:32], 16)),
        external_record_id=external_id,
        source_uri=f"synthetic://attention/{external_id}",
        raw_object_key=f"sha256/{sha256(external_id.encode()).hexdigest()}",
        raw_payload_reference=f"record:{external_id}",
        content_sha256=sha256(f"content:{external_id}".encode()).hexdigest(),
        validation_status="accepted",
    )


def _news(
    count: int = 4,
    *,
    coverage: str = "complete",
    provider_dataset_id: UUID = NEWS_PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    security_id: UUID | None = None,
    available_at: datetime = AVAILABLE,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
) -> PointInTimeAttentionObservation:
    return PointInTimeAttentionObservation(
        id=UUID("52345678-4321-4234-8234-123456789abc"),
        company_id=company_id,
        security_id=security_id,
        provider_dataset_id=provider_dataset_id,
        metric_code="news_mentions_count",
        reported_count=count,
        reported_unit="count",
        scope_code=NEWS_SERIES.scope_code,
        methodology_version=NEWS_SERIES.methodology_version,
        measurement_definition_sha256=NEWS_SERIES.measurement_definition_sha256,
        coverage_status=coverage,
        observation_date=None,
        window_start_at=window_start,
        window_end_at=window_end,
        available_at=available_at,
        revision_at=None,
        ingested_at=available_at + timedelta(hours=2),
        source_record=_source("NEWS"),
    )


def _analyst(
    count: int = 5,
    *,
    coverage: str = "complete",
    provider_dataset_id: UUID = ANALYST_PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    security_id: UUID | None = None,
    available_at: datetime = AVAILABLE,
    observation_date: date = date(2026, 9, 1),
) -> PointInTimeAttentionObservation:
    return PointInTimeAttentionObservation(
        id=UUID("62345678-4321-4234-8234-123456789abc"),
        company_id=company_id,
        security_id=security_id,
        provider_dataset_id=provider_dataset_id,
        metric_code="analyst_coverage_count",
        reported_count=count,
        reported_unit="count",
        scope_code=ANALYST_SERIES.scope_code,
        methodology_version=ANALYST_SERIES.methodology_version,
        measurement_definition_sha256=ANALYST_SERIES.measurement_definition_sha256,
        coverage_status=coverage,
        observation_date=observation_date,
        window_start_at=None,
        window_end_at=None,
        available_at=available_at,
        revision_at=None,
        ingested_at=available_at + timedelta(hours=2),
        source_record=_source("ANALYST"),
    )


def _features(
    reader: _StubAttentionReader,
    *,
    as_of: datetime = AS_OF,
    security_id: UUID | None = None,
    company_level_only: bool = True,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
):
    return AttentionFeaturePrimitives(cast(PointInTimeAttentionReader, reader)).features_as_of(
        company_id=COMPANY_ID,
        security_id=security_id,
        company_level_only=company_level_only,
        as_of=as_of,
        news_series=NEWS_SERIES,
        news_window_start_at=window_start,
        news_window_end_at=window_end,
        analyst_series=ANALYST_SERIES,
        analyst_observation_on_or_before=None,
    )


def test_series_identity_and_input_contract_are_explicit() -> None:
    identity = AttentionSeriesIdentity(
        provider_dataset_id=NEWS_PROVIDER_ID,
        scope_code="scope",
        methodology_version="method_v1",
        measurement_definition_sha256="ABCDEF" * 10 + "ABCD",
    )
    assert identity.measurement_definition_sha256 == ("abcdef" * 10 + "abcd")
    for kwargs in (
        {"scope_code": " scope"},
        {"methodology_version": ""},
        {"measurement_definition_sha256": "bad"},
    ):
        values = {
            "provider_dataset_id": NEWS_PROVIDER_ID,
            "scope_code": "scope",
            "methodology_version": "method_v1",
            "measurement_definition_sha256": NEWS_DEFINITION,
            **kwargs,
        }
        with pytest.raises(ValueError):
            AttentionSeriesIdentity(**values)

    reader = _StubAttentionReader(_news(), _analyst())
    with pytest.raises(ValueError, match="company-level"):
        _features(reader, security_id=SECURITY_ID, company_level_only=True)
    with pytest.raises(ValueError, match="company-level"):
        _features(reader, security_id=None, company_level_only=False)
    with pytest.raises(ValueError, match="timezone-aware"):
        _features(reader, as_of=AS_OF.replace(tzinfo=None))
    with pytest.raises(ValueError, match="before"):
        _features(reader, window_start=WINDOW_END, window_end=WINDOW_START)


def test_complete_counts_exact_time_features_versions_and_lineage() -> None:
    news = _news(count=0)
    analyst = _analyst(count=0)
    reader = _StubAttentionReader(news, analyst)
    bundle = _features(
        reader,
        as_of=datetime(2026, 9, 30, 23, 59, tzinfo=UTC),
    )

    assert bundle.algorithm_version == ATTENTION_FEATURE_BUNDLE_VERSION
    assert bundle.news_mentions_count.value == Decimal("0")
    assert bundle.news_mentions_count.warnings == ()
    assert bundle.news_mentions_count.algorithm_version == (
        NEWS_MENTIONS_COUNT_FEATURE_VERSION
    )
    assert bundle.news_window_duration_days.value == Decimal("30.5")
    assert bundle.news_window_duration_days.algorithm_version == (
        NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION
    )
    assert bundle.news_window_age_days.value == Decimal("29.99930555555555555555555556")
    assert bundle.news_window_age_days.algorithm_version == (
        NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION
    )
    assert bundle.analyst_coverage_count.value == Decimal("0")
    assert bundle.analyst_coverage_count.algorithm_version == (
        ANALYST_COVERAGE_COUNT_FEATURE_VERSION
    )
    assert bundle.analyst_snapshot_age_days.value == Decimal("29")
    assert bundle.analyst_snapshot_age_days.algorithm_version == (
        ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION
    )
    for feature in (
        bundle.news_mentions_count,
        bundle.news_window_duration_days,
        bundle.news_window_age_days,
    ):
        assert (feature.unit, feature.available_at, feature.evidence) == (
            "count" if feature.code == "news_mentions_count" else "days",
            AVAILABLE,
            (news,),
        )
    assert bundle.analyst_coverage_count.evidence == (analyst,)
    assert bundle.analyst_snapshot_age_days.evidence == (analyst,)
    assert reader.news_calls[0]["provider_dataset_id"] == NEWS_PROVIDER_ID
    assert reader.analyst_calls[0]["provider_dataset_id"] == ANALYST_PROVIDER_ID


def test_exact_news_age_and_microseconds_never_use_float() -> None:
    end = datetime(2026, 9, 1, tzinfo=UTC)
    bundle = _features(
        _StubAttentionReader(
            _news(window_start=end - timedelta(microseconds=1), window_end=end),
            _analyst(),
        ),
        window_start=end - timedelta(microseconds=1),
        window_end=end,
        as_of=end + timedelta(days=1, hours=12, microseconds=1),
    )
    exact_microsecond_day = Decimal(1) / Decimal(86_400_000_000)
    assert bundle.news_window_duration_days.value == exact_microsecond_day
    assert bundle.news_window_age_days.value == Decimal("1.5") + exact_microsecond_day
    assert isinstance(bundle.news_window_age_days.value, Decimal)


@pytest.mark.parametrize(
    ("coverage", "news_warning", "analyst_warning"),
    (
        (
            "partial",
            "partial_news_attention_coverage",
            "partial_analyst_attention_coverage",
        ),
        (
            "unknown",
            "unknown_news_attention_coverage",
            "unknown_analyst_attention_coverage",
        ),
    ),
)
def test_incomplete_zero_counts_are_unavailable_but_temporal_metadata_remains(
    coverage: str, news_warning: str, analyst_warning: str
) -> None:
    bundle = _features(
        _StubAttentionReader(
            _news(count=0, coverage=coverage),
            _analyst(count=0, coverage=coverage),
        )
    )
    assert bundle.news_mentions_count.value is None
    assert bundle.news_mentions_count.warnings == (news_warning,)
    assert bundle.analyst_coverage_count.value is None
    assert bundle.analyst_coverage_count.warnings == (analyst_warning,)
    assert bundle.news_window_duration_days.value == Decimal("30.5")
    assert bundle.news_window_age_days.value == Decimal("1.5")
    assert bundle.analyst_snapshot_age_days.value == Decimal("1")
    assert bundle.news_mentions_count.available_at == AVAILABLE
    assert bundle.analyst_coverage_count.available_at == AVAILABLE


def test_missing_observations_remain_missing_not_zero() -> None:
    bundle = _features(_StubAttentionReader(None, None))
    expected = (
        (bundle.news_mentions_count, "missing_news_mentions_observation", "count"),
        (bundle.news_window_duration_days, "missing_news_mentions_observation", "days"),
        (bundle.news_window_age_days, "missing_news_mentions_observation", "days"),
        (
            bundle.analyst_coverage_count,
            "missing_analyst_coverage_observation",
            "count",
        ),
        (
            bundle.analyst_snapshot_age_days,
            "missing_analyst_coverage_observation",
            "days",
        ),
    )
    for feature, warning, unit in expected:
        assert feature.value is None
        assert feature.unit == unit
        assert feature.available_at is None
        assert feature.evidence == ()
        assert feature.warnings == (warning,)


@pytest.mark.parametrize(
    "news",
    (
        _news(company_id=UUID("72345678-4321-4234-8234-123456789abc")),
        _news(provider_dataset_id=ANALYST_PROVIDER_ID),
        replace(_news(), metric_code="analyst_coverage_count"),
        replace(_news(), scope_code="different_scope"),
        replace(_news(), methodology_version="different_method"),
        replace(_news(), measurement_definition_sha256="c" * 64),
        replace(_news(), reported_unit="percent"),
        replace(_news(), coverage_status="global"),
        replace(_news(), source_record=replace(_source("NEWS"), validation_status="quarantined")),
        _news(available_at=AS_OF + timedelta(seconds=1)),
    ),
)
def test_news_observation_coherence_fails_closed(
    news: PointInTimeAttentionObservation,
) -> None:
    with pytest.raises(ValueError):
        _features(_StubAttentionReader(news, _analyst()))


def test_security_specific_mode_is_not_blended() -> None:
    news = _news(count=1, security_id=SECURITY_ID)
    analyst = _analyst(count=2, security_id=SECURITY_ID)
    bundle = _features(
        _StubAttentionReader(news, analyst),
        security_id=SECURITY_ID,
        company_level_only=False,
    )
    assert bundle.security_id == SECURITY_ID
    assert bundle.company_level_only is False
    assert bundle.news_mentions_count.value == Decimal("1")
    assert bundle.analyst_coverage_count.value == Decimal("2")


def _identities(session) -> None:
    company = Company(
        id=COMPANY_ID,
        legal_name="Fictional Attention Features Limited",
        display_name="Fictional Attention Features",
        sector="Synthetic",
        industry="Testing",
    )
    session.add(company)
    session.flush()
    session.add(
        Security(
            id=SECURITY_ID,
            company_id=COMPANY_ID,
            isin="INEATF000001",
            security_type="equity",
            status="inactive",
        )
    )
    session.commit()


def _ingestion_record(
    *,
    metric: str,
    count: int,
    coverage: str,
    observation_date: date | None = None,
) -> AttentionObservationRecord:
    news = metric == "news_mentions_count"
    return AttentionObservationRecord(
        company_legal_name="Fictional Attention Features Limited",
        security_isin=None,
        metric_code=metric,
        reported_count=count,
        reported_unit="count",
        scope_code=(NEWS_SERIES.scope_code if news else ANALYST_SERIES.scope_code),
        methodology_version=(
            NEWS_SERIES.methodology_version if news else ANALYST_SERIES.methodology_version
        ),
        measurement_definition_sha256=(
            NEWS_DEFINITION if news else ANALYST_DEFINITION
        ),
        coverage_status=coverage,
        observation_date=None if news else observation_date,
        window_start_at=WINDOW_START if news else None,
        window_end_at=WINDOW_END if news else None,
    )


def _envelope(
    external_id: str,
    record: AttentionObservationRecord,
    *,
    available_at: datetime,
    salt: str,
    metadata: ProviderMetadata,
) -> IngestionEnvelope[AttentionObservationRecord]:
    return IngestionEnvelope(
        provider=metadata,
        external_record_id=external_id,
        source_uri=f"synthetic://attention/{external_id}",
        raw_payload_reference=f"record:{external_id}:{salt}",
        content_sha256=sha256(f"{record}|{salt}".encode()).hexdigest(),
        retrieved_at=available_at + timedelta(hours=1),
        record=record,
        available_at=available_at,
    )


def _dataset_id(session, provider_code: str) -> UUID:
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(DataProvider.code == provider_code)
    )
    assert dataset_id is not None
    return dataset_id


def test_full_ingestion_pit_feature_pipeline_preserves_zero_corrections_and_lineage(
    session, tmp_path: Path
) -> None:
    _identities(session)
    archive_root = tmp_path / "raw"
    service = IngestionService(session, LocalRawObjectStore(archive_root))
    metadata = ProviderMetadata(
        "synthetic_attention_features",
        "synthetic",
        "external_attention",
        "synthetic-development-only",
    )
    t1 = datetime(2026, 9, 2, 8, tzinfo=UTC)
    t2 = datetime(2026, 9, 3, 8, tzinfo=UTC)
    t3 = datetime(2026, 9, 4, 8, tzinfo=UTC)
    initial = ProviderBatch(
        provider=metadata,
        source_uri="synthetic://attention/initial",
        raw_payload=b"fictional complete zero attention evidence",
        retrieved_at=t1 + timedelta(hours=1),
        records=(
            _envelope(
                "NEWS-ZERO",
                _ingestion_record(
                    metric="news_mentions_count", count=8, coverage="complete"
                ),
                available_at=t1,
                salt="initial",
                metadata=metadata,
            ),
            _envelope(
                "ANALYST-ZERO",
                _ingestion_record(
                    metric="analyst_coverage_count",
                    count=0,
                    coverage="complete",
                    observation_date=date(2026, 9, 1),
                ),
                available_at=t1,
                salt="initial",
                metadata=metadata,
            ),
        ),
    )
    service.ingest_attention_data(MockAttentionDataProvider(initial))
    dataset_id = _dataset_id(session, metadata.provider_code)
    series_news = replace(NEWS_SERIES, provider_dataset_id=dataset_id)
    series_analyst = replace(ANALYST_SERIES, provider_dataset_id=dataset_id)
    primitives = AttentionFeaturePrimitives(PointInTimeAttentionReader(session))
    before = primitives.features_as_of(
        company_id=COMPANY_ID,
        security_id=None,
        company_level_only=True,
        as_of=t1 + timedelta(minutes=1),
        news_series=series_news,
        news_window_start_at=WINDOW_START,
        news_window_end_at=WINDOW_END,
        analyst_series=series_analyst,
        analyst_observation_on_or_before=None,
    )
    assert before.news_mentions_count.value == Decimal("8")
    assert before.analyst_coverage_count.value == Decimal("0")
    assert before.news_observation is not None
    assert before.analyst_observation is not None
    assert (archive_root / before.news_observation.source_record.raw_object_key).exists()
    assert (archive_root / before.analyst_observation.source_record.raw_object_key).exists()

    correction = ProviderBatch(
        provider=metadata,
        source_uri="synthetic://attention/correction",
        raw_payload=b"fictional complete correction attention evidence",
        retrieved_at=t2 + timedelta(hours=1),
        records=(
            _envelope(
                "NEWS-ZERO",
                _ingestion_record(
                    metric="news_mentions_count", count=6, coverage="complete"
                ),
                available_at=t2,
                salt="correction",
                metadata=metadata,
            ),
            _envelope(
                "ANALYST-NEW",
                _ingestion_record(
                    metric="analyst_coverage_count",
                    count=0,
                    coverage="partial",
                    observation_date=date(2026, 9, 2),
                ),
                available_at=t2,
                salt="new-snapshot",
                metadata=metadata,
            ),
        ),
    )
    service.ingest_attention_data(MockAttentionDataProvider(correction))
    historical = primitives.features_as_of(
        company_id=COMPANY_ID,
        security_id=None,
        company_level_only=True,
        as_of=t1 + timedelta(minutes=1),
        news_series=series_news,
        news_window_start_at=WINDOW_START,
        news_window_end_at=WINDOW_END,
        analyst_series=series_analyst,
        analyst_observation_on_or_before=None,
    )
    corrected = primitives.features_as_of(
        company_id=COMPANY_ID,
        security_id=None,
        company_level_only=True,
        as_of=t2 + timedelta(minutes=1),
        news_series=series_news,
        news_window_start_at=WINDOW_START,
        news_window_end_at=WINDOW_END,
        analyst_series=series_analyst,
        analyst_observation_on_or_before=None,
    )
    assert historical.news_mentions_count.value == Decimal("8")
    assert corrected.news_mentions_count.value == Decimal("6")
    assert corrected.analyst_coverage_count.value is None
    assert corrected.analyst_coverage_count.warnings == (
        "partial_analyst_attention_coverage",
    )

    coverage_correction = ProviderBatch(
        provider=metadata,
        source_uri="synthetic://attention/coverage-correction",
        raw_payload=b"fictional partial coverage correction evidence",
        retrieved_at=t3 + timedelta(hours=1),
        records=(
            _envelope(
                "NEWS-ZERO",
                _ingestion_record(
                    metric="news_mentions_count", count=0, coverage="partial"
                ),
                available_at=t3,
                salt="coverage-correction",
                metadata=metadata,
            ),
        ),
    )
    service.ingest_attention_data(MockAttentionDataProvider(coverage_correction))
    after = primitives.features_as_of(
        company_id=COMPANY_ID,
        security_id=None,
        company_level_only=True,
        as_of=t3 + timedelta(minutes=1),
        news_series=series_news,
        news_window_start_at=WINDOW_START,
        news_window_end_at=WINDOW_END,
        analyst_series=series_analyst,
        analyst_observation_on_or_before=None,
    )
    assert after.news_mentions_count.value is None
    assert after.news_mentions_count.warnings == (
        "partial_news_attention_coverage",
    )
    assert after.analyst_coverage_count.value is None
    assert after.analyst_coverage_count.warnings == (
        "partial_analyst_attention_coverage",
    )
    assert after.news_window_duration_days.value == Decimal("30.5")
    assert after.news_observation is not None
    assert after.news_observation.id != before.news_observation.id


def test_timezone_equivalence_and_analyst_date_bound_are_forwarded() -> None:
    reader = _StubAttentionReader(_news(), _analyst())
    bound = date(2026, 9, 1)
    bundle = AttentionFeaturePrimitives(
        cast(PointInTimeAttentionReader, reader)
    ).features_as_of(
        company_id=COMPANY_ID,
        security_id=None,
        company_level_only=True,
        as_of=datetime(2026, 9, 2, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        news_series=NEWS_SERIES,
        news_window_start_at=WINDOW_START,
        news_window_end_at=WINDOW_END,
        analyst_series=ANALYST_SERIES,
        analyst_observation_on_or_before=bound,
    )
    assert bundle.as_of == AS_OF
    assert reader.analyst_calls[0]["observation_on_or_before"] == bound


def test_attention_feature_module_has_no_forbidden_domain_dependency() -> None:
    source = (
        Path(__file__).parents[1]
        / "packages/data/inflector_data/attention_features.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "PriceBar",
        "MarketStructureFeatureBundle",
        "BusinessEvent",
        "BusinessCatalystComponentScore",
        "InflectionScoringPolicy",
        "ScoreSnapshot",
    ):
        assert forbidden not in source
    # Attention still owns migration 0015; the later operations ledger is isolated in 0016.
    assert (
        Path(__file__).parents[1]
        / "migrations/versions/20261002_0016_operational_runs.py"
    ).exists()
