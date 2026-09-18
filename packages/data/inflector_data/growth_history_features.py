"""PIT-clean fixed-window growth consistency and persistence primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from inflector_data.financial_features import FinancialInflectionFeatures, QuarterGrowthValue

GROWTH_WINDOW_VERSION = "growth_window_v1"
GROWTH_CONSISTENCY_VERSION = "growth_consistency_v1"
GROWTH_PERSISTENCE_VERSION = "growth_persistence_v1"


@dataclass(frozen=True, slots=True)
class ComparableGrowthWindow:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    window_size: int
    observations: tuple[QuarterGrowthValue, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class GrowthConsistencyValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    window_size: int
    positive_count: int
    non_positive_count: int
    positive_share: Decimal
    unit: str
    window: ComparableGrowthWindow
    as_of: datetime
    available_at: datetime
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class GrowthPersistenceValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    window_size: int
    threshold: Decimal
    streak_count: int
    window_saturated: bool
    streak_observations: tuple[QuarterGrowthValue, ...]
    stopping_observation: QuarterGrowthValue | None
    window: ComparableGrowthWindow
    as_of: datetime
    available_at: datetime
    algorithm_version: str


class GrowthHistoryFeatures:
    """Build complete comparable histories from public Phase 3D YoY series."""

    def __init__(self, financial_features: FinancialInflectionFeatures) -> None:
        self._financial_features = financial_features

    def growth_window_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        window_size: int,
        as_of: datetime,
    ) -> ComparableGrowthWindow | None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        cutoff = self._knowledge_cutoff(as_of)
        series = self._financial_features.growth_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            comparison_kind="yoy",
            as_of=cutoff,
        )
        endpoint_index = next(
            (
                index
                for index, observation in enumerate(series)
                if (observation.ending_fiscal_year, observation.ending_fiscal_quarter)
                == (fiscal_year, fiscal_quarter)
            ),
            None,
        )
        if endpoint_index is None or endpoint_index + 1 < window_size:
            return None
        observations = tuple(series[endpoint_index + 1 - window_size : endpoint_index + 1])
        if not self._valid_window(
            observations,
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
        ):
            return None
        return ComparableGrowthWindow(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            window_size=window_size,
            observations=observations,
            as_of=cutoff,
            available_at=max(observation.available_at for observation in observations),
            algorithm_version=GROWTH_WINDOW_VERSION,
        )

    def growth_consistency_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        window_size: int,
        as_of: datetime,
    ) -> GrowthConsistencyValue | None:
        window = self.growth_window_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            window_size=window_size,
            as_of=as_of,
        )
        if window is None:
            return None
        positive_count = sum(
            observation.value > Decimal("0")
            for observation in window.observations
            if observation.value is not None
        )
        return GrowthConsistencyValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            window_size=window_size,
            positive_count=positive_count,
            non_positive_count=window_size - positive_count,
            positive_share=Decimal(positive_count) / Decimal(window_size),
            unit="ratio",
            window=window,
            as_of=window.as_of,
            available_at=window.available_at,
            algorithm_version=GROWTH_CONSISTENCY_VERSION,
        )

    def growth_persistence_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        window_size: int,
        threshold: Decimal,
        as_of: datetime,
    ) -> GrowthPersistenceValue | None:
        if not isinstance(threshold, Decimal):
            raise TypeError("threshold must be a Decimal")
        window = self.growth_window_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            window_size=window_size,
            as_of=as_of,
        )
        if window is None:
            return None
        newest_first: list[QuarterGrowthValue] = []
        stopping_observation: QuarterGrowthValue | None = None
        for observation in reversed(window.observations):
            assert observation.value is not None
            if observation.value <= threshold:
                stopping_observation = observation
                break
            newest_first.append(observation)
        streak_observations = tuple(reversed(newest_first))
        streak_count = len(streak_observations)
        return GrowthPersistenceValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            window_size=window_size,
            threshold=threshold,
            streak_count=streak_count,
            window_saturated=streak_count == window_size,
            streak_observations=streak_observations,
            stopping_observation=stopping_observation,
            window=window,
            as_of=window.as_of,
            available_at=window.available_at,
            algorithm_version=GROWTH_PERSISTENCE_VERSION,
        )

    @classmethod
    def _valid_window(
        cls,
        observations: tuple[QuarterGrowthValue, ...],
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
    ) -> bool:
        if not observations:
            return False
        if (observations[-1].ending_fiscal_year, observations[-1].ending_fiscal_quarter) != (
            fiscal_year,
            fiscal_quarter,
        ):
            return False
        if any(
            observation.provider_dataset_id != provider_dataset_id
            or observation.company_id != company_id
            or observation.filing_scope != filing_scope
            or observation.metric_code != metric_code
            or observation.comparison_kind != "yoy"
            or observation.calculation_mode != "percentage_change"
            or not isinstance(observation.value, Decimal)
            for observation in observations
        ):
            return False
        return all(
            cls._adjacent(current, following)
            for current, following in zip(observations, observations[1:])
        )

    @staticmethod
    def _adjacent(current: QuarterGrowthValue, following: QuarterGrowthValue) -> bool:
        current_quarter = current.current_quarter
        following_quarter = following.current_quarter
        expected_quarter = 1 if current_quarter.fiscal_quarter == 4 else (
            current_quarter.fiscal_quarter + 1
        )
        expected_year = (
            current_quarter.fiscal_year + 1
            if current_quarter.fiscal_quarter == 4
            else current_quarter.fiscal_year
        )
        return (
            following_quarter.period_start == current_quarter.period_end + timedelta(days=1)
            and following_quarter.fiscal_quarter == expected_quarter
            and following_quarter.fiscal_year == expected_year
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
