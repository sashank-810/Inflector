"""Conservative point-in-time normalization of reported fiscal duration periods."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from inflector_data.pit import PointInTimeFinancialFact, PointInTimeFinancialReader

PERIOD_NORMALIZATION_VERSION = "period_normalization_v1"


@dataclass(frozen=True, slots=True)
class QuarterizationLineage:
    """One selected immutable fact and its role in a reported or derived quarter."""

    role: str
    fact: PointInTimeFinancialFact
    value: Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class QuarterizedFinancialValue:
    """An individual fiscal-quarter value with complete PIT source lineage."""

    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    fiscal_year: int
    fiscal_quarter: int
    period_start: date
    period_end: date
    value: Decimal
    unit: str
    derivation_kind: str
    operation: str
    algorithm_version: str
    as_of: datetime
    available_at: datetime
    lineage: tuple[QuarterizationLineage, ...]


class FiscalQuarterNormalizer:
    """Produce only supported individual quarters from point-in-time facts."""

    def __init__(self, reader: PointInTimeFinancialReader) -> None:
        self._reader = reader

    def quarter_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> QuarterizedFinancialValue | None:
        """Return a reported quarter or one conservatively derived YTD difference."""

        if fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter must be between 1 and 4")
        cutoff = self._knowledge_cutoff(as_of)
        facts = self._reader.financial_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=cutoff,
        )
        reported = self._reported_quarter(facts, fiscal_year, fiscal_quarter)
        if reported is not None:
            return self._reported_value(reported, cutoff)
        if not self._is_additive_monetary_metric(facts):
            return None
        return self._derive(facts, fiscal_year, fiscal_quarter, cutoff)

    def quarter_series_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
    ) -> list[QuarterizedFinancialValue]:
        """Return available reported/derived quarters without filling missing values."""

        cutoff = self._knowledge_cutoff(as_of)
        facts = self._reader.financial_series_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=cutoff,
        )
        years = sorted({fact.fiscal_period.fiscal_year for fact in facts})
        values = [
            value
            for fiscal_year in years
            for fiscal_quarter in (1, 2, 3, 4)
            if (
                value := self._quarter_from_facts(
                    facts, fiscal_year, fiscal_quarter, cutoff
                )
            )
            is not None
        ]
        return sorted(
            values,
            key=lambda value: (
                value.period_end,
                value.fiscal_year,
                value.fiscal_quarter,
            ),
        )

    def _quarter_from_facts(
        self,
        facts: list[PointInTimeFinancialFact],
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> QuarterizedFinancialValue | None:
        reported = self._reported_quarter(facts, fiscal_year, fiscal_quarter)
        if reported is not None:
            return self._reported_value(reported, as_of)
        if not self._is_additive_monetary_metric(facts):
            return None
        return self._derive(facts, fiscal_year, fiscal_quarter, as_of)

    @staticmethod
    def _reported_quarter(
        facts: list[PointInTimeFinancialFact], fiscal_year: int, fiscal_quarter: int
    ) -> PointInTimeFinancialFact | None:
        """Find an explicitly reported individual quarter, which takes precedence."""

        return next(
            (
                fact
                for fact in facts
                if fact.fiscal_period.fiscal_year == fiscal_year
                and fact.fiscal_period.period_kind == "quarter"
                and fact.fiscal_period.fiscal_quarter == fiscal_quarter
                and (fiscal_quarter == 1 or not fact.fiscal_period.is_ytd)
            ),
            None,
        )

    @staticmethod
    def _is_additive_monetary_metric(facts: list[PointInTimeFinancialFact]) -> bool:
        """Refuse subtraction unless controlled metric metadata proves it is safe."""

        return bool(facts) and all(
            fact.metric.semantic_type == "duration" and fact.metric.unit_category == "monetary"
            for fact in facts
        )

    def _derive(
        self,
        facts: list[PointInTimeFinancialFact],
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> QuarterizedFinancialValue | None:
        if fiscal_quarter == 2:
            h1 = self._find_period(facts, fiscal_year, "half_year", 2, True)
            q1 = self._find_period(facts, fiscal_year, "quarter", 1, None)
            if h1 is None or q1 is None:
                return None
            if not self._compatible_cumulative_pair(q1, h1):
                return None
            return self._difference("h1_minus_q1", h1, q1, 2, as_of)
        if fiscal_quarter == 3:
            nine_month = self._find_period(facts, fiscal_year, "nine_month", 3, True)
            h1 = self._find_period(facts, fiscal_year, "half_year", 2, True)
            if nine_month is None or h1 is None:
                return None
            if not self._compatible_cumulative_pair(h1, nine_month):
                return None
            return self._difference("nine_month_minus_h1", nine_month, h1, 3, as_of)
        if fiscal_quarter == 4:
            annual = self._find_period(facts, fiscal_year, "annual", None, None)
            nine_month = self._find_period(facts, fiscal_year, "nine_month", 3, True)
            if annual is None or nine_month is None:
                return None
            if not self._compatible_cumulative_pair(nine_month, annual):
                return None
            return self._difference("annual_minus_nine_month", annual, nine_month, 4, as_of)
        return None

    @staticmethod
    def _find_period(
        facts: list[PointInTimeFinancialFact],
        fiscal_year: int,
        period_kind: str,
        fiscal_quarter: int | None,
        is_ytd: bool | None,
    ) -> PointInTimeFinancialFact | None:
        return next(
            (
                fact
                for fact in facts
                if fact.fiscal_period.fiscal_year == fiscal_year
                and fact.fiscal_period.period_kind == period_kind
                and fact.fiscal_period.fiscal_quarter == fiscal_quarter
                and (is_ytd is None or fact.fiscal_period.is_ytd == is_ytd)
            ),
            None,
        )

    @staticmethod
    def _compatible_cumulative_pair(
        predecessor: PointInTimeFinancialFact, cumulative: PointInTimeFinancialFact
    ) -> bool:
        """Require exact stored fiscal boundaries and normalized INR compatibility."""

        return (
            predecessor.provider_dataset_id == cumulative.provider_dataset_id
            and predecessor.company_id == cumulative.company_id
            and predecessor.filing.filing_scope == cumulative.filing.filing_scope
            and predecessor.metric_code == cumulative.metric_code
            and predecessor.fiscal_period.fiscal_year == cumulative.fiscal_period.fiscal_year
            and predecessor.fiscal_period.period_start == cumulative.fiscal_period.period_start
            and predecessor.fiscal_period.period_end < cumulative.fiscal_period.period_end
            and predecessor.normalized_value is not None
            and cumulative.normalized_value is not None
            and predecessor.normalized_unit == cumulative.normalized_unit
            and predecessor.normalized_unit == "INR"
        )

    @staticmethod
    def _reported_value(
        fact: PointInTimeFinancialFact, as_of: datetime
    ) -> QuarterizedFinancialValue:
        value = fact.normalized_value if fact.normalized_value is not None else fact.reported_value
        unit = fact.normalized_unit if fact.normalized_unit is not None else fact.reported_unit
        return QuarterizedFinancialValue(
            provider_dataset_id=fact.provider_dataset_id,
            company_id=fact.company_id,
            filing_scope=fact.filing.filing_scope,
            metric_code=fact.metric_code,
            fiscal_year=fact.fiscal_period.fiscal_year,
            fiscal_quarter=fact.fiscal_period.fiscal_quarter or 1,
            period_start=fact.fiscal_period.period_start,
            period_end=fact.fiscal_period.period_end,
            value=value,
            unit=unit,
            derivation_kind="reported",
            operation="reported",
            algorithm_version=PERIOD_NORMALIZATION_VERSION,
            as_of=as_of,
            available_at=fact.available_at,
            lineage=(QuarterizationLineage("reported", fact, value, unit),),
        )

    @staticmethod
    def _difference(
        operation: str,
        minuend: PointInTimeFinancialFact,
        subtrahend: PointInTimeFinancialFact,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> QuarterizedFinancialValue:
        assert minuend.normalized_value is not None and minuend.normalized_unit is not None
        assert subtrahend.normalized_value is not None
        value = minuend.normalized_value - subtrahend.normalized_value
        unit = minuend.normalized_unit
        return QuarterizedFinancialValue(
            provider_dataset_id=minuend.provider_dataset_id,
            company_id=minuend.company_id,
            filing_scope=minuend.filing.filing_scope,
            metric_code=minuend.metric_code,
            fiscal_year=minuend.fiscal_period.fiscal_year,
            fiscal_quarter=fiscal_quarter,
            period_start=subtrahend.fiscal_period.period_end + timedelta(days=1),
            period_end=minuend.fiscal_period.period_end,
            value=value,
            unit=unit,
            derivation_kind="derived_ytd_difference",
            operation=operation,
            algorithm_version=PERIOD_NORMALIZATION_VERSION,
            as_of=as_of,
            available_at=max(minuend.available_at, subtrahend.available_at),
            lineage=(
                QuarterizationLineage("minuend", minuend, minuend.normalized_value, unit),
                QuarterizationLineage(
                    "subtrahend", subtrahend, subtrahend.normalized_value, unit
                ),
            ),
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
