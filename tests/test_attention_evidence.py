"""Phase 6D-A external-attention evidence and PIT acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from inflector_core.providers import (
    AttentionDataProvider,
    AttentionObservationRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.attention_pit import PointInTimeAttentionReader
from inflector_data.providers import CSVAttentionDataProvider, MockAttentionDataProvider
from inflector_data.service import IngestionService
from inflector_database.models import (
    AttentionObservation,
    Company,
    DataProvider,
    DataQualityIssue,
    IngestionRun,
    ProviderDataset,
    Security,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
COMPANY_ID = UUID("12345678-1234-4234-8234-123456789abc")
SECURITY_ID = UUID("22345678-1234-4234-8234-123456789abc")
OTHER_COMPANY_ID = UUID("32345678-1234-4234-8234-123456789abc")
OTHER_SECURITY_ID = UUID("42345678-1234-4234-8234-123456789abc")
RETRIEVED = datetime(2026, 9, 3, 10, tzinfo=UTC)
T1 = datetime(2026, 9, 2, 8, tzinfo=UTC)
T2 = datetime(2026, 9, 3, 8, tzinfo=UTC)
WINDOW_START = datetime(2026, 8, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 1, tzinfo=UTC)
DEFINITION = "a" * 64
METADATA = ProviderMetadata(
    "synthetic_attention",
    "synthetic_csv",
    "external_attention",
    "synthetic-development-only",
)


def _identities(session) -> None:
    company = Company(
        id=COMPANY_ID,
        legal_name="Fictional Attention Limited",
        display_name="Fictional Attention",
        sector="Synthetic",
        industry="Testing",
    )
    other = Company(
        id=OTHER_COMPANY_ID,
        legal_name="Fictional Other Limited",
        display_name="Fictional Other",
        sector="Synthetic",
        industry="Testing",
    )
    session.add_all((company, other))
    session.flush()
    session.add_all(
        (
            Security(
                id=SECURITY_ID,
                company_id=COMPANY_ID,
                isin="INEATT000001",
                security_type="equity",
                status="inactive",
            ),
            Security(
                id=OTHER_SECURITY_ID,
                company_id=OTHER_COMPANY_ID,
                isin="INEATT000002",
                security_type="equity",
                status="inactive",
            ),
        )
    )
    session.commit()


def _record(
    count: int | None,
    *,
    metric: str = "news_mentions_count",
    coverage: str = "complete",
    scope: str = "provider_indexed_business_news",
    methodology: str = "synthetic_news_search_v1",
    definition: str = DEFINITION,
    company: str | None = "Fictional Attention Limited",
    isin: str | None = None,
) -> AttentionObservationRecord:
    is_news = metric == "news_mentions_count"
    return AttentionObservationRecord(
        company_legal_name=company,
        security_isin=isin,
        metric_code=metric,
        reported_count=count,
        reported_unit="count",
        scope_code=scope,
        methodology_version=methodology,
        measurement_definition_sha256=definition,
        coverage_status=coverage,
        observation_date=None if is_news else date(2026, 9, 1),
        window_start_at=WINDOW_START if is_news else None,
        window_end_at=WINDOW_END if is_news else None,
    )


def _envelope(
    external_id: str,
    record: AttentionObservationRecord,
    *,
    available_at: datetime | None = T1,
    revision_at: datetime | None = None,
    salt: str = "",
    metadata: ProviderMetadata = METADATA,
) -> IngestionEnvelope[AttentionObservationRecord]:
    digest = sha256(f"{external_id}|{record}|{salt}".encode()).hexdigest()
    return IngestionEnvelope(
        provider=metadata,
        external_record_id=external_id,
        source_uri=f"synthetic://attention/{external_id}",
        raw_payload_reference=f"record:{external_id}:{salt}",
        content_sha256=digest,
        retrieved_at=RETRIEVED,
        record=record,
        available_at=available_at,
        revision_at=revision_at,
    )


def _batch(
    *envelopes: IngestionEnvelope[AttentionObservationRecord],
    metadata: ProviderMetadata = METADATA,
    payload: bytes = b"fictional external attention evidence",
) -> ProviderBatch[AttentionObservationRecord]:
    return ProviderBatch(
        provider=metadata,
        source_uri="synthetic://attention/batch",
        raw_payload=payload,
        retrieved_at=RETRIEVED,
        records=tuple(envelopes),
    )


def _dataset_id(session, provider_code: str = "synthetic_attention") -> UUID:
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(DataProvider.code == provider_code)
    )
    assert dataset_id is not None
    return dataset_id


def _reader_news(reader: PointInTimeAttentionReader, dataset_id: UUID, as_of: datetime):
    return reader.news_count_for_exact_window_as_of(
        provider_dataset_id=dataset_id,
        company_id=COMPANY_ID,
        scope_code="provider_indexed_business_news",
        methodology_version="synthetic_news_search_v1",
        measurement_definition_sha256=DEFINITION,
        window_start_at=WINDOW_START,
        window_end_at=WINDOW_END,
        as_of=as_of,
        security_id=None,
        company_level_only=True,
    )


def test_csv_provider_archive_first_idempotency_and_zero_coverage_states(
    session, tmp_path: Path
) -> None:
    _identities(session)
    archive = LocalRawObjectStore(tmp_path / "raw")
    service = IngestionService(session, archive)
    provider = CSVAttentionDataProvider(FIXTURES / "attention_synthetic.csv", METADATA, RETRIEVED)
    port: AttentionDataProvider = provider

    first = service.ingest_attention_data(port)
    duplicate = service.ingest_attention_data(provider)

    assert (first.records_received, first.records_accepted) == (6, 6)
    assert duplicate.records_duplicated == 6
    assert session.scalar(select(func.count()).select_from(AttentionObservation)) == 6
    assert session.scalar(select(func.count()).select_from(SourceRecord)) == 6
    observations = list(session.scalars(select(AttentionObservation)))
    assert {(item.reported_count, item.coverage_status) for item in observations} >= {
        (0, "complete"),
        (3, "partial"),
        (2, "unknown"),
    }
    sources = list(session.scalars(select(SourceRecord)))
    assert all((tmp_path / "raw" / item.raw_object_key).exists() for item in sources)


def test_attention_persistence_failure_reports_only_durable_counters(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))

    def fail_observation(**_kwargs: object) -> None:
        raise RuntimeError("synthetic attention persistence failure")

    monkeypatch.setattr(service._repository, "add_attention_observation", fail_observation)
    with pytest.raises(RuntimeError, match="synthetic attention persistence failure"):
        service.ingest_attention_data(
            MockAttentionDataProvider(_batch(_envelope("FAIL", _record(8))))
        )
    run = session.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()))
    assert run is not None
    assert run.status == "failed"
    assert (run.records_received, run.records_accepted, run.records_quarantined) == (
        1,
        0,
        0,
    )
    assert session.scalar(select(func.count()).select_from(AttentionObservation)) == 0


def test_news_and_analyst_corrections_are_append_only_and_pit_selected(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    news_old = _envelope("NEWS-REV", _record(8), salt="old")
    analyst_old = _envelope(
        "ANALYST-REV",
        _record(
            5,
            metric="analyst_coverage_count",
            scope="provider_active_equity_analysts",
            methodology="synthetic_analyst_coverage_v1",
        ),
        salt="old",
    )
    service.ingest_attention_data(
        MockAttentionDataProvider(_batch(news_old, analyst_old, payload=b"old"))
    )
    news_new = _envelope("NEWS-REV", _record(6), available_at=T2, salt="new")
    analyst_new = _envelope(
        "ANALYST-REV-NEW-ID",
        replace(analyst_old.record, reported_count=4),
        available_at=T2,
        salt="new",
    )
    service.ingest_attention_data(
        MockAttentionDataProvider(_batch(news_new, analyst_new, payload=b"new"))
    )

    dataset_id = _dataset_id(session)
    reader = PointInTimeAttentionReader(session)
    early_news = _reader_news(reader, dataset_id, T1 + timedelta(hours=1))
    late_news = _reader_news(reader, dataset_id, T2 + timedelta(hours=1))
    assert early_news is not None and early_news.reported_count == 8
    assert late_news is not None and late_news.reported_count == 6
    early = reader.latest_analyst_coverage_as_of(
        provider_dataset_id=dataset_id,
        company_id=COMPANY_ID,
        scope_code="provider_active_equity_analysts",
        methodology_version="synthetic_analyst_coverage_v1",
        measurement_definition_sha256=DEFINITION,
        as_of=T1 + timedelta(hours=1),
        security_id=None,
        company_level_only=True,
    )
    late = reader.latest_analyst_coverage_as_of(
        provider_dataset_id=dataset_id,
        company_id=COMPANY_ID,
        scope_code="provider_active_equity_analysts",
        methodology_version="synthetic_analyst_coverage_v1",
        measurement_definition_sha256=DEFINITION,
        as_of=T2 + timedelta(hours=1),
        security_id=None,
        company_level_only=True,
    )
    assert early is not None and early.reported_count == 5
    assert late is not None and late.reported_count == 4
    assert session.scalar(select(func.count()).select_from(AttentionObservation)) == 4


def test_methodology_scope_provider_and_security_series_stay_isolated(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    rows = (
        _envelope("A", _record(8)),
        _envelope("B", _record(5, methodology="synthetic_news_search_v2")),
        _envelope("C", _record(4, scope="publisher_subset")),
        _envelope("D", _record(3, company=None, isin="INEATT000001")),
    )
    service.ingest_attention_data(MockAttentionDataProvider(_batch(*rows)))
    second = ProviderMetadata(
        "synthetic_attention_two", "synthetic", "external_attention", "synthetic"
    )
    service.ingest_attention_data(
        MockAttentionDataProvider(
            _batch(
                _envelope("A", _record(1), metadata=second),
                metadata=second,
                payload=b"second provider",
            )
        )
    )
    reader = PointInTimeAttentionReader(session)
    first_dataset = _dataset_id(session)
    first = reader.observations_as_of(
        provider_dataset_id=first_dataset,
        company_id=COMPANY_ID,
        metric_code="news_mentions_count",
        scope_code="provider_indexed_business_news",
        methodology_version="synthetic_news_search_v1",
        measurement_definition_sha256=DEFINITION,
        as_of=T2,
        security_id=None,
        company_level_only=True,
    )
    security_only = reader.observations_as_of(
        provider_dataset_id=first_dataset,
        company_id=COMPANY_ID,
        metric_code="news_mentions_count",
        scope_code="provider_indexed_business_news",
        methodology_version="synthetic_news_search_v1",
        measurement_definition_sha256=DEFINITION,
        as_of=T2,
        security_id=SECURITY_ID,
        company_level_only=False,
    )
    assert [item.reported_count for item in first] == [8]
    assert [item.reported_count for item in security_only] == [3]
    with pytest.raises(ValueError, match="company-level"):
        reader.observations_as_of(
            provider_dataset_id=first_dataset,
            company_id=COMPANY_ID,
            metric_code="news_mentions_count",
            scope_code="provider_indexed_business_news",
            methodology_version="synthetic_news_search_v1",
            measurement_definition_sha256=DEFINITION,
            as_of=T2,
            security_id=None,
            company_level_only=False,
        )


@pytest.mark.parametrize("coverage", ("complete", "partial", "unknown"))
def test_zero_remains_an_explicit_observation_but_missing_is_none(
    session, tmp_path: Path, coverage: str
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_attention_data(
        MockAttentionDataProvider(_batch(_envelope("ZERO", _record(0, coverage=coverage))))
    )
    reader = PointInTimeAttentionReader(session)
    dataset_id = _dataset_id(session)
    selected = _reader_news(reader, dataset_id, T2)
    assert selected is not None
    assert (selected.reported_count, selected.coverage_status) == (0, coverage)
    missing = reader.news_count_for_exact_window_as_of(
        provider_dataset_id=dataset_id,
        company_id=COMPANY_ID,
        scope_code="provider_indexed_business_news",
        methodology_version="synthetic_news_search_v1",
        measurement_definition_sha256=DEFINITION,
        window_start_at=WINDOW_START - timedelta(days=1),
        window_end_at=WINDOW_END,
        as_of=T2,
        security_id=None,
        company_level_only=True,
    )
    assert missing is None


def test_availability_is_not_invented_from_retrieval_or_observation_date(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    analyst = _record(
        5,
        metric="analyst_coverage_count",
        scope="provider_active_equity_analysts",
        methodology="synthetic_analyst_coverage_v1",
    )
    result = service.ingest_attention_data(
        MockAttentionDataProvider(
            _batch(
                _envelope("NO-NEWS-AVAILABLE", _record(8), available_at=None),
                _envelope("NO-ANALYST-AVAILABLE", analyst, available_at=None),
            )
        )
    )
    assert result.records_quarantined == 2
    assert session.scalar(select(func.count()).select_from(AttentionObservation)) == 0
    issue_codes = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert "missing_attention_available_at" in issue_codes


def test_invalid_records_and_company_security_mismatch_are_quarantined(
    session, tmp_path: Path
) -> None:
    _identities(session)
    invalid_news = replace(
        _record(-1),
        reported_unit="percent",
        scope_code=" untrimmed ",
        measurement_definition_sha256="bad",
        coverage_status="global",
        observation_date=date(2026, 9, 1),
    )
    mismatch = _record(1, isin="INEATT000002")
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    outcome = service.ingest_attention_data(
        MockAttentionDataProvider(
            _batch(
                _envelope("INVALID", invalid_news),
                _envelope("MISMATCH", mismatch),
            )
        )
    )
    assert outcome.records_quarantined == 2
    codes = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert {
        "negative_attention_count",
        "invalid_attention_unit",
        "invalid_attention_scope",
        "invalid_measurement_definition_sha256",
        "invalid_attention_coverage_status",
        "invalid_news_observation_date",
        "attention_company_security_mismatch",
    }.issubset(codes)


def test_same_or_earlier_changed_revision_is_quarantined(session, tmp_path: Path) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_attention_data(
        MockAttentionDataProvider(_batch(_envelope("REV", _record(8), salt="old")))
    )
    outcome = service.ingest_attention_data(
        MockAttentionDataProvider(
            _batch(
                _envelope("NEW-ID", _record(6), salt="changed"),
                payload=b"ambiguous correction",
            )
        )
    )
    assert outcome.records_quarantined == 1
    assert list(session.scalars(select(AttentionObservation.reported_count))) == [8]
    assert "ambiguous_attention_revision" in set(
        session.scalars(select(DataQualityIssue.rule_code))
    )


def test_latest_external_revision_does_not_resurrect_an_old_series(session, tmp_path: Path) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_attention_data(
        MockAttentionDataProvider(_batch(_envelope("MOVED", _record(8), salt="v1")))
    )
    service.ingest_attention_data(
        MockAttentionDataProvider(
            _batch(
                _envelope(
                    "MOVED",
                    _record(5, methodology="synthetic_news_search_v2"),
                    available_at=T2,
                    salt="v2",
                ),
                payload=b"methodology revision",
            )
        )
    )
    reader = PointInTimeAttentionReader(session)
    dataset_id = _dataset_id(session)
    old_series = reader.observations_as_of(
        provider_dataset_id=dataset_id,
        company_id=COMPANY_ID,
        metric_code="news_mentions_count",
        scope_code="provider_indexed_business_news",
        methodology_version="synthetic_news_search_v1",
        measurement_definition_sha256=DEFINITION,
        as_of=T2 + timedelta(hours=1),
        security_id=None,
        company_level_only=True,
    )
    new_series = reader.observations_as_of(
        provider_dataset_id=dataset_id,
        company_id=COMPANY_ID,
        metric_code="news_mentions_count",
        scope_code="provider_indexed_business_news",
        methodology_version="synthetic_news_search_v2",
        measurement_definition_sha256=DEFINITION,
        as_of=T2 + timedelta(hours=1),
        security_id=None,
        company_level_only=True,
    )
    assert old_series == ()
    assert [item.reported_count for item in new_series] == [5]


def test_visibility_begins_at_available_at_not_window_end(session, tmp_path: Path) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_attention_data(
        MockAttentionDataProvider(_batch(_envelope("VISIBILITY", _record(8))))
    )
    reader = PointInTimeAttentionReader(session)
    dataset_id = _dataset_id(session)
    assert _reader_news(reader, dataset_id, T1 - timedelta(microseconds=1)) is None
    visible = _reader_news(reader, dataset_id, T1)
    assert visible is not None and visible.reported_count == 8


def test_attention_modules_have_no_market_event_or_scoring_dependency() -> None:
    sources = "\n".join(
        (Path(__file__).parents[1] / path).read_text(encoding="utf-8")
        for path in (
            "packages/data/inflector_data/attention.py",
            "packages/data/inflector_data/attention_pit.py",
        )
    )
    for forbidden in (
        "PriceBar",
        "MarketStructureFeatureBundle",
        "BusinessEvent",
        "BusinessCatalystComponentScore",
        "InflectionScoringPolicy",
    ):
        assert forbidden not in sources
