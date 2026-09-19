"""PIT-safe split/bonus price adjustments and adjacent simple returns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from inflector_data.corporate_action_pit import (
    PointInTimeCorporateAction,
    PointInTimeCorporateActionReader,
)
from inflector_data.market_pit import (
    BenchmarkSeriesView,
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)

CORPORATE_ACTION_PRICE_FACTOR_VERSION = "corporate_action_price_factor_v1"
ADJUSTED_MARKET_PRICE_VERSION = "adjusted_market_price_v1"
SIMPLE_PRICE_RETURN_VERSION = "simple_price_return_v1"
SIMPLE_BENCHMARK_RETURN_VERSION = "simple_benchmark_return_v1"

_SUPPORTED_PRICE_ACTIONS = frozenset({"split", "bonus"})


@dataclass(frozen=True, slots=True)
class CorporateActionPriceAdjustment:
    """Auditable per-action share and historical-price factors."""

    action: PointInTimeCorporateAction
    event_date: date
    action_type: str
    ratio_numerator: int
    ratio_denominator: int
    share_factor: Decimal
    price_factor: Decimal
    available_at: datetime
    algorithm_version: str = CORPORATE_ACTION_PRICE_FACTOR_VERSION


@dataclass(frozen=True, slots=True)
class AdjustedMarketBar:
    """One raw market bar rebased to an explicit latest economic price basis."""

    raw_bar: PointInTimeMarketBar
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    cumulative_price_factor: Decimal
    applied_adjustments: tuple[CorporateActionPriceAdjustment, ...]
    adjustment_basis_date: date
    as_of: datetime
    available_at: datetime
    algorithm_version: str = ADJUSTED_MARKET_PRICE_VERSION


@dataclass(frozen=True, slots=True)
class AdjustedMarketSeries:
    """A raw PIT series and action evidence on one explicit price basis."""

    market_provider_dataset_id: UUID
    corporate_action_provider_dataset_id: UUID
    security_id: UUID
    interval: str
    adjustment_basis_date: date | None
    bars: tuple[AdjustedMarketBar, ...]
    selected_actions: tuple[PointInTimeCorporateAction, ...]
    applicable_adjustments: tuple[CorporateActionPriceAdjustment, ...]
    as_of: datetime
    algorithm_version: str = ADJUSTED_MARKET_PRICE_VERSION


@dataclass(frozen=True, slots=True)
class SimplePriceReturn:
    """Adjacent-observation simple return over adjusted security closes."""

    security_id: UUID
    previous_trading_date: date
    trading_date: date
    previous_adjusted_close: Decimal
    current_adjusted_close: Decimal
    value: Decimal | None
    unit: str
    previous_bar: AdjustedMarketBar
    current_bar: AdjustedMarketBar
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimpleBenchmarkReturn:
    """Adjacent-observation simple return over raw PIT benchmark closes."""

    benchmark_series: BenchmarkSeriesView
    previous_trading_date: date
    trading_date: date
    previous_close: Decimal
    current_close: Decimal
    value: Decimal | None
    unit: str
    previous_bar: PointInTimeBenchmarkBar
    current_bar: PointInTimeBenchmarkBar
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    warnings: tuple[str, ...]


class MarketAdjustmentPrimitives:
    """Compose approved PIT readers into non-persisted adjustment/return evidence."""

    def __init__(
        self,
        market_reader: PointInTimeMarketReader,
        corporate_action_reader: PointInTimeCorporateActionReader,
    ) -> None:
        self._market_reader = market_reader
        self._corporate_action_reader = corporate_action_reader

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
        """Rebase selected raw OHLC using only PIT-visible split/bonus actions."""

        cutoff = self._knowledge_cutoff(as_of)
        raw_bars = self._market_reader.market_series_as_of(
            provider_dataset_id=market_provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=cutoff,
            start_date=start_date,
            end_date=end_date,
        )
        selected_actions = tuple(
            self._corporate_action_reader.corporate_actions_as_of(
                provider_dataset_id=corporate_action_provider_dataset_id,
                security_id=security_id,
                as_of=cutoff,
            )
        )
        if not raw_bars:
            return AdjustedMarketSeries(
                market_provider_dataset_id=market_provider_dataset_id,
                corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
                security_id=security_id,
                interval=interval,
                adjustment_basis_date=None,
                bars=(),
                selected_actions=selected_actions,
                applicable_adjustments=(),
                as_of=cutoff,
            )

        basis_date = raw_bars[-1].trading_date
        first_date = raw_bars[0].trading_date
        factor_candidates: list[CorporateActionPriceAdjustment] = []
        for action in selected_actions:
            if action.action_type not in _SUPPORTED_PRICE_ACTIONS:
                continue
            if action.effective_date is None:
                raise ValueError("split/bonus effective_date is required")
            if first_date < action.effective_date <= basis_date:
                factor_candidates.append(self._price_adjustment(action))
        applicable = tuple(
            sorted(
                factor_candidates,
                key=lambda factor: (
                    factor.event_date,
                    factor.action_type,
                    str(factor.action.id),
                ),
            )
        )
        bars = tuple(
            self._adjusted_bar(
                raw_bar=raw_bar,
                applicable_adjustments=applicable,
                basis_date=basis_date,
                as_of=cutoff,
            )
            for raw_bar in raw_bars
        )
        return AdjustedMarketSeries(
            market_provider_dataset_id=market_provider_dataset_id,
            corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
            security_id=security_id,
            interval=interval,
            adjustment_basis_date=basis_date,
            bars=bars,
            selected_actions=selected_actions,
            applicable_adjustments=applicable,
            as_of=cutoff,
        )

    def simple_price_returns_as_of(
        self,
        *,
        market_provider_dataset_id: UUID,
        corporate_action_provider_dataset_id: UUID,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[SimplePriceReturn, ...]:
        """Return exact simple returns between adjacent selected adjusted bars."""

        series = self.adjusted_market_series_as_of(
            market_provider_dataset_id=market_provider_dataset_id,
            corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
        )
        returns: list[SimplePriceReturn] = []
        for previous, current in zip(series.bars, series.bars[1:], strict=False):
            value, warnings = self._simple_return(previous.adjusted_close, current.adjusted_close)
            returns.append(
                SimplePriceReturn(
                    security_id=security_id,
                    previous_trading_date=previous.raw_bar.trading_date,
                    trading_date=current.raw_bar.trading_date,
                    previous_adjusted_close=previous.adjusted_close,
                    current_adjusted_close=current.adjusted_close,
                    value=value,
                    unit="ratio",
                    previous_bar=previous,
                    current_bar=current,
                    as_of=series.as_of,
                    available_at=max(previous.available_at, current.available_at),
                    algorithm_version=SIMPLE_PRICE_RETURN_VERSION,
                    warnings=warnings,
                )
            )
        return tuple(returns)

    def simple_benchmark_returns_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        interval: str,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[SimpleBenchmarkReturn, ...]:
        """Return exact simple returns between adjacent raw PIT benchmark bars."""

        cutoff = self._knowledge_cutoff(as_of)
        bars = self._market_reader.benchmark_series_as_of(
            provider_dataset_id=provider_dataset_id,
            benchmark_code=benchmark_code,
            interval=interval,
            as_of=cutoff,
            start_date=start_date,
            end_date=end_date,
        )
        returns: list[SimpleBenchmarkReturn] = []
        for previous, current in zip(bars, bars[1:], strict=False):
            value, warnings = self._simple_return(previous.close_value, current.close_value)
            returns.append(
                SimpleBenchmarkReturn(
                    benchmark_series=current.benchmark_series,
                    previous_trading_date=previous.trading_date,
                    trading_date=current.trading_date,
                    previous_close=previous.close_value,
                    current_close=current.close_value,
                    value=value,
                    unit="ratio",
                    previous_bar=previous,
                    current_bar=current,
                    as_of=cutoff,
                    available_at=max(previous.available_at, current.available_at),
                    algorithm_version=SIMPLE_BENCHMARK_RETURN_VERSION,
                    warnings=warnings,
                )
            )
        return tuple(returns)

    @staticmethod
    def _price_adjustment(
        action: PointInTimeCorporateAction,
    ) -> CorporateActionPriceAdjustment:
        if action.action_type not in _SUPPORTED_PRICE_ACTIONS:
            raise ValueError("only split and bonus actions define price factors")
        if action.effective_date is None:
            raise ValueError("split/bonus effective_date is required")
        numerator = action.ratio_numerator
        denominator = action.ratio_denominator
        if numerator is None or denominator is None or numerator <= 0 or denominator <= 0:
            raise ValueError("split/bonus ratio components must be positive")
        numerator_decimal = Decimal(numerator)
        denominator_decimal = Decimal(denominator)
        if action.action_type == "split":
            share_factor = numerator_decimal / denominator_decimal
            price_factor = denominator_decimal / numerator_decimal
        else:
            share_factor = (denominator_decimal + numerator_decimal) / denominator_decimal
            price_factor = denominator_decimal / (denominator_decimal + numerator_decimal)
        return CorporateActionPriceAdjustment(
            action=action,
            event_date=action.effective_date,
            action_type=action.action_type,
            ratio_numerator=numerator,
            ratio_denominator=denominator,
            share_factor=share_factor,
            price_factor=price_factor,
            available_at=action.available_at,
        )

    @staticmethod
    def _adjusted_bar(
        *,
        raw_bar: PointInTimeMarketBar,
        applicable_adjustments: tuple[CorporateActionPriceAdjustment, ...],
        basis_date: date,
        as_of: datetime,
    ) -> AdjustedMarketBar:
        applied = tuple(
            adjustment
            for adjustment in applicable_adjustments
            if raw_bar.trading_date < adjustment.event_date <= basis_date
        )
        factor = Decimal("1")
        available_at = raw_bar.available_at
        for adjustment in applied:
            factor *= adjustment.price_factor
            available_at = max(available_at, adjustment.available_at)
        return AdjustedMarketBar(
            raw_bar=raw_bar,
            adjusted_open=raw_bar.open_price * factor,
            adjusted_high=raw_bar.high_price * factor,
            adjusted_low=raw_bar.low_price * factor,
            adjusted_close=raw_bar.close_price * factor,
            cumulative_price_factor=factor,
            applied_adjustments=applied,
            adjustment_basis_date=basis_date,
            as_of=as_of,
            available_at=available_at,
        )

    @staticmethod
    def _simple_return(
        previous_close: Decimal, current_close: Decimal
    ) -> tuple[Decimal | None, tuple[str, ...]]:
        if previous_close <= Decimal("0"):
            return None, ("non_positive_previous_close",)
        return current_close / previous_close - Decimal("1"), ()

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
