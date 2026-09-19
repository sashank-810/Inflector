"""Phase 3G-A point-in-time market and benchmark read tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.market_pit import MarketDataIntegrityError, PointInTimeMarketReader
from inflector_data.providers import (
    CSVBenchmarkDataProvider,
    CSVMarketDataProvider,
    CSVUniverseProvider,
)
from inflector_data.service import IngestionService
from inflector_database.models import (
    BenchmarkBar,
    BenchmarkSeries,
    DataProvider,
    PriceBar,
    ProviderDataset,
    Security,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 2, 2, 12, tzinfo=UTC)
T1 = datetime(2026, 1, 5, 18, tzinfo=UTC)
T2 = datetime(2026, 1, 8, 18, tzinfo=UTC)
T3 = datetime(2026, 1, 9, 18, tzinfo=UTC)
UNIVERSE = ProviderMetadata(
    "market_pit_provider_1", "csv", "universe", "synthetic-development-only"
)
MARKET_P1 = ProviderMetadata(
    "market_pit_provider_1", "csv", "market_daily", "synthetic-development-only"
)
MARKET_P2 = ProviderMetadata(
    "market_pit_provider_2", "csv", "market_daily", "synthetic-development-only"
)
BENCHMARK_P1 = ProviderMetadata(
    "benchmark_pit_provider_1", "csv", "benchmark_daily", "synthetic-development-only"
)
BENCHMARK_P2 = ProviderMetadata(
    "benchmark_pit_provider_2", "csv", "benchmark_daily", "synthetic-development-only"
)


def _dataset_id(session, provider_code: str, dataset_code: str) -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(DataProvider.code == provider_code, ProviderDataset.code == dataset_code)
    )
    assert value is not None
    return value


def _market_setup(session, tmp_path: Path, *, corrections: bool = True):
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    assert (
        service.ingest_universe(
            CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
        ).records_accepted
        == 3
    )
    assert (
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "market_pit_original_synthetic.csv", MARKET_P1, RETRIEVED_AT
            )
        ).records_accepted
        == 2
    )
    if corrections:
        assert (
            service.ingest_market_data(
                CSVMarketDataProvider(
                    FIXTURES / "market_pit_correction_synthetic.csv",
                    MARKET_P1,
                    RETRIEVED_AT,
                )
            ).records_accepted
            == 1
        )
    security_id = session.scalar(select(Security.id).where(Security.isin == "INF0AUR01018"))
    assert security_id is not None
    return (
        service,
        security_id,
        _dataset_id(session, MARKET_P1.provider_code, MARKET_P1.dataset_code),
    )


def _benchmark_setup(session, tmp_path: Path, *, correction: bool = True):
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    assert (
        service.ingest_benchmark_data(
            CSVBenchmarkDataProvider(
                FIXTURES / "benchmark_pit_original_synthetic.csv",
                BENCHMARK_P1,
                RETRIEVED_AT,
            )
        ).records_accepted
        == 2
    )
    if correction:
        assert (
            service.ingest_benchmark_data(
                CSVBenchmarkDataProvider(
                    FIXTURES / "benchmark_pit_correction_synthetic.csv",
                    BENCHMARK_P1,
                    RETRIEVED_AT,
                )
            ).records_accepted
            == 1
        )
    return service, _dataset_id(
        session, BENCHMARK_P1.provider_code, BENCHMARK_P1.dataset_code
    )


def test_market_exact_cutoff_correction_provider_interval_and_provenance(
    session, tmp_path: Path
) -> None:
    service, security_id, p1 = _market_setup(session, tmp_path)
    assert (
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "market_pit_provider2_synthetic.csv", MARKET_P2, RETRIEVED_AT
            )
        ).records_accepted
        == 1
    )
    p2 = _dataset_id(session, MARKET_P2.provider_code, MARKET_P2.dataset_code)
    security = session.get(Security, security_id)
    assert security is not None
    security.status = "inactive"
    session.flush()
    reader = PointInTimeMarketReader(session)
    def read_market(cutoff: datetime, interval: str = "1d"):
        return reader.market_bar_as_of(
            provider_dataset_id=p1,
            security_id=security_id,
            trading_date=date(2026, 1, 5),
            interval=interval,
            as_of=cutoff,
        )

    assert read_market(T1 - timedelta(microseconds=1)) is None
    at_t1 = read_market(T1)
    between = read_market(T2 - timedelta(microseconds=1))
    at_t2 = read_market(T2)
    assert at_t1 is not None and between is not None and at_t2 is not None
    assert (at_t1.close_price, between.close_price, at_t2.close_price) == (
        Decimal("100.000000"),
        Decimal("100.000000"),
        Decimal("101.000000"),
    )
    assert at_t1.market_cap == Decimal("1000")
    assert at_t2.market_cap == Decimal("1100")
    assert at_t2.delivery_percentage == Decimal("0.41")
    assert at_t2.available_at == T2
    assert at_t2.source_record.validation_status == "accepted"
    assert at_t2.source_record.external_record_id == "SYN-MKT-PIT-D1"
    assert at_t2.source_record.source_uri.endswith("market_pit_correction_synthetic.csv")
    assert at_t2.source_record.raw_object_key
    assert at_t2.source_record.raw_payload_reference == "row-1"
    assert len(at_t2.source_record.content_sha256) == 64
    with pytest.raises(FrozenInstanceError):
        at_t2.volume = 0  # type: ignore[misc]

    provider_two = reader.market_bar_as_of(
        provider_dataset_id=p2,
        security_id=security_id,
        trading_date=date(2026, 1, 5),
        interval="1d",
        as_of=T2,
    )
    assert provider_two is not None and provider_two.close_price == Decimal("200.000000")
    assert read_market(T2, "1h") is None


def test_market_series_replaces_correction_orders_filters_and_does_not_fill_gaps(
    session, tmp_path: Path
) -> None:
    _, security_id, dataset_id = _market_setup(session, tmp_path)
    reader = PointInTimeMarketReader(session)

    before = reader.market_series_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        interval="1d",
        as_of=T2 - timedelta(microseconds=1),
    )
    after = reader.market_series_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        interval="1d",
        as_of=T2,
    )
    assert [bar.trading_date for bar in before] == [date(2026, 1, 5), date(2026, 1, 7)]
    assert [bar.close_price for bar in before] == [Decimal("100.000000"), Decimal("105.000000")]
    assert [bar.trading_date for bar in after] == [date(2026, 1, 5), date(2026, 1, 7)]
    assert [bar.close_price for bar in after] == [Decimal("101.000000"), Decimal("105.000000")]
    assert len({bar.trading_date for bar in after}) == len(after) == 2

    one_day = reader.market_series_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        interval="1d",
        as_of=T2,
        start_date=date(2026, 1, 5),
        end_date=date(2026, 1, 5),
    )
    assert [bar.trading_date for bar in one_day] == [date(2026, 1, 5)]
    with pytest.raises(ValueError, match="start_date"):
        reader.market_series_as_of(
            provider_dataset_id=dataset_id,
            security_id=security_id,
            interval="1d",
            as_of=T2,
            start_date=date(2026, 1, 7),
            end_date=date(2026, 1, 5),
        )


def test_latest_market_uses_economic_date_and_honors_on_or_before(
    session, tmp_path: Path
) -> None:
    service, security_id, dataset_id = _market_setup(session, tmp_path)
    assert (
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "market_pit_null_correction_synthetic.csv",
                MARKET_P1,
                RETRIEVED_AT,
            )
        ).records_accepted
        == 1
    )
    reader = PointInTimeMarketReader(session)

    latest = reader.latest_market_bar_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        interval="1d",
        as_of=T3,
    )
    older = reader.latest_market_bar_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        interval="1d",
        as_of=T3,
        on_or_before=date(2026, 1, 5),
    )
    assert latest is not None and latest.trading_date == date(2026, 1, 7)
    assert older is not None and older.trading_date == date(2026, 1, 5)
    assert older.close_price == Decimal("102.000000")


def test_atomic_market_revision_keeps_corrected_nulls_and_uses_available_at_only(
    session, tmp_path: Path
) -> None:
    service, security_id, dataset_id = _market_setup(session, tmp_path)
    assert (
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "market_pit_null_correction_synthetic.csv",
                MARKET_P1,
                RETRIEVED_AT,
            )
        ).records_accepted
        == 1
    )
    latest_row = session.scalar(
        select(PriceBar).where(PriceBar.available_at == T3.replace(tzinfo=None))
    )
    assert latest_row is not None
    latest_row.ingested_at = datetime(2026, 3, 1, tzinfo=UTC)
    session.flush()

    selected = PointInTimeMarketReader(session).market_bar_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        trading_date=date(2026, 1, 5),
        interval="1d",
        as_of=T3,
    )
    assert selected is not None
    assert selected.close_price == Decimal("102.000000")
    assert selected.market_cap is None
    assert selected.delivery_quantity is None
    assert selected.delivery_percentage is None
    assert selected.revision_at == datetime(2026, 2, 1, tzinfo=UTC)
    assert selected.ingested_at == datetime(2026, 3, 1, tzinfo=UTC)


def test_market_revision_order_uses_revision_after_available_at(session, tmp_path: Path) -> None:
    _, security_id, dataset_id = _market_setup(session, tmp_path)
    rows = list(
        session.scalars(
            select(PriceBar)
            .where(PriceBar.trading_date == date(2026, 1, 5))
            .order_by(PriceBar.available_at)
        )
    )
    original, correction = rows
    original.available_at = correction.available_at
    original.revision_at = datetime(2026, 2, 2, tzinfo=UTC)
    original.ingested_at = datetime(2026, 1, 1, tzinfo=UTC)
    correction.ingested_at = datetime(2026, 3, 1, tzinfo=UTC)
    session.flush()

    selected = PointInTimeMarketReader(session).market_bar_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        trading_date=date(2026, 1, 5),
        interval="1d",
        as_of=T2,
    )
    assert selected is not None and selected.close_price == Decimal("100.000000")


def test_market_nonaccepted_source_is_excluded_and_missing_reads_are_empty(
    session, tmp_path: Path
) -> None:
    service, security_id, p1 = _market_setup(session, tmp_path, corrections=False)
    assert (
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "market_pit_provider2_synthetic.csv", MARKET_P2, RETRIEVED_AT
            )
        ).records_accepted
        == 1
    )
    p2 = _dataset_id(session, MARKET_P2.provider_code, MARKET_P2.dataset_code)
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.provider_dataset_id == p2)
    )
    assert source is not None
    source.validation_status = "parsed"
    session.flush()
    reader = PointInTimeMarketReader(session)

    assert (
        reader.market_bar_as_of(
            provider_dataset_id=p2,
            security_id=security_id,
            trading_date=date(2026, 1, 5),
            interval="1d",
            as_of=T3,
        )
        is None
    )
    assert (
        reader.market_series_as_of(
            provider_dataset_id=p2,
            security_id=security_id,
            interval="1d",
            as_of=T3,
        )
        == []
    )
    assert (
        reader.latest_market_bar_as_of(
            provider_dataset_id=p2,
            security_id=security_id,
            interval="1d",
            as_of=T3,
        )
        is None
    )
    assert reader.market_series_as_of(
        provider_dataset_id=p1,
        security_id=UUID(int=0),
        interval="1d",
        as_of=T3,
    ) == []


def test_market_time_normalization_and_structural_inputs(session, tmp_path: Path) -> None:
    _, security_id, dataset_id = _market_setup(session, tmp_path, corrections=False)
    reader = PointInTimeMarketReader(session)
    equivalent = datetime(2026, 1, 5, 23, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    selected = reader.market_bar_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        trading_date=date(2026, 1, 5),
        interval="1d",
        as_of=equivalent,
    )
    assert selected is not None and selected.available_at == T1
    with pytest.raises(ValueError, match="timezone-aware"):
        reader.market_series_as_of(
            provider_dataset_id=dataset_id,
            security_id=security_id,
            interval="1d",
            as_of=datetime(2026, 1, 5, 18),
        )
    with pytest.raises(ValueError, match="interval"):
        reader.latest_market_bar_as_of(
            provider_dataset_id=dataset_id,
            security_id=security_id,
            interval=" ",
            as_of=T1,
        )


def test_benchmark_cutoffs_provider_identity_series_latest_and_status_independence(
    session, tmp_path: Path
) -> None:
    service, p1 = _benchmark_setup(session, tmp_path)
    assert (
        service.ingest_benchmark_data(
            CSVBenchmarkDataProvider(
                FIXTURES / "benchmark_pit_provider2_synthetic.csv",
                BENCHMARK_P2,
                RETRIEVED_AT,
            )
        ).records_accepted
        == 1
    )
    p2 = _dataset_id(session, BENCHMARK_P2.provider_code, BENCHMARK_P2.dataset_code)
    series_identity = session.scalar(
        select(BenchmarkSeries).where(BenchmarkSeries.provider_dataset_id == p1)
    )
    assert series_identity is not None
    series_identity.status = "retired"
    session.flush()
    reader = PointInTimeMarketReader(session)
    def read_benchmark(cutoff: datetime):
        return reader.benchmark_bar_as_of(
            provider_dataset_id=p1,
            benchmark_code="NIFTY50",
            trading_date=date(2026, 1, 5),
            interval="1d",
            as_of=cutoff,
        )

    assert read_benchmark(T1) is None
    original = read_benchmark(datetime(2026, 1, 5, 18, 30, tzinfo=UTC))
    before_correction = read_benchmark(datetime(2026, 1, 8, 18, 29, 59, tzinfo=UTC))
    corrected = read_benchmark(datetime(2026, 1, 8, 18, 30, tzinfo=UTC))
    assert original is not None and before_correction is not None and corrected is not None
    assert (original.close_value, before_correction.close_value, corrected.close_value) == (
        Decimal("25000"),
        Decimal("25000"),
        Decimal("25100"),
    )
    assert corrected.revision_at == datetime(2026, 2, 1, tzinfo=UTC)
    assert corrected.benchmark_series.provider_dataset_id == p1
    assert corrected.benchmark_series.code == "NIFTY50"
    assert corrected.source_record.validation_status == "accepted"
    assert corrected.source_record.raw_object_key

    provider_two = reader.benchmark_bar_as_of(
        provider_dataset_id=p2,
        benchmark_code="NIFTY50",
        trading_date=date(2026, 1, 5),
        interval="1d",
        as_of=T2,
    )
    assert provider_two is not None and provider_two.close_value == Decimal("30000")

    selected_series = reader.benchmark_series_as_of(
        provider_dataset_id=p1,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 1, 8, 18, 30, tzinfo=UTC),
    )
    assert [bar.trading_date for bar in selected_series] == [
        date(2026, 1, 5),
        date(2026, 1, 7),
    ]
    assert [bar.close_value for bar in selected_series] == [Decimal("25100"), Decimal("25200")]
    latest = reader.latest_benchmark_bar_as_of(
        provider_dataset_id=p1,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 1, 8, 18, 30, tzinfo=UTC),
    )
    older = reader.latest_benchmark_bar_as_of(
        provider_dataset_id=p1,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 1, 8, 18, 30, tzinfo=UTC),
        on_or_before=date(2026, 1, 5),
    )
    assert latest is not None and latest.trading_date == date(2026, 1, 7)
    assert older is not None and older.close_value == Decimal("25100")


def test_benchmark_filters_unknown_inputs_and_utc_normalization(session, tmp_path: Path) -> None:
    _, dataset_id = _benchmark_setup(session, tmp_path, correction=False)
    reader = PointInTimeMarketReader(session)
    equivalent = datetime(2026, 1, 8, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    filtered = reader.benchmark_series_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=equivalent,
        start_date=date(2026, 1, 7),
        end_date=date(2026, 1, 7),
    )
    assert [bar.trading_date for bar in filtered] == [date(2026, 1, 7)]
    assert reader.benchmark_series_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="UNKNOWN",
        interval="1d",
        as_of=T3,
    ) == []
    assert (
        reader.benchmark_bar_as_of(
            provider_dataset_id=dataset_id,
            benchmark_code="UNKNOWN",
            trading_date=date(2026, 1, 5),
            interval="1d",
            as_of=T3,
        )
        is None
    )
    assert (
        reader.latest_benchmark_bar_as_of(
            provider_dataset_id=dataset_id,
            benchmark_code="UNKNOWN",
            interval="1d",
            as_of=T3,
        )
        is None
    )
    with pytest.raises(ValueError, match="start_date"):
        reader.benchmark_series_as_of(
            provider_dataset_id=dataset_id,
            benchmark_code="NIFTY50",
            interval="1d",
            as_of=T3,
            start_date=date(2026, 1, 8),
            end_date=date(2026, 1, 7),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        reader.latest_benchmark_bar_as_of(
            provider_dataset_id=dataset_id,
            benchmark_code="NIFTY50",
            interval="1d",
            as_of=datetime(2026, 1, 8, 18, 30),
        )
    with pytest.raises(ValueError, match="benchmark_code"):
        reader.benchmark_series_as_of(
            provider_dataset_id=dataset_id,
            benchmark_code="",
            interval="1d",
            as_of=T3,
        )


def test_benchmark_nonaccepted_source_is_excluded(session, tmp_path: Path) -> None:
    _, dataset_id = _benchmark_setup(session, tmp_path, correction=False)
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "SYN-BENCH-PIT-D1")
    )
    assert source is not None
    source.validation_status = "duplicate_economic"
    session.flush()
    selected = PointInTimeMarketReader(session).benchmark_bar_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="NIFTY50",
        trading_date=date(2026, 1, 5),
        interval="1d",
        as_of=T3,
    )
    assert selected is None


def test_benchmark_source_dataset_mismatch_fails_closed(session, tmp_path: Path) -> None:
    service, p1 = _benchmark_setup(session, tmp_path, correction=False)
    assert (
        service.ingest_benchmark_data(
            CSVBenchmarkDataProvider(
                FIXTURES / "benchmark_pit_provider2_synthetic.csv",
                BENCHMARK_P2,
                RETRIEVED_AT,
            )
        ).records_accepted
        == 1
    )
    p2 = _dataset_id(session, BENCHMARK_P2.provider_code, BENCHMARK_P2.dataset_code)
    series = session.scalar(
        select(BenchmarkSeries).where(BenchmarkSeries.provider_dataset_id == p1)
    )
    assert series is not None
    source = session.scalar(
        select(SourceRecord)
        .join(BenchmarkBar, BenchmarkBar.source_record_id == SourceRecord.id)
        .where(
            BenchmarkBar.benchmark_series_id == series.id,
            BenchmarkBar.trading_date == date(2026, 1, 5),
        )
    )
    assert source is not None
    source.provider_dataset_id = p2
    session.flush()

    with pytest.raises(MarketDataIntegrityError, match="source dataset"):
        PointInTimeMarketReader(session).benchmark_bar_as_of(
            provider_dataset_id=p1,
            benchmark_code="NIFTY50",
            trading_date=date(2026, 1, 5),
            interval="1d",
            as_of=T3,
        )
