"""PIT-safe deterministic Market Structure evidence without scoring or persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from uuid import UUID

from inflector_data.market_adjustments import (
    AdjustedMarketBar,
    MarketAdjustmentPrimitives,
    SimplePriceReturn,
    simple_price_returns_from_adjusted_series,
)
from inflector_data.market_pit import (
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketDeliveryObservation,
    PointInTimeMarketReader,
)

SHORT_PRICE_WINDOW = 20
MEDIUM_PRICE_WINDOW = 60
SHORT_RETURN_WINDOW = 20
MEDIUM_RETURN_WINDOW = 60
RELATIVE_STRENGTH_RETURN_WINDOW = 60

MARKET_STRUCTURE_FEATURE_BUNDLE_VERSION = "market_structure_feature_bundle_v1"
RELATIVE_STRENGTH_60_VERSION = "relative_strength_60_to_benchmark_v1"
CLOSE_TO_SMA20_VERSION = "close_to_sma20_v1"
SMA20_TO_SMA60_VERSION = "sma20_to_sma60_v1"
RETURN_VOLATILITY_20_VERSION = "return_volatility_20_v1"
RETURN_VOLATILITY_60_VERSION = "return_volatility_60_v1"
VOLATILITY_RATIO_20_TO_60_VERSION = "volatility_ratio_20_to_60_v1"
CONSOLIDATION_RANGE_20_VERSION = "consolidation_range_20_v1"
AVERAGE_CLOSE_TIMES_VOLUME_20_VERSION = "average_close_times_volume_20_inr_v1"
CLOSE_TIMES_VOLUME_RATIO_20_TO_60_VERSION = "close_times_volume_ratio_20_to_60_v1"
AVERAGE_DELIVERY_PERCENTAGE_20_VERSION = "average_delivery_percentage_20_v1"


@dataclass(frozen=True, slots=True)
class RelativeStrengthEvidence:
    security_start: AdjustedMarketBar
    security_end: AdjustedMarketBar
    benchmark_start: PointInTimeBenchmarkBar | None
    benchmark_end: PointInTimeBenchmarkBar | None
    security_growth: Decimal | None
    benchmark_growth: Decimal | None


@dataclass(frozen=True, slots=True)
class MovingAverageEvidence:
    bars: tuple[AdjustedMarketBar, ...]
    sma20: Decimal | None
    sma60: Decimal | None
    latest_adjusted_close: Decimal | None
    basis_date: date | None


@dataclass(frozen=True, slots=True)
class VolatilityEvidence:
    returns: tuple[SimplePriceReturn, ...]
    mean_return: Decimal | None
    variance: Decimal | None


@dataclass(frozen=True, slots=True)
class VolatilityRatioEvidence:
    short_volatility: MarketStructureFeatureValue
    medium_volatility: MarketStructureFeatureValue


@dataclass(frozen=True, slots=True)
class ConsolidationEvidence:
    bars: tuple[AdjustedMarketBar, ...]
    highest_adjusted_high: Decimal | None
    lowest_adjusted_low: Decimal | None
    mean_adjusted_close: Decimal | None
    basis_date: date | None


@dataclass(frozen=True, slots=True)
class ActivityEvidence:
    bars: tuple[PointInTimeMarketBar, ...]
    recent_average: Decimal | None
    medium_average: Decimal | None
    basis_date: date | None


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    bars: tuple[PointInTimeMarketBar, ...]
    basis_date: date | None


@dataclass(frozen=True, slots=True)
class IndependentDeliveryEvidence:
    """Separate official delivery observations aligned to the price window."""

    observations: tuple[PointInTimeMarketDeliveryObservation, ...]
    expected_trading_dates: tuple[date, ...]
    basis_date: date | None


@dataclass(frozen=True, slots=True)
class MarketStructureFeatureValue:
    code: str
    value: Decimal | None
    unit: str
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str
    evidence: object


@dataclass(frozen=True, slots=True)
class MarketStructureFeatureBundle:
    market_provider_dataset_id: UUID
    corporate_action_provider_dataset_id: UUID
    benchmark_provider_dataset_id: UUID
    benchmark_code: str
    security_id: UUID
    interval: str
    market_on_or_before: date | None
    as_of: datetime
    basis_date: date | None
    relative_strength_60_to_benchmark: MarketStructureFeatureValue
    close_to_sma20: MarketStructureFeatureValue
    sma20_to_sma60: MarketStructureFeatureValue
    return_volatility_20: MarketStructureFeatureValue
    return_volatility_60: MarketStructureFeatureValue
    volatility_ratio_20_to_60: MarketStructureFeatureValue
    consolidation_range_20: MarketStructureFeatureValue
    average_close_times_volume_20_inr: MarketStructureFeatureValue
    close_times_volume_ratio_20_to_60: MarketStructureFeatureValue
    average_delivery_percentage_20: MarketStructureFeatureValue
    algorithm_version: str


class MarketStructureFeaturePrimitives:
    """Compose approved PIT observations into non-scored market evidence."""

    def __init__(
        self,
        market_reader: PointInTimeMarketReader,
        market_adjustments: MarketAdjustmentPrimitives,
    ) -> None:
        self._market_reader = market_reader
        self._market_adjustments = market_adjustments

    def features_as_of(
        self,
        *,
        market_provider_dataset_id: UUID,
        corporate_action_provider_dataset_id: UUID,
        benchmark_provider_dataset_id: UUID,
        benchmark_code: str,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        market_on_or_before: date | None = None,
        delivery_provider_dataset_id: UUID | None = None,
        delivery_series: str = "EQ",
        delivery_observation_window: int = SHORT_PRICE_WINDOW,
    ) -> MarketStructureFeatureBundle:
        cutoff = self._knowledge_cutoff(as_of)
        if not benchmark_code.strip():
            raise ValueError("benchmark_code must be non-empty")
        adjusted = self._market_adjustments.adjusted_market_series_as_of(
            market_provider_dataset_id=market_provider_dataset_id,
            corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=cutoff,
            end_date=market_on_or_before,
        )
        bars = adjusted.bars
        raw_bars = tuple(bar.raw_bar for bar in bars)
        returns = simple_price_returns_from_adjusted_series(adjusted)
        basis_date = adjusted.adjustment_basis_date

        close_to_sma20 = self._close_to_sma20(bars, basis_date, cutoff)
        sma20_to_sma60 = self._sma20_to_sma60(bars, basis_date, cutoff)
        volatility20 = self._volatility(
            returns,
            SHORT_RETURN_WINDOW,
            "return_volatility_20",
            RETURN_VOLATILITY_20_VERSION,
            cutoff,
        )
        volatility60 = self._volatility(
            returns,
            MEDIUM_RETURN_WINDOW,
            "return_volatility_60",
            RETURN_VOLATILITY_60_VERSION,
            cutoff,
        )
        return MarketStructureFeatureBundle(
            market_provider_dataset_id=market_provider_dataset_id,
            corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
            benchmark_provider_dataset_id=benchmark_provider_dataset_id,
            benchmark_code=benchmark_code,
            security_id=security_id,
            interval=interval,
            market_on_or_before=market_on_or_before,
            as_of=cutoff,
            basis_date=basis_date,
            relative_strength_60_to_benchmark=self._relative_strength(
                bars,
                benchmark_provider_dataset_id,
                benchmark_code,
                interval,
                cutoff,
            ),
            close_to_sma20=close_to_sma20,
            sma20_to_sma60=sma20_to_sma60,
            return_volatility_20=volatility20,
            return_volatility_60=volatility60,
            volatility_ratio_20_to_60=self._volatility_ratio(volatility20, volatility60, cutoff),
            consolidation_range_20=self._consolidation(bars, basis_date, cutoff),
            average_close_times_volume_20_inr=self._activity_level(raw_bars, basis_date, cutoff),
            close_times_volume_ratio_20_to_60=self._activity_ratio(raw_bars, basis_date, cutoff),
            average_delivery_percentage_20=(
                self._delivery(raw_bars, basis_date, cutoff)
                if delivery_provider_dataset_id is None
                else self._independent_delivery(
                    raw_bars,
                    basis_date,
                    cutoff,
                    delivery_provider_dataset_id=delivery_provider_dataset_id,
                    security_id=security_id,
                    series=delivery_series,
                    observation_window=delivery_observation_window,
                )
            ),
            algorithm_version=MARKET_STRUCTURE_FEATURE_BUNDLE_VERSION,
        )

    def _relative_strength(
        self,
        bars: tuple[AdjustedMarketBar, ...],
        benchmark_provider_dataset_id: UUID,
        benchmark_code: str,
        interval: str,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        partial = bars[-(RELATIVE_STRENGTH_RETURN_WINDOW + 1) :]
        if len(partial) < RELATIVE_STRENGTH_RETURN_WINDOW + 1:
            return self._feature(
                "relative_strength_60_to_benchmark",
                None,
                "ratio",
                ("insufficient_relative_strength_history",),
                as_of,
                self._adjusted_available_at(partial),
                RELATIVE_STRENGTH_60_VERSION,
                MovingAverageEvidence(partial, None, None, None, self._basis(partial)),
            )
        start, end = partial[0], partial[-1]
        start_date = start.raw_bar.trading_date
        end_date = end.raw_bar.trading_date
        benchmark_start = self._market_reader.benchmark_bar_as_of(
            provider_dataset_id=benchmark_provider_dataset_id,
            benchmark_code=benchmark_code,
            trading_date=start_date,
            interval=interval,
            as_of=as_of,
        )
        benchmark_end = self._market_reader.benchmark_bar_as_of(
            provider_dataset_id=benchmark_provider_dataset_id,
            benchmark_code=benchmark_code,
            trading_date=end_date,
            interval=interval,
            as_of=as_of,
        )
        warning: str | None = None
        if benchmark_start is None:
            warning = "missing_benchmark_start_bar"
        elif benchmark_end is None:
            warning = "missing_benchmark_end_bar"
        elif start.adjusted_close <= 0:
            warning = "non_positive_security_start_close"
        elif benchmark_start.close_value <= 0:
            warning = "non_positive_benchmark_start_close"
        elif benchmark_end.close_value <= 0:
            warning = "non_positive_benchmark_end_close"
        security_growth = (
            end.adjusted_close / start.adjusted_close if start.adjusted_close > 0 else None
        )
        benchmark_growth = (
            benchmark_end.close_value / benchmark_start.close_value
            if benchmark_start is not None
            and benchmark_end is not None
            and benchmark_start.close_value > 0
            else None
        )
        value = (
            security_growth / benchmark_growth - Decimal("1")
            if warning is None
            and security_growth is not None
            and benchmark_growth is not None
            and benchmark_growth > 0
            else None
        )
        evidence = RelativeStrengthEvidence(
            start,
            end,
            benchmark_start,
            benchmark_end,
            security_growth,
            benchmark_growth,
        )
        return self._feature(
            "relative_strength_60_to_benchmark",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._available_at(start, end, benchmark_start, benchmark_end),
            RELATIVE_STRENGTH_60_VERSION,
            evidence,
        )

    def _close_to_sma20(
        self,
        bars: tuple[AdjustedMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = bars[-SHORT_PRICE_WINDOW:]
        if len(window) < SHORT_PRICE_WINDOW:
            return self._feature(
                "close_to_sma20",
                None,
                "ratio",
                ("insufficient_price_history",),
                as_of,
                self._adjusted_available_at(window),
                CLOSE_TO_SMA20_VERSION,
                MovingAverageEvidence(
                    window,
                    None,
                    None,
                    window[-1].adjusted_close if window else None,
                    basis_date,
                ),
            )
        sma20 = self._mean(tuple(bar.adjusted_close for bar in window))
        warning = "non_positive_sma20" if sma20 <= 0 else None
        value = window[-1].adjusted_close / sma20 - Decimal("1") if warning is None else None
        return self._feature(
            "close_to_sma20",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._adjusted_available_at(window),
            CLOSE_TO_SMA20_VERSION,
            MovingAverageEvidence(window, sma20, None, window[-1].adjusted_close, basis_date),
        )

    def _sma20_to_sma60(
        self,
        bars: tuple[AdjustedMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = bars[-MEDIUM_PRICE_WINDOW:]
        if len(window) < MEDIUM_PRICE_WINDOW:
            return self._feature(
                "sma20_to_sma60",
                None,
                "ratio",
                ("insufficient_price_history",),
                as_of,
                self._adjusted_available_at(window),
                SMA20_TO_SMA60_VERSION,
                MovingAverageEvidence(
                    window,
                    None,
                    None,
                    window[-1].adjusted_close if window else None,
                    basis_date,
                ),
            )
        sma20 = self._mean(tuple(bar.adjusted_close for bar in window[-SHORT_PRICE_WINDOW:]))
        sma60 = self._mean(tuple(bar.adjusted_close for bar in window))
        warning = "non_positive_sma60" if sma60 <= 0 else None
        value = sma20 / sma60 - Decimal("1") if warning is None else None
        return self._feature(
            "sma20_to_sma60",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._adjusted_available_at(window),
            SMA20_TO_SMA60_VERSION,
            MovingAverageEvidence(window, sma20, sma60, window[-1].adjusted_close, basis_date),
        )

    def _volatility(
        self,
        returns: tuple[SimplePriceReturn, ...],
        window_size: int,
        code: str,
        version: str,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = returns[-window_size:]
        if len(window) < window_size:
            return self._feature(
                code,
                None,
                "ratio",
                ("insufficient_return_history",),
                as_of,
                self._return_available_at(window),
                version,
                VolatilityEvidence(window, None, None),
            )
        if any(item.value is None for item in window):
            return self._feature(
                code,
                None,
                "ratio",
                ("invalid_return_in_window",),
                as_of,
                self._return_available_at(window),
                version,
                VolatilityEvidence(window, None, None),
            )
        values = tuple(item.value for item in window if item.value is not None)
        with localcontext() as context:
            context.prec = 50
            mean = sum(values, Decimal("0")) / Decimal(window_size)
            variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(
                window_size
            )
            volatility = variance.sqrt()
        return self._feature(
            code,
            volatility,
            "ratio",
            (),
            as_of,
            self._return_available_at(window),
            version,
            VolatilityEvidence(window, mean, variance),
        )

    def _volatility_ratio(
        self,
        short: MarketStructureFeatureValue,
        medium: MarketStructureFeatureValue,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        warning: str | None = None
        if short.value is None:
            warning = short.warnings[0]
        elif medium.value is None:
            warning = medium.warnings[0]
        elif medium.value == 0:
            warning = "zero_medium_volatility"
        value = (
            short.value / medium.value
            if warning is None and short.value is not None and medium.value is not None
            else None
        )
        return self._feature(
            "volatility_ratio_20_to_60",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._available_at(short, medium),
            VOLATILITY_RATIO_20_TO_60_VERSION,
            VolatilityRatioEvidence(short, medium),
        )

    def _consolidation(
        self,
        bars: tuple[AdjustedMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = bars[-SHORT_PRICE_WINDOW:]
        if len(window) < SHORT_PRICE_WINDOW:
            evidence = ConsolidationEvidence(window, None, None, None, basis_date)
            return self._feature(
                "consolidation_range_20",
                None,
                "ratio",
                ("insufficient_price_history",),
                as_of,
                self._adjusted_available_at(window),
                CONSOLIDATION_RANGE_20_VERSION,
                evidence,
            )
        highest = max(bar.adjusted_high for bar in window)
        lowest = min(bar.adjusted_low for bar in window)
        mean_close = self._mean(tuple(bar.adjusted_close for bar in window))
        warning = "non_positive_consolidation_mean_close" if mean_close <= 0 else None
        value = (highest - lowest) / mean_close if warning is None else None
        return self._feature(
            "consolidation_range_20",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._adjusted_available_at(window),
            CONSOLIDATION_RANGE_20_VERSION,
            ConsolidationEvidence(window, highest, lowest, mean_close, basis_date),
        )

    def _activity_level(
        self,
        bars: tuple[PointInTimeMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = bars[-SHORT_PRICE_WINDOW:]
        average = self._activity_average(window) if len(window) == SHORT_PRICE_WINDOW else None
        return self._feature(
            "average_close_times_volume_20_inr",
            average,
            "INR_proxy",
            () if average is not None else ("insufficient_price_history",),
            as_of,
            self._raw_available_at(window),
            AVERAGE_CLOSE_TIMES_VOLUME_20_VERSION,
            ActivityEvidence(window, average, None, basis_date),
        )

    def _activity_ratio(
        self,
        bars: tuple[PointInTimeMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = bars[-MEDIUM_PRICE_WINDOW:]
        if len(window) < MEDIUM_PRICE_WINDOW:
            return self._feature(
                "close_times_volume_ratio_20_to_60",
                None,
                "ratio",
                ("insufficient_price_history",),
                as_of,
                self._raw_available_at(window),
                CLOSE_TIMES_VOLUME_RATIO_20_TO_60_VERSION,
                ActivityEvidence(window, None, None, basis_date),
            )
        recent = self._activity_average(window[-SHORT_PRICE_WINDOW:])
        medium = self._activity_average(window)
        warning = "zero_medium_activity_proxy" if medium == 0 else None
        value = recent / medium if warning is None else None
        return self._feature(
            "close_times_volume_ratio_20_to_60",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._raw_available_at(window),
            CLOSE_TIMES_VOLUME_RATIO_20_TO_60_VERSION,
            ActivityEvidence(window, recent, medium, basis_date),
        )

    def _delivery(
        self,
        bars: tuple[PointInTimeMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
    ) -> MarketStructureFeatureValue:
        window = bars[-SHORT_PRICE_WINDOW:]
        warning: str | None = None
        value: Decimal | None = None
        if len(window) < SHORT_PRICE_WINDOW:
            warning = "insufficient_price_history"
        elif any(bar.delivery_percentage is None for bar in window):
            warning = "incomplete_delivery_window"
        else:
            percentages = tuple(
                bar.delivery_percentage for bar in window if bar.delivery_percentage is not None
            )
            if any(value < 0 or value > 1 for value in percentages):
                raise ValueError("delivery percentage evidence must be in [0, 1]")
            value = sum(percentages, Decimal("0")) / Decimal(SHORT_PRICE_WINDOW)
        return self._feature(
            "average_delivery_percentage_20",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            self._raw_available_at(window),
            AVERAGE_DELIVERY_PERCENTAGE_20_VERSION,
            DeliveryEvidence(window, basis_date),
        )

    def _independent_delivery(
        self,
        bars: tuple[PointInTimeMarketBar, ...],
        basis_date: date | None,
        as_of: datetime,
        *,
        delivery_provider_dataset_id: UUID,
        security_id: UUID,
        series: str,
        observation_window: int,
    ) -> MarketStructureFeatureValue:
        if observation_window != SHORT_PRICE_WINDOW:
            raise ValueError("delivery observation window must match the accepted 20-bar primitive")
        price_window = bars[-observation_window:]
        expected_dates = tuple(bar.trading_date for bar in price_window)
        observations = self._market_reader.market_delivery_series_as_of(
            provider_dataset_id=delivery_provider_dataset_id,
            security_id=security_id,
            series=series,
            as_of=as_of,
            start_date=expected_dates[0] if expected_dates else None,
            end_date=expected_dates[-1] if expected_dates else None,
        )
        by_date = {item.trading_date: item for item in observations}
        aligned = tuple(by_date[item] for item in expected_dates if item in by_date)
        warning: str | None = None
        value: Decimal | None = None
        if len(price_window) < observation_window:
            warning = "insufficient_price_history"
        elif len(aligned) != observation_window or any(
            item.delivery_percentage is None for item in aligned
        ):
            warning = "incomplete_delivery_window"
        else:
            percentages = tuple(
                item.delivery_percentage for item in aligned if item.delivery_percentage is not None
            )
            if any(item < 0 or item > 1 for item in percentages):
                raise ValueError("delivery percentage evidence must be in [0, 1]")
            value = sum(percentages, Decimal("0")) / Decimal(observation_window)
        available_at = max((item.available_at for item in aligned), default=None)
        return self._feature(
            "average_delivery_percentage_20",
            value,
            "ratio",
            () if warning is None else (warning,),
            as_of,
            available_at,
            AVERAGE_DELIVERY_PERCENTAGE_20_VERSION,
            IndependentDeliveryEvidence(aligned, expected_dates, basis_date),
        )

    @staticmethod
    def _feature(
        code: str,
        value: Decimal | None,
        unit: str,
        warnings: tuple[str, ...],
        as_of: datetime,
        available_at: datetime | None,
        algorithm_version: str,
        evidence: object,
    ) -> MarketStructureFeatureValue:
        return MarketStructureFeatureValue(
            code=code,
            value=value,
            unit=unit,
            warnings=warnings,
            as_of=as_of,
            available_at=available_at,
            algorithm_version=algorithm_version,
            evidence=evidence,
        )

    @staticmethod
    def _mean(values: tuple[Decimal, ...]) -> Decimal:
        return sum(values, Decimal("0")) / Decimal(len(values))

    @classmethod
    def _activity_average(cls, bars: tuple[PointInTimeMarketBar, ...]) -> Decimal:
        values = tuple(bar.close_price * Decimal(bar.volume) for bar in bars)
        return cls._mean(values)

    @staticmethod
    def _basis(bars: tuple[AdjustedMarketBar, ...]) -> date | None:
        return bars[-1].raw_bar.trading_date if bars else None

    @staticmethod
    def _adjusted_available_at(bars: tuple[AdjustedMarketBar, ...]) -> datetime | None:
        return max((bar.available_at for bar in bars), default=None)

    @staticmethod
    def _raw_available_at(bars: tuple[PointInTimeMarketBar, ...]) -> datetime | None:
        return max((bar.available_at for bar in bars), default=None)

    @staticmethod
    def _return_available_at(returns: tuple[SimplePriceReturn, ...]) -> datetime | None:
        return max((item.available_at for item in returns), default=None)

    @staticmethod
    def _available_at(*items: object | None) -> datetime | None:
        timestamps = [
            timestamp
            for item in items
            if item is not None and (timestamp := getattr(item, "available_at")) is not None
        ]
        return max(timestamps) if timestamps else None

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
