"""PIT-clean leverage and capital-efficiency primitives."""

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

NET_DEBT_VERSION = "net_debt_v1"
DEBT_EQUITY_VERSION = "debt_equity_v1"
INTEREST_COVERAGE_VERSION = "interest_coverage_v1"
ROE_VERSION = "roe_v1"
ROCE_VERSION = "roce_v1"


@dataclass(frozen=True, slots=True)
class NetDebtValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    value: Decimal
    unit: str
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    snapshot: InstantFinancialSnapshot


@dataclass(frozen=True, slots=True)
class DebtToEquityValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    value: Decimal | None
    unit: str
    debt: Decimal
    equity: Decimal
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str
    snapshot: InstantFinancialSnapshot


@dataclass(frozen=True, slots=True)
class InterestCoverageValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    ttm_ebit: TrailingTwelveMonthValue
    ttm_finance_cost: TrailingTwelveMonthValue
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class ReturnOnEquityValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    ttm_pat: TrailingTwelveMonthValue
    beginning_snapshot: InstantFinancialSnapshot
    ending_snapshot: InstantFinancialSnapshot
    beginning_equity: Decimal
    ending_equity: Decimal
    average_equity: Decimal | None
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class ReturnOnCapitalEmployedValue:
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    value: Decimal | None
    unit: str
    ttm_ebit: TrailingTwelveMonthValue
    beginning_snapshot: InstantFinancialSnapshot
    ending_snapshot: InstantFinancialSnapshot
    beginning_capital_employed: Decimal
    ending_capital_employed: Decimal
    average_capital_employed: Decimal | None
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str


class CapitalEfficiencyFeatures:
    """Calculate conservative ratios from explicit PIT readers."""

    def __init__(
        self,
        snapshot_reader: InstantFinancialSnapshotReader,
        ttm_normalizer: TrailingTwelveMonthNormalizer,
    ) -> None:
        self._snapshot_reader = snapshot_reader
        self._ttm_normalizer = ttm_normalizer

    def net_debt_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        as_of: datetime,
    ) -> NetDebtValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        snapshot = self._snapshot_reader.latest_common_snapshot_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=("total_debt", "cash_and_equivalents"),
            as_of=cutoff,
        )
        if snapshot is None:
            return None
        values = self._component_values(snapshot)
        return NetDebtValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            value=values["total_debt"] - values["cash_and_equivalents"],
            unit="INR",
            as_of=cutoff,
            available_at=snapshot.available_at,
            algorithm_version=NET_DEBT_VERSION,
            snapshot=snapshot,
        )

    def debt_to_equity_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        as_of: datetime,
    ) -> DebtToEquityValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        snapshot = self._snapshot_reader.latest_common_snapshot_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=("total_debt", "total_equity"),
            as_of=cutoff,
        )
        if snapshot is None:
            return None
        values = self._component_values(snapshot)
        debt, equity = values["total_debt"], values["total_equity"]
        valid = equity > 0
        return DebtToEquityValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            value=debt / equity if valid else None,
            unit="ratio",
            debt=debt,
            equity=equity,
            warnings=() if valid else ("non_positive_equity",),
            as_of=cutoff,
            available_at=snapshot.available_at,
            algorithm_version=DEBT_EQUITY_VERSION,
            snapshot=snapshot,
        )

    def interest_coverage_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> InterestCoverageValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        ebit = self._ttm_normalizer.ttm_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code="ebit",
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
        )
        finance_cost = self._ttm_normalizer.ttm_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code="finance_cost",
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=cutoff,
        )
        if not self._matching_ttms(
            ebit,
            finance_cost,
            provider_dataset_id,
            company_id,
            filing_scope,
            fiscal_year,
            fiscal_quarter,
        ):
            return None
        assert ebit is not None and finance_cost is not None
        valid = finance_cost.value > 0
        return InterestCoverageValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=ebit.value / finance_cost.value if valid else None,
            unit="ratio",
            ttm_ebit=ebit,
            ttm_finance_cost=finance_cost,
            warnings=() if valid else ("non_positive_finance_cost",),
            as_of=cutoff,
            available_at=max(ebit.available_at, finance_cost.available_at),
            algorithm_version=INTEREST_COVERAGE_VERSION,
        )

    def roe_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> ReturnOnEquityValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        pat = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "pat",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        if pat is None:
            return None
        boundaries = self._boundary_snapshots(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=("total_equity",),
            period_start=pat.period_start,
            period_end=pat.period_end,
            as_of=cutoff,
        )
        if boundaries is None:
            return None
        beginning, ending = boundaries
        beginning_equity = self._component_values(beginning)["total_equity"]
        ending_equity = self._component_values(ending)["total_equity"]
        valid = beginning_equity > 0 and ending_equity > 0
        average = (beginning_equity + ending_equity) / Decimal("2") if valid else None
        return ReturnOnEquityValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=pat.value / average if average is not None else None,
            unit="ratio",
            ttm_pat=pat,
            beginning_snapshot=beginning,
            ending_snapshot=ending,
            beginning_equity=beginning_equity,
            ending_equity=ending_equity,
            average_equity=average,
            warnings=() if valid else ("non_positive_equity",),
            as_of=cutoff,
            available_at=max(pat.available_at, beginning.available_at, ending.available_at),
            algorithm_version=ROE_VERSION,
        )

    def roce_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> ReturnOnCapitalEmployedValue | None:
        cutoff = self._knowledge_cutoff(as_of)
        ebit = self._ttm(
            provider_dataset_id,
            company_id,
            filing_scope,
            "ebit",
            fiscal_year,
            fiscal_quarter,
            cutoff,
        )
        if ebit is None:
            return None
        metric_codes = ("total_equity", "total_debt", "cash_and_equivalents")
        boundaries = self._boundary_snapshots(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=metric_codes,
            period_start=ebit.period_start,
            period_end=ebit.period_end,
            as_of=cutoff,
        )
        if boundaries is None:
            return None
        beginning, ending = boundaries
        beginning_capital = self._capital_employed(beginning)
        ending_capital = self._capital_employed(ending)
        valid = beginning_capital > 0 and ending_capital > 0
        average = (beginning_capital + ending_capital) / Decimal("2") if valid else None
        return ReturnOnCapitalEmployedValue(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            ending_fiscal_year=fiscal_year,
            ending_fiscal_quarter=fiscal_quarter,
            value=ebit.value / average if average is not None else None,
            unit="ratio",
            ttm_ebit=ebit,
            beginning_snapshot=beginning,
            ending_snapshot=ending,
            beginning_capital_employed=beginning_capital,
            ending_capital_employed=ending_capital,
            average_capital_employed=average,
            warnings=() if valid else ("non_positive_capital_employed",),
            as_of=cutoff,
            available_at=max(ebit.available_at, beginning.available_at, ending.available_at),
            algorithm_version=ROCE_VERSION,
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
    def _capital_employed(snapshot: InstantFinancialSnapshot) -> Decimal:
        values = CapitalEfficiencyFeatures._component_values(snapshot)
        return (
            values["total_equity"]
            + values["total_debt"]
            - values["cash_and_equivalents"]
        )

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
        ebit: TrailingTwelveMonthValue | None,
        finance_cost: TrailingTwelveMonthValue | None,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
    ) -> bool:
        if ebit is None or finance_cost is None:
            return False
        if not CapitalEfficiencyFeatures._valid_ttm_context(
            ebit,
            provider_dataset_id,
            company_id,
            filing_scope,
            "ebit",
            fiscal_year,
            fiscal_quarter,
        ) or not CapitalEfficiencyFeatures._valid_ttm_context(
            finance_cost,
            provider_dataset_id,
            company_id,
            filing_scope,
            "finance_cost",
            fiscal_year,
            fiscal_quarter,
        ):
            return False
        return (
            ebit.ending_fiscal_year == finance_cost.ending_fiscal_year
            and ebit.ending_fiscal_quarter == finance_cost.ending_fiscal_quarter
            and ebit.period_start == finance_cost.period_start
            and ebit.period_end == finance_cost.period_end
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
