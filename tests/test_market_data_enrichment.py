"""Phase 2D-A raw market enrichment and benchmark ingestion tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import func, select

from inflector_core.providers import (
    BenchmarkBarRecord,
    IngestionEnvelope,
    MarketBarRecord,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import (
    CSVBenchmarkDataProvider,
    CSVMarketDataProvider,
    CSVUniverseProvider,
    MockBenchmarkDataProvider,
)
from inflector_data.service import IngestionService
from inflector_data.validation import validate_benchmark, validate_price
from inflector_database.models import (
    BenchmarkBar,
    BenchmarkSeries,
    DataQualityIssue,
    IngestionRun,
    PriceBar,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 1, 7, 20, tzinfo=UTC)
UNIVERSE_METADATA = ProviderMetadata(
    "market_enrichment_synthetic",
    "csv",
    "universe",
    "synthetic-development-only",
)
MARKET_METADATA = ProviderMetadata(
    "market_enrichment_synthetic",
    "csv",
    "market_daily",
    "synthetic-development-only",
)
BENCHMARK_METADATA = ProviderMetadata(
    "benchmark_synthetic",
    "csv",
    "benchmark_daily",
    "synthetic-development-only",
)


def _service_with_universe(session, tmp_path: Path) -> IngestionService:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_synthetic.csv",
            UNIVERSE_METADATA,
            RETRIEVED_AT,
        )
    )
    assert result.records_accepted == 3
    return service


def test_market_enrichment_round_trips_exactly_and_corrections_append(
    session,
    tmp_path: Path,
) -> None:
    service = _service_with_universe(session, tmp_path)
    original = service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_enriched_synthetic.csv",
            MARKET_METADATA,
            RETRIEVED_AT,
        )
    )
    corrected = service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_enriched_correction_synthetic.csv",
            MARKET_METADATA,
            RETRIEVED_AT,
        )
    )

    bars = tuple(session.scalars(select(PriceBar).order_by(PriceBar.available_at)))
    assert original.records_accepted == corrected.records_accepted == 1
    assert len(bars) == 2
    assert [bar.market_cap for bar in bars] == [
        Decimal("1234567890.25"),
        Decimal("1234567999.25"),
    ]
    assert [bar.delivery_quantity for bar in bars] == [250000, 275000]
    assert [bar.delivery_percentage for bar in bars] == [
        Decimal("0.375"),
        Decimal("0.40"),
    ]
    assert bars[0].source_record_id != bars[1].source_record_id
    assert bars[1].revision_at is not None
    hashes = tuple(
        session.scalars(
            select(SourceRecord.content_sha256)
            .where(SourceRecord.external_record_id == "SYN-MKT-ENRICHED-001")
            .order_by(SourceRecord.available_at)
        )
    )
    assert len(hashes) == 2 and hashes[0] != hashes[1]


def test_legacy_positional_market_record_keeps_parse_error_position() -> None:
    record = MarketBarRecord(
        "INF0AUR01018",
        datetime(2026, 1, 5, tzinfo=UTC).date(),
        "1d",
        Decimal("99"),
        Decimal("102"),
        Decimal("98"),
        Decimal("100"),
        120000,
        ("legacy_parse_error",),
    )

    assert record.parse_errors == ("legacy_parse_error",)
    assert record.market_cap is None
    assert record.delivery_quantity is None
    assert record.delivery_percentage is None


def test_old_ohlcv_csv_without_enrichment_remains_valid(session, tmp_path: Path) -> None:
    service = _service_with_universe(session, tmp_path)
    result = service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_valid_synthetic.csv",
            MARKET_METADATA,
            RETRIEVED_AT,
        )
    )

    bar = session.scalar(select(PriceBar))
    assert result.records_accepted == 1 and bar is not None
    assert bar.market_cap is None
    assert bar.delivery_quantity is None
    assert bar.delivery_percentage is None


def test_enrichment_and_benchmark_values_reject_binary_floats() -> None:
    trading_date = datetime(2026, 1, 5, tzinfo=UTC).date()
    market = MarketBarRecord(
        security_isin="INF0AUR01018",
        trading_date=trading_date,
        interval="1d",
        open_price=Decimal("99"),
        high_price=Decimal("102"),
        low_price=Decimal("98"),
        close_price=Decimal("100"),
        volume=120000,
        market_cap=cast(Decimal, 1000.0),
        delivery_percentage=cast(Decimal, 0.42),
    )
    benchmark = BenchmarkBarRecord(
        benchmark_code="NIFTY50",
        benchmark_display_name="Nifty 50",
        currency="INR",
        trading_date=trading_date,
        interval="1d",
        open_value=cast(Decimal, 24900.0),
        high_value=Decimal("25100"),
        low_value=Decimal("24800"),
        close_value=Decimal("25000"),
    )

    assert {issue.rule_code for issue in validate_price(market)} >= {
        "invalid_market_cap_type",
        "invalid_delivery_percentage_type",
    }
    assert {issue.rule_code for issue in validate_benchmark(benchmark)} >= {
        "invalid_open_type"
    }


def test_invalid_market_enrichment_is_quarantined_without_partial_bars(
    session,
    tmp_path: Path,
) -> None:
    service = _service_with_universe(session, tmp_path)
    result = service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_enrichment_invalid_synthetic.csv",
            MARKET_METADATA,
            RETRIEVED_AT,
        )
    )

    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert (result.records_accepted, result.records_quarantined) == (0, 5)
    assert session.scalar(select(func.count()).select_from(PriceBar)) == 0
    assert {
        "negative_delivery_quantity",
        "invalid_delivery_percentage",
        "invalid_market_cap",
    }.issubset(rules)


def test_benchmark_archive_idempotency_revision_and_provenance(
    session,
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "raw"
    service = IngestionService(session, LocalRawObjectStore(archive_root))
    provider = CSVBenchmarkDataProvider(
        FIXTURES / "benchmark_valid_synthetic.csv",
        BENCHMARK_METADATA,
        RETRIEVED_AT,
    )

    first = service.ingest_benchmark_data(provider)
    duplicate = service.ingest_benchmark_data(provider)
    correction = service.ingest_benchmark_data(
        CSVBenchmarkDataProvider(
            FIXTURES / "benchmark_correction_synthetic.csv",
            BENCHMARK_METADATA,
            RETRIEVED_AT,
        )
    )

    series = tuple(session.scalars(select(BenchmarkSeries)))
    bars = tuple(session.scalars(select(BenchmarkBar).order_by(BenchmarkBar.available_at)))
    sources = tuple(
        session.scalars(
            select(SourceRecord)
            .where(SourceRecord.external_record_id == "SYN-BENCH-001")
            .order_by(SourceRecord.available_at)
        )
    )
    assert first.records_accepted == 1
    assert duplicate.records_duplicated == 1
    assert correction.records_accepted == 1
    assert len(series) == 1
    assert (series[0].code, series[0].display_name, series[0].currency) == (
        "NIFTY50",
        "Nifty 50",
        "INR",
    )
    assert [bar.close_value for bar in bars] == [Decimal("25000"), Decimal("25100")]
    assert bars[0].source_record_id != bars[1].source_record_id
    assert bars[1].revision_at is not None
    assert len(sources) == 2
    assert all(
        source.source_uri == "file://benchmark_valid_synthetic.csv" for source in sources[:1]
    )
    assert all(source.raw_object_key and source.raw_payload_reference for source in sources)
    assert all(source.raw_content_sha256 and source.content_sha256 for source in sources)
    assert (archive_root / sources[0].raw_object_key).read_bytes() == (
        FIXTURES / "benchmark_valid_synthetic.csv"
    ).read_bytes()


def test_benchmark_identity_is_provider_dataset_local(session, tmp_path: Path) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    first_metadata = BENCHMARK_METADATA
    second_metadata = ProviderMetadata(
        "benchmark_synthetic_second",
        "csv",
        "benchmark_daily",
        "synthetic-development-only",
    )
    for metadata in (first_metadata, second_metadata):
        result = service.ingest_benchmark_data(
            CSVBenchmarkDataProvider(
                FIXTURES / "benchmark_valid_synthetic.csv",
                metadata,
                RETRIEVED_AT,
            )
        )
        assert result.records_accepted == 1

    series = tuple(session.scalars(select(BenchmarkSeries)))
    assert len(series) == 2
    assert {item.code for item in series} == {"NIFTY50"}
    assert len({item.provider_dataset_id for item in series}) == 2
    assert session.scalar(select(func.count()).select_from(BenchmarkBar)) == 2


def test_invalid_benchmark_rows_are_quarantined(session, tmp_path: Path) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_benchmark_data(
        CSVBenchmarkDataProvider(
            FIXTURES / "benchmark_invalid_synthetic.csv",
            BENCHMARK_METADATA,
            RETRIEVED_AT,
        )
    )

    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert (result.records_accepted, result.records_quarantined) == (0, 3)
    assert session.scalar(select(func.count()).select_from(BenchmarkSeries)) == 0
    assert session.scalar(select(func.count()).select_from(BenchmarkBar)) == 0
    assert {"invalid_currency", "open_outside_range", "missing_available_at"}.issubset(rules)


def test_mock_benchmark_provider_uses_common_batch_contract() -> None:
    raw = b"synthetic benchmark"
    record = BenchmarkBarRecord(
        benchmark_code="NIFTY50",
        benchmark_display_name="Nifty 50",
        currency="INR",
        trading_date=datetime(2026, 1, 5, tzinfo=UTC).date(),
        interval="1d",
        open_value=Decimal("24900"),
        high_value=Decimal("25100"),
        low_value=Decimal("24800"),
        close_value=Decimal("25000"),
    )
    envelope = IngestionEnvelope(
        provider=BENCHMARK_METADATA,
        external_record_id="mock-benchmark",
        source_uri="mock://benchmark",
        raw_payload_reference="record-1",
        content_sha256=sha256(raw).hexdigest(),
        retrieved_at=RETRIEVED_AT,
        available_at=datetime(2026, 1, 5, 18, 30, tzinfo=UTC),
        record=record,
    )
    batch = ProviderBatch(
        provider=BENCHMARK_METADATA,
        source_uri="mock://benchmark",
        raw_payload=raw,
        retrieved_at=RETRIEVED_AT,
        records=(envelope,),
    )

    assert MockBenchmarkDataProvider(batch).fetch_benchmark_data() is batch


def test_benchmark_persistence_failure_rolls_back_normalized_state(
    session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_root = tmp_path / "raw"
    service = IngestionService(session, LocalRawObjectStore(raw_root))

    def fail_add_benchmark_bar(**_: object) -> None:
        raise RuntimeError("benchmark persistence unavailable")

    monkeypatch.setattr(service._repository, "add_benchmark_bar", fail_add_benchmark_bar)
    with pytest.raises(RuntimeError, match="benchmark persistence unavailable"):
        service.ingest_benchmark_data(
            CSVBenchmarkDataProvider(
                FIXTURES / "benchmark_valid_synthetic.csv",
                BENCHMARK_METADATA,
                RETRIEVED_AT,
            )
        )

    run = session.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()))
    assert run is not None
    assert run.status == "failed"
    assert run.records_received == 1
    assert run.records_accepted == 0
    assert run.records_quarantined == 0
    assert session.scalar(select(func.count()).select_from(BenchmarkSeries)) == 0
    assert session.scalar(select(func.count()).select_from(BenchmarkBar)) == 0
    assert any(path.is_file() for path in raw_root.rglob("*"))
