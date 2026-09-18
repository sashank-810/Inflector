"""PIT-clean cash-flow quality and trade working-capital primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from inflector_data.financial_snapshots import (
    InstantFinancialSnapshot,
    InstantFinancialSnapshotReader,
)
from inflector_data.ttm import TrailingTwelveMonthNormalizer, TrailingTwelveMonthValue

CFO_CONVERSION_VERSION = "cfo_conversion_v1"
CFO_EBITDA_VERSION = "cfo_ebitda_v1"
RECEIVABLE_DAYS_VERSION = "receivable_days_v1"
TRADE_WORKING_CAPITAL_CHANGE_VERSION = "trade_working_capital_change_v1"


@dataclass(frozen=True, slots=True)
class CashFlowConversionValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    cfo: Decimal
    pat: Decimal
    cash_minus_pat: Decimal
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    ttm_cfo: TrailingTwelveMonthValue
    ttm_pat: TrailingTwelveMonthValue


@dataclass(frozen=True, slots=True)
class CashFlowToEbitdaValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    cfo: Decimal
    ebitda: Decimal
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    ttm_cfo: TrailingTwelveMonthValue
    ttm_ebitda: TrailingTwelveMonthValue


@dataclass(frozen=True, slots=True)
class ReceivableDaysValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    beginning_receivables: Decimal
    ending_receivables: Decimal
    average_receivables: Decimal | None
    warnings: tuple[str, ...]
    ttm_revenue: TrailingTwelveMonthValue
    beginning_snapshot: InstantFinancialSnapshot
    ending_snapshot: InstantFinancialSnapshot
    as_of: datetime
    available_at: datetime
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class TradeWorkingCapitalChangeValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    beginning_receivables: Decimal
    beginning_inventory: Decimal
    beginning_payables: Decimal
    beginning_trade_working_capital: Decimal | None
    ending_receivables: Decimal
    ending_inventory: Decimal
    ending_payables: Decimal
    ending_trade_working_capital: Decimal | None
    warnings: tuple[str, ...]
    ttm_revenue: TrailingTwelveMonthValue
    beginning_snapshot: InstantFinancialSnapshot
    ending_snapshot: InstantFinancialSnapshot
    as_of: datetime
    available_at: datetime
    algorithm_version: str


class CashFlowQualityFeatures:
    """Calculate cash-flow and working-capital primitives from explicit PIT readers."""

    def __init__(
        self,
        snapshot_reader: InstantFinancialSnapshotReader,
        ttm_normalizer: TrailingTwelveMonthNormalizer,
    ) -> None:
        self._snapshot_reader = snapshot_reader
        self._ttm_normalizer = ttm_normalizer

    def cfo_conversion_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> CashFlowConversionValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        cfo = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "cash_flow_from_operations",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        pat = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "pat",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        if not self._matching_ttms(cfo, pat):
            return None
        assert cfo is not None and pat is not None
        valid = pat.value > 0
        return CashFlowConversionValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=cfo.value / pat.value if valid else None,
            unit="ratio",
            cfo=cfo.value,
            pat=pat.value,
            cash_minus_pat=cfo.value - pat.value,
            warnings=() if valid else ("non_positive_pat",),
            as_of=cutoff,
            available_at=max(cfo.available_at, pat.available_at),
            algorithm_version=CFO_CONVERSION_VERSION,
            ttm_cfo=cfo,
            ttm_pat=pat,
        )

    def cfo_to_ebitda_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> CashFlowToEbitdaValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        cfo = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "cash_flow_from_operations",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        ebitda = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "ebitda_reported",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        if not self._matching_ttms(cfo, ebitda):
            return None
        assert cfo is not None and ebitda is not None
        valid = ebitda.value > 0
        return CashFlowToEbitdaValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=cfo.value / ebitda.value if valid else None,
            unit="ratio",
            cfo=cfo.value,
            ebitda=ebitda.value,
            warnings=() if valid else ("non_positive_ebitda",),
            as_of=cutoff,
            available_at=max(cfo.available_at, ebitda.available_at),
            algorithm_version=CFO_EBITDA_VERSION,
            ttm_cfo=cfo,
            ttm_ebitda=ebitda,
        )

    def receivable_days_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> ReceivableDaysValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        revenue = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "revenue",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        if revenue is None:
            return None
        boundaries = self._boundary_snapshots(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=("trade_receivables",),
            period_start=revenue.period_start,
            period_end=revenue.period_end,
            as_of=cutoff,
        )
        if boundaries is None:
            return None
        beginning, ending = boundaries
        beginning_receivables = self._component_values(beginning)["trade_receivables"]
        ending_receivables = self._component_values(ending)["trade_receivables"]
        receivables_valid = beginning_receivables >= 0 and ending_receivables >= 0
        average = (
            (beginning_receivables + ending_receivables) / Decimal("2")
            if receivables_valid
            else None
        )
        if not receivables_valid:
            value = None
            warnings = ("negative_trade_receivables",)
        elif revenue.value <= 0:
            value = None
            warnings = ("non_positive_revenue",)
        else:
            assert average is not None
            value = average / revenue.value * Decimal("365")
            warnings = ()
        return ReceivableDaysValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=value,
            unit="days",
            beginning_receivables=beginning_receivables,
            ending_receivables=ending_receivables,
            average_receivables=average,
            warnings=warnings,
            ttm_revenue=revenue,
            beginning_snapshot=beginning,
            ending_snapshot=ending,
            as_of=cutoff,
            available_at=max(revenue.available_at, beginning.available_at, ending.available_at),
            algorithm_version=RECEIVABLE_DAYS_VERSION,
        )

    def trade_working_capital_change_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> TradeWorkingCapitalChangeValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        revenue = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "revenue",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        if revenue is None:
            return None
        metrics = ("trade_receivables", "inventory", "trade_payables")
        boundaries = self._boundary_snapshots(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=metrics,
            period_start=revenue.period_start,
            period_end=revenue.period_end,
            as_of=cutoff,
        )
        if boundaries is None:
            return None
        beginning, ending = boundaries
        beginning_values = self._component_values(beginning)
        ending_values = self._component_values(ending)
        beginning_components = tuple(beginning_values[metric] for metric in metrics)
        ending_components = tuple(ending_values[metric] for metric in metrics)
        beginning_valid = all(value >= 0 for value in beginning_components)
        ending_valid = all(value >= 0 for value in ending_components)
        beginning_working_capital = (
            self._trade_working_capital(beginning_values) if beginning_valid else None
        )
        ending_working_capital = (
            self._trade_working_capital(ending_values) if ending_valid else None
        )
        valid = beginning_valid and ending_valid
        value = (
            ending_working_capital - beginning_working_capital
            if ending_working_capital is not None and beginning_working_capital is not None
            else None
        )
        return TradeWorkingCapitalChangeValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=value,
            unit="INR",
            beginning_receivables=beginning_values["trade_receivables"],
            beginning_inventory=beginning_values["inventory"],
            beginning_payables=beginning_values["trade_payables"],
            beginning_trade_working_capital=beginning_working_capital,
            ending_receivables=ending_values["trade_receivables"],
            ending_inventory=ending_values["inventory"],
            ending_payables=ending_values["trade_payables"],
            ending_trade_working_capital=ending_working_capital,
            warnings=() if valid else ("negative_working_capital_component",),
            ttm_revenue=revenue,
            beginning_snapshot=beginning,
            ending_snapshot=ending,
            as_of=cutoff,
            available_at=max(revenue.available_at, beginning.available_at, ending.available_at),
            algorithm_version=TRADE_WORKING_CAPITAL_CHANGE_VERSION,
        )

    def _ttm(
        self,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> TrailingTwelveMonthValue | None:
        value = self._ttm_normalizer.ttm_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=as_of,
        )
        if value is None or not self._valid_ttm_context(
            value,
            provider_dataset_id,
            company_id,
            filing_scope,
            metric_code,
            fiscal_year,
            fiscal_quarter,
        ):
            return None
        return value

    def _boundary_snapshots(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_codes: tuple[str, ...],
        period_start: date,
        period_end: date,
        as_of: datetime,
    ) -> tuple[InstantFinancialSnapshot, InstantFinancialSnapshot] | None:
        beginning = self._snapshot_reader.common_snapshot_for_period_end_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            period_end=period_start - timedelta(days=1),
            metric_codes=metric_codes,
            as_of=as_of,
        )
        ending = self._snapshot_reader.common_snapshot_for_period_end_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            period_end=period_end,
            metric_codes=metric_codes,
            as_of=as_of,
        )
        return None if beginning is None or ending is None else (beginning, ending)

    @staticmethod
    def _component_values(snapshot: InstantFinancialSnapshot) -> dict[str, Decimal]:
        return {component.metric_code: component.value for component in snapshot.components}

    @staticmethod
    def _trade_working_capital(values: dict[str, Decimal]) -> Decimal:
        return values["trade_receivables"] + values["inventory"] - values["trade_payables"]

    @staticmethod
    def _valid_ttm_context(
        value: TrailingTwelveMonthValue,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
    ) -> bool:
        return (
            value.provider_dataset_id == provider_dataset_id
            and value.company_id == company_id
            and value.filing_scope == filing_scope
            and value.metric_code == metric_code
            and value.ending_fiscal_year == fiscal_year
            and value.ending_fiscal_quarter == fiscal_quarter
            and value.unit == "INR"
            and isinstance(value.value, Decimal)
        )

    @staticmethod
    def _matching_ttms(
        first: TrailingTwelveMonthValue | None,
        second: TrailingTwelveMonthValue | None,
    ) -> bool:
        if first is None or second is None:
            return False
        return (
            first.provider_dataset_id == second.provider_dataset_id
            and first.company_id == second.company_id
            and first.filing_scope == second.filing_scope
            and first.ending_fiscal_year == second.ending_fiscal_year
            and first.ending_fiscal_quarter == second.ending_fiscal_quarter
            and first.period_start == second.period_start
            and first.period_end == second.period_end
            and first.unit == second.unit == "INR"
            and isinstance(first.value, Decimal)
            and isinstance(second.value, Decimal)
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
