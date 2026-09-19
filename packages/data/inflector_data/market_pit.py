"""Deterministic point-in-time reads over accepted raw market observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_data.pit import SourceRecordView
from inflector_database.models import BenchmarkBar, BenchmarkSeries, PriceBar, SourceRecord


class MarketDataIntegrityError(ValueError):
    """Raised when persisted market provenance violates its provider identity."""


@dataclass(frozen=True, slots=True)
class PointInTimeMarketBar:
    """One atomic provider market observation selected at a knowledge cutoff."""

    id: UUID
    provider_dataset_id: UUID
    security_id: UUID
    trading_date: date
    interval: str
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: int
    market_cap: Decimal | None
    delivery_quantity: int | None
    delivery_percentage: Decimal | None
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    source_record: SourceRecordView


@dataclass(frozen=True, slots=True)
class BenchmarkSeriesView:
    """Provider-dataset-local benchmark identity without mutable status policy."""

    id: UUID
    provider_dataset_id: UUID
    code: str
    display_name: str
    currency: str


@dataclass(frozen=True, slots=True)
class PointInTimeBenchmarkBar:
    """One atomic provider benchmark observation selected at a knowledge cutoff."""

    id: UUID
    benchmark_series: BenchmarkSeriesView
    trading_date: date
    interval: str
    open_value: Decimal
    high_value: Decimal
    low_value: Decimal
    close_value: Decimal
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    source_record: SourceRecordView


_MarketRow = tuple[PriceBar, SourceRecord]
_BenchmarkRow = tuple[BenchmarkBar, BenchmarkSeries, SourceRecord]


class PointInTimeMarketReader:
    """Read accepted market evidence without provider fallback or derivation."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def market_bar_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        trading_date: date,
        interval: str,
        as_of: datetime,
    ) -> PointInTimeMarketBar | None:
        """Return the newest eligible revision for one exact market-bar identity."""

        cutoff = self._knowledge_cutoff(as_of)
        self._validate_interval(interval)
        rows = self._market_rows_as_of(
            provider_dataset_id=provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=cutoff,
            start_date=trading_date,
            end_date=trading_date,
        )
        return self._market_view(rows[0]) if rows else None

    def market_series_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[PointInTimeMarketBar]:
        """Return one selected atomic revision per economic trading date."""

        cutoff = self._knowledge_cutoff(as_of)
        self._validate_interval(interval)
        self._validate_range(start_date, end_date)
        rows = self._market_rows_as_of(
            provider_dataset_id=provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=cutoff,
            start_date=start_date,
            end_date=end_date,
        )
        selected: dict[date, _MarketRow] = {}
        for row in rows:
            selected.setdefault(row[0].trading_date, row)
        return [self._market_view(row) for row in selected.values()]

    def latest_market_bar_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        on_or_before: date | None = None,
    ) -> PointInTimeMarketBar | None:
        """Return the greatest eligible economic trading date, without a freshness rule."""

        series = self.market_series_as_of(
            provider_dataset_id=provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=as_of,
            end_date=on_or_before,
        )
        return series[-1] if series else None

    def benchmark_bar_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        trading_date: date,
        interval: str,
        as_of: datetime,
    ) -> PointInTimeBenchmarkBar | None:
        """Return one exact provider-local benchmark bar as known at the cutoff."""

        cutoff = self._knowledge_cutoff(as_of)
        self._validate_benchmark_code(benchmark_code)
        self._validate_interval(interval)
        rows = self._benchmark_rows_as_of(
            provider_dataset_id=provider_dataset_id,
            benchmark_code=benchmark_code,
            interval=interval,
            as_of=cutoff,
            start_date=trading_date,
            end_date=trading_date,
        )
        return self._benchmark_view(rows[0]) if rows else None

    def benchmark_series_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        interval: str,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[PointInTimeBenchmarkBar]:
        """Return one benchmark revision per economic trading date, oldest first."""

        cutoff = self._knowledge_cutoff(as_of)
        self._validate_benchmark_code(benchmark_code)
        self._validate_interval(interval)
        self._validate_range(start_date, end_date)
        rows = self._benchmark_rows_as_of(
            provider_dataset_id=provider_dataset_id,
            benchmark_code=benchmark_code,
            interval=interval,
            as_of=cutoff,
            start_date=start_date,
            end_date=end_date,
        )
        selected: dict[date, _BenchmarkRow] = {}
        for row in rows:
            selected.setdefault(row[0].trading_date, row)
        return [self._benchmark_view(row) for row in selected.values()]

    def latest_benchmark_bar_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        interval: str,
        as_of: datetime,
        on_or_before: date | None = None,
    ) -> PointInTimeBenchmarkBar | None:
        """Return the greatest eligible benchmark trading date, without staleness policy."""

        series = self.benchmark_series_as_of(
            provider_dataset_id=provider_dataset_id,
            benchmark_code=benchmark_code,
            interval=interval,
            as_of=as_of,
            end_date=on_or_before,
        )
        return series[-1] if series else None

    def _market_rows_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        start_date: date | None,
        end_date: date | None,
    ) -> list[_MarketRow]:
        statement = (
            select(PriceBar, SourceRecord)
            .join(SourceRecord, PriceBar.source_record_id == SourceRecord.id)
            .where(
                SourceRecord.provider_dataset_id == provider_dataset_id,
                SourceRecord.validation_status == "accepted",
                PriceBar.security_id == security_id,
                PriceBar.interval == interval,
                PriceBar.available_at <= as_of,
            )
            .order_by(
                PriceBar.trading_date.asc(),
                PriceBar.available_at.desc(),
                func.coalesce(PriceBar.revision_at, PriceBar.available_at).desc(),
                PriceBar.ingested_at.desc(),
                PriceBar.id.desc(),
            )
        )
        if start_date is not None:
            statement = statement.where(PriceBar.trading_date >= start_date)
        if end_date is not None:
            statement = statement.where(PriceBar.trading_date <= end_date)
        return list(self._session.execute(statement).tuples())

    def _benchmark_rows_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        interval: str,
        as_of: datetime,
        start_date: date | None,
        end_date: date | None,
    ) -> list[_BenchmarkRow]:
        statement = (
            select(BenchmarkBar, BenchmarkSeries, SourceRecord)
            .join(
                BenchmarkSeries,
                BenchmarkBar.benchmark_series_id == BenchmarkSeries.id,
            )
            .join(SourceRecord, BenchmarkBar.source_record_id == SourceRecord.id)
            .where(
                BenchmarkSeries.provider_dataset_id == provider_dataset_id,
                BenchmarkSeries.code == benchmark_code,
                SourceRecord.validation_status == "accepted",
                BenchmarkBar.interval == interval,
                BenchmarkBar.available_at <= as_of,
            )
            .order_by(
                BenchmarkBar.trading_date.asc(),
                BenchmarkBar.available_at.desc(),
                func.coalesce(BenchmarkBar.revision_at, BenchmarkBar.available_at).desc(),
                BenchmarkBar.ingested_at.desc(),
                BenchmarkBar.id.desc(),
            )
        )
        if start_date is not None:
            statement = statement.where(BenchmarkBar.trading_date >= start_date)
        if end_date is not None:
            statement = statement.where(BenchmarkBar.trading_date <= end_date)
        rows = list(self._session.execute(statement).tuples())
        for _, series, source in rows:
            if source.provider_dataset_id != series.provider_dataset_id:
                raise MarketDataIntegrityError(
                    "benchmark source dataset does not match benchmark series dataset"
                )
        return rows

    @staticmethod
    def _market_view(row: _MarketRow) -> PointInTimeMarketBar:
        bar, source = row
        return PointInTimeMarketBar(
            id=bar.id,
            provider_dataset_id=source.provider_dataset_id,
            security_id=bar.security_id,
            trading_date=bar.trading_date,
            interval=bar.interval,
            open_price=bar.open_price,
            high_price=bar.high_price,
            low_price=bar.low_price,
            close_price=bar.close_price,
            volume=bar.volume,
            market_cap=bar.market_cap,
            delivery_quantity=bar.delivery_quantity,
            delivery_percentage=bar.delivery_percentage,
            available_at=PointInTimeMarketReader._as_utc(bar.available_at),
            revision_at=PointInTimeMarketReader._as_utc_or_none(bar.revision_at),
            ingested_at=PointInTimeMarketReader._as_utc(bar.ingested_at),
            source_record=PointInTimeMarketReader._source_view(source),
        )

    @staticmethod
    def _benchmark_view(row: _BenchmarkRow) -> PointInTimeBenchmarkBar:
        bar, series, source = row
        return PointInTimeBenchmarkBar(
            id=bar.id,
            benchmark_series=BenchmarkSeriesView(
                id=series.id,
                provider_dataset_id=series.provider_dataset_id,
                code=series.code,
                display_name=series.display_name,
                currency=series.currency,
            ),
            trading_date=bar.trading_date,
            interval=bar.interval,
            open_value=bar.open_value,
            high_value=bar.high_value,
            low_value=bar.low_value,
            close_value=bar.close_value,
            available_at=PointInTimeMarketReader._as_utc(bar.available_at),
            revision_at=PointInTimeMarketReader._as_utc_or_none(bar.revision_at),
            ingested_at=PointInTimeMarketReader._as_utc(bar.ingested_at),
            source_record=PointInTimeMarketReader._source_view(source),
        )

    @staticmethod
    def _source_view(source: SourceRecord) -> SourceRecordView:
        return SourceRecordView(
            id=source.id,
            external_record_id=source.external_record_id,
            source_uri=source.source_uri,
            raw_object_key=source.raw_object_key,
            raw_payload_reference=source.raw_payload_reference,
            content_sha256=source.content_sha256,
            validation_status=source.validation_status,
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)

    @staticmethod
    def _validate_interval(interval: str) -> None:
        if not interval.strip():
            raise ValueError("interval must be non-empty")

    @staticmethod
    def _validate_benchmark_code(benchmark_code: str) -> None:
        if not benchmark_code.strip():
            raise ValueError("benchmark_code must be non-empty")

    @staticmethod
    def _validate_range(start_date: date | None, end_date: date | None) -> None:
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date must be on or before end_date")

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _as_utc_or_none(value: datetime | None) -> datetime | None:
        return None if value is None else PointInTimeMarketReader._as_utc(value)
