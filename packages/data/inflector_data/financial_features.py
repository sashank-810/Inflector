"""Read-only point-in-time quarter growth, acceleration, and margin primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from inflector_data.period_normalization import FiscalQuarterNormalizer, QuarterizedFinancialValue

GROWTH_VERSION = "growth_v1"
ACCELERATION_VERSION = "growth_acceleration_v1"
MARGIN_VERSION = "margin_v1"
MARGIN_EXPANSION_VERSION = "margin_expansion_v1"


@dataclass(frozen=True, slots=True)
class QuarterGrowthValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    comparison_kind: str
    calculation_mode: str
    value: Decimal | None
    absolute_change: Decimal
    unit: str
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    current_quarter: QuarterizedFinancialValue
    comparison_quarter: QuarterizedFinancialValue
    transition: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GrowthAccelerationValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    current_yoy: QuarterGrowthValue
    prior_yoys: tuple[QuarterGrowthValue, ...]
    prior_median: Decimal
    acceleration: Decimal
    operation: str
    as_of: datetime
    available_at: datetime
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class QuarterMarginValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    margin_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    period_start: date
    period_end: date
    numerator_metric_code: str
    denominator_metric_code: str
    value: Decimal
    unit: str
    operation: str
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    numerator_quarter: QuarterizedFinancialValue
    denominator_quarter: QuarterizedFinancialValue


@dataclass(frozen=True, slots=True)
class MarginExpansionValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    margin_code: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    current_margin: QuarterMarginValue
    prior_year_margin: QuarterMarginValue
    change: Decimal
    basis_points: Decimal
    operation: str
    as_of: datetime
    available_at: datetime
    algorithm_version: str


class FinancialInflectionFeatures:
    """Build feature primitives solely from Phase 3B PIT quarterized values."""

    def __init__(self, quarter_normalizer: FiscalQuarterNormalizer) -> None:
        self._quarters = quarter_normalizer

    def quarter_growth_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        comparison_kind: str,
        as_of: datetime,
    ) -> QuarterGrowthValue | None:
        cutoff = self._cutoff(as_of)
        if comparison_kind not in {"yoy", "qoq"}:
            raise ValueError("comparison_kind must be yoy or qoq")
        series = self._series(provider_dataset_id, company_id, filing_scope, metric_code, cutoff)
        index = next(
            (
                i
                for i, q in enumerate(series)
                if (q.fiscal_year, q.fiscal_quarter) == (fiscal_year, fiscal_quarter)
            ),
            None,
        )
        offset = 4 if comparison_kind == "yoy" else 1
        if index is None or index < offset:
            return None
        pair = series[index - offset : index + 1]
        if not self._continuous(pair) or not self._eligible(pair):
            return None
        current, comparison = series[index], series[index - offset]
        change = current.value - comparison.value
        if comparison.value > 0:
            transition = "profit_to_loss" if current.value < 0 else None
            return QuarterGrowthValue(
                provider_dataset_id,
                company_id,
                filing_scope,
                metric_code,
                fiscal_year,
                fiscal_quarter,
                comparison_kind,
                "percentage_change",
                current.value / comparison.value - Decimal("1"),
                change,
                "ratio",
                cutoff,
                max(quarter.available_at for quarter in pair),
                GROWTH_VERSION,
                current,
                comparison,
                transition,
                (),
            )
        transition = self._transition(comparison.value, current.value)
        return QuarterGrowthValue(
            provider_dataset_id,
            company_id,
            filing_scope,
            metric_code,
            fiscal_year,
            fiscal_quarter,
            comparison_kind,
            "absolute_change",
            None,
            change,
            "INR",
            cutoff,
            max(quarter.available_at for quarter in pair),
            GROWTH_VERSION,
            current,
            comparison,
            transition,
            ("non_positive_comparison_base",),
        )

    def growth_series_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        comparison_kind: str,
        as_of: datetime,
    ) -> list[QuarterGrowthValue]:
        cutoff = self._cutoff(as_of)
        series = self._series(provider_dataset_id, company_id, filing_scope, metric_code, cutoff)
        return [
            result
            for q in series
            if (
                result := self.quarter_growth_as_of(
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    metric_code=metric_code,
                    fiscal_year=q.fiscal_year,
                    fiscal_quarter=q.fiscal_quarter,
                    comparison_kind=comparison_kind,
                    as_of=cutoff,
                )
            )
            is not None
        ]

    def growth_acceleration_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> GrowthAccelerationValue | None:
        cutoff = self._cutoff(as_of)
        values = self.growth_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            comparison_kind="yoy",
            as_of=cutoff,
        )
        index = next(
            (
                i
                for i, v in enumerate(values)
                if (v.ending_fiscal_year, v.ending_fiscal_quarter) == (fiscal_year, fiscal_quarter)
            ),
            None,
        )
        if index is None or index < 3:
            return None
        prior = tuple(values[index - 3 : index])
        current = values[index]
        all_values = (*prior, current)
        if any(v.calculation_mode != "percentage_change" or v.value is None for v in all_values):
            return None
        if not self._continuous([v.current_quarter for v in all_values]):
            return None
        prior_values = sorted(v.value for v in prior if v.value is not None)
        median = prior_values[1]
        assert current.value is not None
        return GrowthAccelerationValue(
            provider_dataset_id,
            company_id,
            filing_scope,
            metric_code,
            fiscal_year,
            fiscal_quarter,
            current,
            prior,
            median,
            current.value - median,
            "current_yoy_minus_prior3_median",
            cutoff,
            max(v.available_at for v in all_values),
            ACCELERATION_VERSION,
        )

    def quarter_margin_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        margin_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> QuarterMarginValue | None:
        numerator_code = {
            "operating_margin": "operating_profit",
            "ebitda_margin": "ebitda_reported",
        }.get(margin_code)
        if numerator_code is None:
            raise ValueError("unsupported margin_code")
        cutoff = self._cutoff(as_of)
        numerator = self._quarters.quarter_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=numerator_code,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
        )
        revenue = self._quarters.quarter_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code="revenue",
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
        )
        if (
            numerator is None
            or revenue is None
            or not self._eligible([numerator, revenue])
            or revenue.value <= 0
        ):
            return None
        if (numerator.period_start, numerator.period_end) != (
            revenue.period_start,
            revenue.period_end,
        ):
            return None
        return QuarterMarginValue(
            provider_dataset_id,
            company_id,
            filing_scope,
            margin_code,
            fiscal_year,
            fiscal_quarter,
            revenue.period_start,
            revenue.period_end,
            numerator_code,
            "revenue",
            numerator.value / revenue.value,
            "ratio",
            "divide_quarter_values",
            cutoff,
            max(numerator.available_at, revenue.available_at),
            MARGIN_VERSION,
            numerator,
            revenue,
        )

    def margin_expansion_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        margin_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> MarginExpansionValue | None:
        cutoff = self._cutoff(as_of)
        current = self.quarter_margin_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            margin_code=margin_code,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
        )
        prior = self.quarter_margin_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            margin_code=margin_code,
            fiscal_year=fiscal_year - 1,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
        )
        if current is None or prior is None:
            return None
        path = self._comparison_path(
            current.numerator_quarter,
            prior.numerator_quarter,
            provider_dataset_id,
            company_id,
            filing_scope,
            current.numerator_metric_code,
            cutoff,
        )
        if path is None:
            return None
        change = current.value - prior.value
        return MarginExpansionValue(
            provider_dataset_id,
            company_id,
            filing_scope,
            margin_code,
            fiscal_year,
            fiscal_quarter,
            current,
            prior,
            change,
            change * Decimal("10000"),
            "current_margin_minus_prior_year_margin",
            cutoff,
            max(
                current.available_at,
                prior.available_at,
                *(quarter.available_at for quarter in path),
            ),
            MARGIN_EXPANSION_VERSION,
        )

    def _series(
        self,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
    ) -> list[QuarterizedFinancialValue]:
        return self._quarters.quarter_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=as_of,
        )

    def _comparison_path(
        self,
        current: QuarterizedFinancialValue,
        prior: QuarterizedFinancialValue,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
    ) -> list[QuarterizedFinancialValue] | None:
        series = self._series(provider_dataset_id, company_id, filing_scope, metric_code, as_of)
        try:
            end = series.index(current)
            start = series.index(prior)
        except ValueError:
            return None
        path = series[start : end + 1]
        return path if end - start == 4 and self._continuous(path) else None

    @staticmethod
    def _eligible(quarters: list[QuarterizedFinancialValue]) -> bool:
        return bool(quarters) and all(
            q.unit == "INR"
            and isinstance(q.value, Decimal)
            and all(
                item.fact.metric.semantic_type == "duration"
                and item.fact.metric.unit_category == "monetary"
                for item in q.lineage
            )
            for q in quarters
        )

    @staticmethod
    def _continuous(quarters: list[QuarterizedFinancialValue]) -> bool:
        return all(
            next_q.period_start == current.period_end + timedelta(days=1)
            and next_q.fiscal_quarter
            == (1 if current.fiscal_quarter == 4 else current.fiscal_quarter + 1)
            and next_q.fiscal_year
            == (current.fiscal_year + 1 if current.fiscal_quarter == 4 else current.fiscal_year)
            for current, next_q in zip(quarters, quarters[1:])
        )

    @staticmethod
    def _transition(base: Decimal, current: Decimal) -> str:
        if base == 0:
            return (
                "zero_base_positive"
                if current > 0
                else "zero_base_negative"
                if current < 0
                else "zero_base_zero"
            )
        if current > 0:
            return "loss_to_profit"
        if current == base:
            return "loss_unchanged"
        return "loss_narrowing" if current > base else "loss_widening"

    @staticmethod
    def _cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
