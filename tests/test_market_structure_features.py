"""Phase 3G-C deterministic Market Structure feature tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.providers import (
    BenchmarkBarRecord,
    CorporateActionRecord,
    IngestionEnvelope,
    MarketBarRecord,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.corporate_action_pit import PointInTimeCorporateActionReader
from inflector_data.market_adjustments import (
    ADJUSTED_MARKET_PRICE_VERSION,
    AdjustedMarketBar,
    AdjustedMarketSeries,
    MarketAdjustmentPrimitives,
)
from inflector_data.market_pit import (
    BenchmarkSeriesView,
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)
from inflector_data.market_structure_features import (
    MarketStructureFeaturePrimitives,
    MovingAverageEvidence,
    RelativeStrengthEvidence,
)
from inflector_data.pit import SourceRecordView
from inflector_data.providers import (
    CSVUniverseProvider,
    MockBenchmarkDataProvider,
    MockCorporateActionProvider,
    MockMarketDataProvider,
)
from inflector_data.service import IngestionService
from inflector_database.models import DataProvider, ProviderDataset, Security

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = datetime(2026, 4, 1, 12, tzinfo=UTC)
START = date(2026, 1, 1)
MARKET_ID = UUID(int=101)
ACTION_ID = UUID(int=102)
BENCHMARK_ID = UUID(int=103)
SECURITY_ID = UUID(int=104)


def _source(identifier: int) -> SourceRecordView:
    return SourceRecordView(
        id=UUID(int=identifier),
        external_record_id=f"record-{identifier}",
        source_uri="fixture://market-structure",
        raw_object_key=f"raw/{identifier}",
        raw_payload_reference=f"row-{identifier}",
        content_sha256=f"{identifier:064x}",
        validation_status="accepted",
    )


def _raw_bar(
    index: int,
    close: Decimal,
    *,
    volume: int = 200,
    delivery: Decimal | None = Decimal("0.4"),
    available_at: datetime | None = None,
) -> PointInTimeMarketBar:
    timestamp = available_at or datetime(2026, 3, 1, tzinfo=UTC)
    return PointInTimeMarketBar(
        id=UUID(int=1000 + index),
        provider_dataset_id=MARKET_ID,
        security_id=SECURITY_ID,
        trading_date=START + timedelta(days=index * 2),
        interval="1d",
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=max(Decimal("0"), close - Decimal("1")),
        close_price=close,
        volume=volume,
        market_cap=Decimal("1000000"),
        delivery_quantity=80,
        delivery_percentage=delivery,
        available_at=timestamp,
        revision_at=None,
        ingested_at=timestamp + timedelta(days=10),
        source_record=_source(1000 + index),
    )


def _series(
    closes: Sequence[Decimal],
    *,
    raw_closes: Sequence[Decimal] | None = None,
    volumes: Sequence[int] | None = None,
    deliveries: Sequence[Decimal | None] | None = None,
    adjusted_available_at: datetime | None = None,
) -> AdjustedMarketSeries:
    raw_values = raw_closes or closes
    volume_values = volumes or [200] * len(closes)
    delivery_values = deliveries or [Decimal("0.4")] * len(closes)
    bars: list[AdjustedMarketBar] = []
    basis_date = START + timedelta(days=(len(closes) - 1) * 2) if closes else None
    for index, adjusted_close in enumerate(closes):
        raw = _raw_bar(
            index,
            raw_values[index],
            volume=volume_values[index],
            delivery=delivery_values[index],
        )
        bars.append(
            AdjustedMarketBar(
                raw_bar=raw,
                adjusted_open=adjusted_close,
                adjusted_high=adjusted_close + Decimal("1"),
                adjusted_low=max(Decimal("0"), adjusted_close - Decimal("1")),
                adjusted_close=adjusted_close,
                cumulative_price_factor=Decimal("1"),
                applied_adjustments=(),
                adjustment_basis_date=basis_date or raw.trading_date,
                as_of=AS_OF,
                available_at=adjusted_available_at or raw.available_at,
                algorithm_version=ADJUSTED_MARKET_PRICE_VERSION,
            )
        )
    return AdjustedMarketSeries(
        market_provider_dataset_id=MARKET_ID,
        corporate_action_provider_dataset_id=ACTION_ID,
        security_id=SECURITY_ID,
        interval="1d",
        adjustment_basis_date=basis_date,
        bars=tuple(bars),
        selected_actions=(),
        applicable_adjustments=(),
        as_of=AS_OF,
        algorithm_version=ADJUSTED_MARKET_PRICE_VERSION,
    )


def _benchmark_bar(
    trading_date: date,
    close: Decimal,
    *,
    provider_id: UUID = BENCHMARK_ID,
    available_at: datetime | None = None,
) -> PointInTimeBenchmarkBar:
    timestamp = available_at or datetime(2026, 3, 2, tzinfo=UTC)
    return PointInTimeBenchmarkBar(
        id=UUID(int=5000 + trading_date.toordinal()),
        benchmark_series=BenchmarkSeriesView(
            id=UUID(int=6000 + provider_id.int),
            provider_dataset_id=provider_id,
            code="NIFTY50",
            display_name="Nifty 50",
            currency="INR",
        ),
        trading_date=trading_date,
        interval="1d",
        open_value=close,
        high_value=close,
        low_value=close,
        close_value=close,
        available_at=timestamp,
        revision_at=None,
        ingested_at=timestamp,
        source_record=_source(5000 + trading_date.toordinal()),
    )


class _StaticMarketReader(PointInTimeMarketReader):
    def __init__(self, bars: dict[tuple[UUID, date], PointInTimeBenchmarkBar]) -> None:
        self._bars = bars

    def benchmark_bar_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        trading_date: date,
        interval: str,
        as_of: datetime,
    ) -> PointInTimeBenchmarkBar | None:
        assert benchmark_code == "NIFTY50"
        assert interval == "1d"
        assert as_of == AS_OF
        return self._bars.get((provider_dataset_id, trading_date))


class _StaticAdjustments(MarketAdjustmentPrimitives):
    def __init__(self, series: AdjustedMarketSeries) -> None:
        self._series = series

    def adjusted_market_series_as_of(
        self,
        *,
        market_provider_dataset_id: UUID,
        corporate_action_provider_dataset_id: UUID,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AdjustedMarketSeries:
        assert market_provider_dataset_id == MARKET_ID
        assert corporate_action_provider_dataset_id == ACTION_ID
        assert security_id == SECURITY_ID
        assert interval == "1d"
        assert as_of == AS_OF
        assert start_date is None
        bars = tuple(
            bar
            for bar in self._series.bars
            if end_date is None or bar.raw_bar.trading_date <= end_date
        )
        return AdjustedMarketSeries(
            market_provider_dataset_id=MARKET_ID,
            corporate_action_provider_dataset_id=ACTION_ID,
            security_id=SECURITY_ID,
            interval="1d",
            adjustment_basis_date=bars[-1].raw_bar.trading_date if bars else None,
            bars=bars,
            selected_actions=self._series.selected_actions,
            applicable_adjustments=self._series.applicable_adjustments,
            as_of=AS_OF,
            algorithm_version=ADJUSTED_MARKET_PRICE_VERSION,
        )


def _features(
    series: AdjustedMarketSeries,
    benchmark_bars: dict[tuple[UUID, date], PointInTimeBenchmarkBar] | None = None,
    *,
    benchmark_provider_id: UUID = BENCHMARK_ID,
    market_on_or_before: date | None = None,
):
    engine = MarketStructureFeaturePrimitives(
        _StaticMarketReader(benchmark_bars or {}), _StaticAdjustments(series)
    )
    return engine.features_as_of(
        market_provider_dataset_id=MARKET_ID,
        corporate_action_provider_dataset_id=ACTION_ID,
        benchmark_provider_dataset_id=benchmark_provider_id,
        benchmark_code="NIFTY50",
        security_id=SECURITY_ID,
        interval="1d",
        as_of=AS_OF,
        market_on_or_before=market_on_or_before,
    )


@pytest.mark.parametrize(
    ("count", "available_codes"),
    [
        (0, set()),
        (1, set()),
        (19, set()),
        (
            20,
            {
                "close_to_sma20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "average_delivery_percentage_20",
            },
        ),
        (
            21,
            {
                "close_to_sma20",
                "return_volatility_20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "average_delivery_percentage_20",
            },
        ),
        (
            59,
            {
                "close_to_sma20",
                "return_volatility_20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "average_delivery_percentage_20",
            },
        ),
        (
            60,
            {
                "close_to_sma20",
                "sma20_to_sma60",
                "return_volatility_20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "close_times_volume_ratio_20_to_60",
                "average_delivery_percentage_20",
            },
        ),
        (
            61,
            {
                "relative_strength_60_to_benchmark",
                "close_to_sma20",
                "sma20_to_sma60",
                "return_volatility_20",
                "return_volatility_60",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "close_times_volume_ratio_20_to_60",
                "average_delivery_percentage_20",
            },
        ),
    ],
)
def test_observation_window_thresholds(count: int, available_codes: set[str]) -> None:
    series = _series([Decimal("100")] * count)
    benchmarks: dict[tuple[UUID, date], PointInTimeBenchmarkBar] = {}
    if count >= 61:
        start, end = series.bars[-61], series.bars[-1]
        benchmarks = {
            (BENCHMARK_ID, start.raw_bar.trading_date): _benchmark_bar(
                start.raw_bar.trading_date, Decimal("100")
            ),
            (BENCHMARK_ID, end.raw_bar.trading_date): _benchmark_bar(
                end.raw_bar.trading_date, Decimal("100")
            ),
        }
    bundle = _features(series, benchmarks)
    values = (
        bundle.relative_strength_60_to_benchmark,
        bundle.close_to_sma20,
        bundle.sma20_to_sma60,
        bundle.return_volatility_20,
        bundle.return_volatility_60,
        bundle.volatility_ratio_20_to_60,
        bundle.consolidation_range_20,
        bundle.average_close_times_volume_20_inr,
        bundle.close_times_volume_ratio_20_to_60,
        bundle.average_delivery_percentage_20,
    )
    assert {item.code for item in values if item.value is not None} == available_codes
    assert bundle.basis_date == (series.bars[-1].raw_bar.trading_date if count else None)


def test_trend_consolidation_activity_delivery_and_missing_dates_are_exact() -> None:
    closes = [Decimal(index) for index in range(1, 62)]
    bundle = _features(_series(closes))

    assert bundle.close_to_sma20.value == Decimal("61") / Decimal("51.5") - Decimal("1")
    assert bundle.sma20_to_sma60.value == Decimal("51.5") / Decimal("31.5") - Decimal("1")
    expected_range = Decimal("21") / Decimal("51.5")
    assert bundle.consolidation_range_20.value == expected_range
    assert bundle.average_delivery_percentage_20.value == Decimal("0.4")
    moving_average_evidence = bundle.close_to_sma20.evidence
    assert isinstance(moving_average_evidence, MovingAverageEvidence)
    assert all(
        later.raw_bar.trading_date - earlier.raw_bar.trading_date == timedelta(days=2)
        for earlier, later in zip(
            moving_average_evidence.bars,
            moving_average_evidence.bars[1:],
            strict=False,
        )
    )


def test_volatility_uses_population_decimal_stddev_and_invalid_returns_fail_closed() -> None:
    closes = [Decimal("1") if index % 2 == 0 else Decimal("2") for index in range(61)]
    bundle = _features(_series(closes))
    assert bundle.return_volatility_20.value == Decimal("0.75")
    assert bundle.return_volatility_60.value == Decimal("0.75")
    assert bundle.volatility_ratio_20_to_60.value == Decimal("1")
    assert _features(_series(closes)).return_volatility_60.value == Decimal("0.75")

    invalid = closes.copy()
    invalid[-10] = Decimal("0")
    invalid_bundle = _features(_series(invalid))
    assert invalid_bundle.return_volatility_20.value is None
    assert invalid_bundle.return_volatility_20.warnings == ("invalid_return_in_window",)


def test_flat_series_relative_strength_and_zero_volatility_ratio() -> None:
    series = _series([Decimal("100")] * 61)
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert bundle.relative_strength_60_to_benchmark.value == Decimal("0")
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value == Decimal("0")
    assert bundle.return_volatility_20.value == Decimal("0")
    assert bundle.return_volatility_60.value == Decimal("0")
    assert bundle.volatility_ratio_20_to_60.value is None
    assert bundle.volatility_ratio_20_to_60.warnings == ("zero_medium_volatility",)
    assert bundle.consolidation_range_20.value == Decimal("0.02")


def test_relative_strength_uses_exact_endpoints_and_explicit_provider() -> None:
    series = _series([Decimal("100")] * 60 + [Decimal("110")])
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    other_provider = UUID(int=999)
    bars = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("105")),
        (other_provider, start): _benchmark_bar(
            start, Decimal("100"), provider_id=other_provider
        ),
        (other_provider, end): _benchmark_bar(end, Decimal("110"), provider_id=other_provider),
    }
    expected = Decimal("1.1") / Decimal("1.05") - Decimal("1")
    assert _features(series, bars).relative_strength_60_to_benchmark.value == expected
    assert (
        _features(series, bars, benchmark_provider_id=other_provider)
        .relative_strength_60_to_benchmark.value
        == Decimal("0")
    )
    missing_start = {(BENCHMARK_ID, end): bars[(BENCHMARK_ID, end)]}
    missing_end = {(BENCHMARK_ID, start): bars[(BENCHMARK_ID, start)]}
    assert _features(series, missing_start).relative_strength_60_to_benchmark.warnings == (
        "missing_benchmark_start_bar",
    )
    assert _features(series, missing_end).relative_strength_60_to_benchmark.warnings == (
        "missing_benchmark_end_bar",
    )
    underperforming = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("120")),
    }
    underperformance = _features(
        series, underperforming
    ).relative_strength_60_to_benchmark.value
    assert underperformance is not None and underperformance < 0


def test_zero_price_boundaries_fail_closed_only_when_used_as_denominators() -> None:
    closes = [Decimal("1")] * 60 + [Decimal("0")]
    series = _series(closes)
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert bundle.close_to_sma20.value == Decimal("-1")
    assert bundle.relative_strength_60_to_benchmark.value == Decimal("-1")

    all_zero = _features(_series([Decimal("0")] * 61), benchmarks)
    assert all_zero.close_to_sma20.warnings == ("non_positive_sma20",)
    assert all_zero.sma20_to_sma60.warnings == ("non_positive_sma60",)
    assert all_zero.consolidation_range_20.warnings == (
        "non_positive_consolidation_mean_close",
    )
    assert all_zero.close_times_volume_ratio_20_to_60.warnings == (
        "zero_medium_activity_proxy",
    )


def test_bonus_neutralized_series_does_not_create_false_price_structure() -> None:
    adjusted = [Decimal("100")] * 61
    raw = [Decimal("150")] * 30 + [Decimal("100")] * 31
    series = _series(adjusted, raw_closes=raw)
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert bundle.relative_strength_60_to_benchmark.value == Decimal("0")
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value == Decimal("0")
    assert bundle.return_volatility_60.value == Decimal("0")


def test_activity_uses_raw_close_times_raw_volume_and_delivery_requires_complete_window() -> None:
    adjusted = [Decimal("100")] * 60
    raw = [Decimal("200")] * 30 + [Decimal("100")] * 30
    volumes = [100] * 30 + [200] * 30
    bundle = _features(_series(adjusted, raw_closes=raw, volumes=volumes))
    assert bundle.average_close_times_volume_20_inr.value == Decimal("20000")
    assert bundle.close_times_volume_ratio_20_to_60.value == Decimal("1")

    deliveries: list[Decimal | None] = [Decimal("0.4")] * 60
    deliveries[-1] = None
    missing = _features(_series(adjusted, deliveries=deliveries))
    assert missing.average_delivery_percentage_20.value is None
    assert missing.average_delivery_percentage_20.warnings == (
        "incomplete_delivery_window",
    )


def test_action_and_benchmark_corrections_only_move_dependent_features() -> None:
    raw = [Decimal("100")] * 61
    base = _series(raw)
    start, end = base.bars[0].raw_bar.trading_date, base.bars[-1].raw_bar.trading_date
    benchmark_a = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    benchmark_b = {
        **benchmark_a,
        (BENCHMARK_ID, end): _benchmark_bar(
            end, Decimal("110"), available_at=datetime(2026, 3, 15, tzinfo=UTC)
        ),
    }
    first = _features(base, benchmark_a)
    benchmark_corrected = _features(base, benchmark_b)
    assert first.relative_strength_60_to_benchmark.value != (
        benchmark_corrected.relative_strength_60_to_benchmark.value
    )
    assert first.close_to_sma20 == benchmark_corrected.close_to_sma20
    assert first.average_close_times_volume_20_inr == (
        benchmark_corrected.average_close_times_volume_20_inr
    )

    adjusted_change = [Decimal("50")] * 30 + [Decimal("100")] * 31
    action_corrected = _features(
        _series(
            adjusted_change,
            raw_closes=raw,
            adjusted_available_at=datetime(2026, 3, 20, tzinfo=UTC),
        ),
        benchmark_a,
    )
    assert action_corrected.sma20_to_sma60.value != first.sma20_to_sma60.value
    assert action_corrected.return_volatility_60.value != first.return_volatility_60.value
    assert action_corrected.average_close_times_volume_20_inr.value == (
        first.average_close_times_volume_20_inr.value
    )
    assert action_corrected.average_delivery_percentage_20.value == (
        first.average_delivery_percentage_20.value
    )
    assert action_corrected.sma20_to_sma60.available_at == datetime(
        2026, 3, 20, tzinfo=UTC
    )
    assert action_corrected.average_close_times_volume_20_inr.available_at == datetime(
        2026, 3, 1, tzinfo=UTC
    )


def test_market_on_or_before_uses_historical_economic_basis_and_utc_cutoff() -> None:
    series = _series([Decimal("100")] * 61)
    bound = series.bars[19].raw_bar.trading_date
    bundle = _features(series, market_on_or_before=bound)
    assert bundle.basis_date == bound
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value is None

    engine = MarketStructureFeaturePrimitives(_StaticMarketReader({}), _StaticAdjustments(series))
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.features_as_of(
            market_provider_dataset_id=MARKET_ID,
            corporate_action_provider_dataset_id=ACTION_ID,
            benchmark_provider_dataset_id=BENCHMARK_ID,
            benchmark_code="NIFTY50",
            security_id=SECURITY_ID,
            interval="1d",
            as_of=datetime(2026, 4, 1, 12),
        )


UNIVERSE = ProviderMetadata("structure_universe", "csv", "universe", "synthetic")
MARKET = ProviderMetadata("structure_market", "mock", "market_daily", "synthetic")
ACTIONS = ProviderMetadata("structure_actions", "mock", "corporate_actions", "synthetic")
BENCHMARK = ProviderMetadata("structure_benchmark", "mock", "benchmark_daily", "synthetic")
ISIN = "INF0AUR01018"


def _batch[TRecord: (MarketBarRecord, BenchmarkBarRecord, CorporateActionRecord)](
    records: Sequence[TRecord],
    metadata: ProviderMetadata,
    prefix: str,
    *,
    available_at: datetime = datetime(2026, 3, 1, tzinfo=UTC),
    external_ids: Sequence[str] | None = None,
) -> ProviderBatch[TRecord]:
    rows = [f"{prefix}-{index}:{record!r}" for index, record in enumerate(records)]
    envelopes = tuple(
        IngestionEnvelope(
            provider=metadata,
            external_record_id=(
                external_ids[index] if external_ids is not None else f"{prefix}-{index}"
            ),
            source_uri=f"fixture://{prefix}",
            raw_payload_reference=f"row-{index}",
            content_sha256=sha256(rows[index].encode()).hexdigest(),
            retrieved_at=datetime(2026, 3, 1, tzinfo=UTC),
            record=record,
            available_at=available_at,
        )
        for index, record in enumerate(records)
    )
    return ProviderBatch(
        provider=metadata,
        source_uri=f"fixture://{prefix}",
        raw_payload="\n".join(rows).encode(),
        retrieved_at=datetime(2026, 3, 1, tzinfo=UTC),
        records=envelopes,
    )


def _dataset_id(session: Session, metadata: ProviderMetadata) -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(
            DataProvider.code == metadata.provider_code,
            ProviderDataset.code == metadata.dataset_code,
        )
    )
    assert value is not None
    return value


def test_full_real_ingestion_split_neutralized_feature_lineage(
    session: Session, tmp_path: Path
) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_synthetic.csv",
            UNIVERSE,
            datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    security_id = session.scalar(select(Security.id).where(Security.isin == ISIN))
    assert security_id is not None
    market_records: list[MarketBarRecord] = []
    benchmark_records: list[BenchmarkBarRecord] = []
    for index in range(61):
        trading_date = START + timedelta(days=index)
        pre_split = index < 30
        price = Decimal("200") if pre_split else Decimal("100")
        volume = 100 if pre_split else 200
        market_records.append(
            MarketBarRecord(
                security_isin=ISIN,
                trading_date=trading_date,
                interval="1d",
                open_price=price,
                high_price=price,
                low_price=price,
                close_price=price,
                volume=volume,
                market_cap=None,
                delivery_quantity=None,
                delivery_percentage=Decimal("0.4"),
            )
        )
        benchmark_records.append(
            BenchmarkBarRecord(
                benchmark_code="NIFTY50",
                benchmark_display_name="Nifty 50",
                currency="INR",
                trading_date=trading_date,
                interval="1d",
                open_value=Decimal("100"),
                high_value=Decimal("100"),
                low_value=Decimal("100"),
                close_value=Decimal("100"),
            )
        )
    action = CorporateActionRecord(
        security_isin=ISIN,
        action_type="split",
        announcement_date=date(2026, 1, 20),
        ex_date=None,
        record_date=None,
        effective_date=START + timedelta(days=30),
        ratio_numerator=2,
        ratio_denominator=1,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
        subscription_price=None,
        subscription_currency=None,
        exchange=None,
        old_symbol=None,
        new_symbol=None,
        successor_isin=None,
    )
    service.ingest_market_data(MockMarketDataProvider(_batch(market_records, MARKET, "market")))
    service.ingest_corporate_actions(
        MockCorporateActionProvider(_batch([action], ACTIONS, "actions"))
    )
    service.ingest_benchmark_data(
        MockBenchmarkDataProvider(_batch(benchmark_records, BENCHMARK, "benchmark"))
    )
    market_reader = PointInTimeMarketReader(session)
    adjustments = MarketAdjustmentPrimitives(
        market_reader, PointInTimeCorporateActionReader(session)
    )
    engine = MarketStructureFeaturePrimitives(market_reader, adjustments)

    def read(cutoff: datetime):
        return engine.features_as_of(
            market_provider_dataset_id=_dataset_id(session, MARKET),
            corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS),
            benchmark_provider_dataset_id=_dataset_id(session, BENCHMARK),
            benchmark_code="NIFTY50",
            security_id=security_id,
            interval="1d",
            as_of=cutoff,
        )

    baseline_cutoff = datetime(2026, 3, 5, tzinfo=UTC)
    bundle = read(baseline_cutoff)

    assert bundle.relative_strength_60_to_benchmark.value == Decimal("0")
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value == Decimal("0")
    assert bundle.return_volatility_20.value == Decimal("0")
    assert bundle.return_volatility_60.value == Decimal("0")
    assert bundle.average_close_times_volume_20_inr.value == Decimal("20000")
    assert bundle.close_times_volume_ratio_20_to_60.value == Decimal("1")
    assert bundle.average_delivery_percentage_20.value == Decimal("0.4")
    relative_evidence = bundle.relative_strength_60_to_benchmark.evidence
    assert isinstance(relative_evidence, RelativeStrengthEvidence)
    assert relative_evidence.security_start.raw_bar.source_record.raw_object_key
    action_source = relative_evidence.security_start.applied_adjustments[0].action.source_record
    assert action_source.raw_object_key
    assert relative_evidence.benchmark_start is not None
    assert relative_evidence.benchmark_start.source_record.raw_object_key

    corrected_action = CorporateActionRecord(
        security_isin=ISIN,
        action_type="split",
        announcement_date=date(2026, 1, 20),
        ex_date=None,
        record_date=None,
        effective_date=START + timedelta(days=30),
        ratio_numerator=4,
        ratio_denominator=1,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
        subscription_price=None,
        subscription_currency=None,
        exchange=None,
        old_symbol=None,
        new_symbol=None,
        successor_isin=None,
    )
    action_cutoff = datetime(2026, 3, 10, tzinfo=UTC)
    service.ingest_corporate_actions(
        MockCorporateActionProvider(
            _batch(
                [corrected_action],
                ACTIONS,
                "actions-corrected",
                available_at=action_cutoff,
                external_ids=["actions-0"],
            )
        )
    )
    action_corrected = read(action_cutoff)
    assert action_corrected.sma20_to_sma60.value != bundle.sma20_to_sma60.value
    assert action_corrected.return_volatility_60.value != bundle.return_volatility_60.value
    assert action_corrected.relative_strength_60_to_benchmark.value != (
        bundle.relative_strength_60_to_benchmark.value
    )
    assert action_corrected.average_close_times_volume_20_inr.value == (
        bundle.average_close_times_volume_20_inr.value
    )
    assert action_corrected.average_delivery_percentage_20.value == (
        bundle.average_delivery_percentage_20.value
    )
    assert action_corrected.sma20_to_sma60.available_at == action_cutoff
    assert action_corrected.average_close_times_volume_20_inr.available_at == datetime(
        2026, 3, 1, tzinfo=UTC
    )

    benchmark_cutoff = datetime(2026, 3, 20, tzinfo=UTC)
    corrected_benchmark = BenchmarkBarRecord(
        benchmark_code="NIFTY50",
        benchmark_display_name="Nifty 50",
        currency="INR",
        trading_date=START + timedelta(days=60),
        interval="1d",
        open_value=Decimal("110"),
        high_value=Decimal("110"),
        low_value=Decimal("110"),
        close_value=Decimal("110"),
    )
    service.ingest_benchmark_data(
        MockBenchmarkDataProvider(
            _batch(
                [corrected_benchmark],
                BENCHMARK,
                "benchmark-corrected",
                available_at=benchmark_cutoff,
                external_ids=["benchmark-60"],
            )
        )
    )
    benchmark_corrected = read(benchmark_cutoff)
    assert benchmark_corrected.relative_strength_60_to_benchmark.value != (
        action_corrected.relative_strength_60_to_benchmark.value
    )
    for code in (
        "close_to_sma20",
        "sma20_to_sma60",
        "return_volatility_20",
        "return_volatility_60",
        "consolidation_range_20",
        "average_close_times_volume_20_inr",
        "close_times_volume_ratio_20_to_60",
        "average_delivery_percentage_20",
    ):
        assert getattr(benchmark_corrected, code).value == getattr(
            action_corrected, code
        ).value

    market_cutoff = datetime(2026, 3, 30, tzinfo=UTC)
    corrected_market = MarketBarRecord(
        security_isin=ISIN,
        trading_date=START + timedelta(days=60),
        interval="1d",
        open_price=Decimal("110"),
        high_price=Decimal("110"),
        low_price=Decimal("110"),
        close_price=Decimal("110"),
        volume=200,
        market_cap=None,
        delivery_quantity=None,
        delivery_percentage=Decimal("0.6"),
    )
    service.ingest_market_data(
        MockMarketDataProvider(
            _batch(
                [corrected_market],
                MARKET,
                "market-corrected",
                available_at=market_cutoff,
                external_ids=["market-60"],
            )
        )
    )
    market_corrected = read(market_cutoff)
    assert market_corrected.close_to_sma20.value != benchmark_corrected.close_to_sma20.value
    assert market_corrected.average_close_times_volume_20_inr.value != (
        benchmark_corrected.average_close_times_volume_20_inr.value
    )
    assert market_corrected.average_delivery_percentage_20.value != (
        benchmark_corrected.average_delivery_percentage_20.value
    )

    historical_rerun = read(baseline_cutoff)
    assert historical_rerun.relative_strength_60_to_benchmark.value == (
        bundle.relative_strength_60_to_benchmark.value
    )
    assert historical_rerun.sma20_to_sma60.value == bundle.sma20_to_sma60.value
    assert historical_rerun.average_close_times_volume_20_inr.value == (
        bundle.average_close_times_volume_20_inr.value
    )
    assert historical_rerun.average_delivery_percentage_20.value == (
        bundle.average_delivery_percentage_20.value
    )
