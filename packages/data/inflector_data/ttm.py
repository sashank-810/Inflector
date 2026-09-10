"""Point-in-time trailing-twelve-month construction over quarterized values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from inflector_data.period_normalization import (
    FiscalQuarterNormalizer,
    QuarterizedFinancialValue,
)

TTM_ALGORITHM_VERSION = "ttm_v1"


@dataclass(frozen=True, slots=True)
class TTMQuarterComponent:
    """One quarter selected for a four-quarter TTM window."""

    role: str
    quarter: QuarterizedFinancialValue


@dataclass(frozen=True, slots=True)
class TrailingTwelveMonthValue:
    """A non-persisted, PIT-clean sum of four contiguous fiscal quarters."""

    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    period_start: date
    period_end: date
    value: Decimal
    unit: str
    construction_kind: str
    operation: str
    algorithm_version: str
    as_of: datetime
    available_at: datetime
    lineage: tuple[TTMQuarterComponent, ...]


class TrailingTwelveMonthNormalizer:
    """Build TTM values exclusively from PIT-clean individual fiscal quarters."""

    def __init__(self, quarter_normalizer: FiscalQuarterNormalizer) -> None:
        self._quarter_normalizer = quarter_normalizer

    def ttm_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> TrailingTwelveMonthValue | None:
        """Return the four-quarter TTM ending at one explicit fiscal quarter."""

        cutoff = self._knowledge_cutoff(as_of)
        quarters = self._quarter_normalizer.quarter_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=cutoff,
        )
        endpoint_index = next(
            (
                index
                for index, quarter in enumerate(quarters)
                if quarter.fiscal_year == fiscal_year and quarter.fiscal_quarter == fiscal_quarter
            ),
            None,
        )
        if endpoint_index is None or endpoint_index < 3:
            return None
        return self._from_window(quarters[endpoint_index - 3 : endpoint_index + 1], cutoff)

    def ttm_series_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
    ) -> list[TrailingTwelveMonthValue]:
        """Return every valid rolling four-quarter TTM in ascending period-end order."""

        cutoff = self._knowledge_cutoff(as_of)
        quarters = self._quarter_normalizer.quarter_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=cutoff,
        )
        return [
            ttm
            for endpoint_index in range(3, len(quarters))
            if (ttm := self._from_window(quarters[endpoint_index - 3 : endpoint_index + 1], cutoff))
            is not None
        ]

    @staticmethod
    def _from_window(
        quarters: list[QuarterizedFinancialValue], as_of: datetime
    ) -> TrailingTwelveMonthValue | None:
        if not TrailingTwelveMonthNormalizer._valid_window(quarters):
            return None
        first, *_, endpoint = quarters
        return TrailingTwelveMonthValue(
            provider_dataset_id=first.provider_dataset_id,
            company_id=first.company_id,
            filing_scope=first.filing_scope,
            metric_code=first.metric_code,
            ending_fiscal_year=endpoint.fiscal_year,
            ending_fiscal_quarter=endpoint.fiscal_quarter,
            period_start=first.period_start,
            period_end=endpoint.period_end,
            value=sum((quarter.value for quarter in quarters), start=Decimal("0")),
            unit="INR",
            construction_kind="four_quarters",
            operation="sum_four_quarters",
            algorithm_version=TTM_ALGORITHM_VERSION,
            as_of=as_of,
            available_at=max(quarter.available_at for quarter in quarters),
            lineage=tuple(
                TTMQuarterComponent(
                    f"quarter_t_minus_{3 - index}" if index < 3 else "quarter_t", quarter
                )
                for index, quarter in enumerate(quarters)
            ),
        )

    @staticmethod
    def _valid_window(quarters: list[QuarterizedFinancialValue]) -> bool:
        """Require one context, additive monetary facts, and exact fiscal continuity."""

        if len(quarters) != 4:
            return False
        first = quarters[0]
        if not all(
            quarter.provider_dataset_id == first.provider_dataset_id
            and quarter.company_id == first.company_id
            and quarter.filing_scope == first.filing_scope
            and quarter.metric_code == first.metric_code
            and quarter.unit == "INR"
            and isinstance(quarter.value, Decimal)
            and TrailingTwelveMonthNormalizer._is_additive_monetary(quarter)
            for quarter in quarters
        ):
            return False
        identities = {
            (quarter.fiscal_year, quarter.fiscal_quarter, quarter.period_start, quarter.period_end)
            for quarter in quarters
        }
        if len(identities) != 4:
            return False
        return all(
            TrailingTwelveMonthNormalizer._adjacent(previous, following)
            for previous, following in zip(quarters, quarters[1:])
        )

    @staticmethod
    def _is_additive_monetary(quarter: QuarterizedFinancialValue) -> bool:
        return bool(quarter.lineage) and all(
            component.fact.metric.semantic_type == "duration"
            and component.fact.metric.unit_category == "monetary"
            for component in quarter.lineage
        )

    @staticmethod
    def _adjacent(
        previous: QuarterizedFinancialValue, following: QuarterizedFinancialValue
    ) -> bool:
        expected_year = (
            previous.fiscal_year + 1 if previous.fiscal_quarter == 4 else previous.fiscal_year
        )
        expected_quarter = 1 if previous.fiscal_quarter == 4 else previous.fiscal_quarter + 1
        return (
            following.fiscal_year == expected_year
            and following.fiscal_quarter == expected_quarter
            and following.period_start == previous.period_end + timedelta(days=1)
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
