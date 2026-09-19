"""PIT-safe deterministic valuation evidence without scoring or persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.financial_snapshots import (
    InstantFinancialSnapshot,
    InstantFinancialSnapshotReader,
)
from inflector_data.market_pit import PointInTimeMarketBar, PointInTimeMarketReader
from inflector_data.ttm import TrailingTwelveMonthNormalizer, TrailingTwelveMonthValue
from inflector_database.models import Security

MARKET_CAP_TO_TTM_PAT_VERSION = "market_cap_to_ttm_pat_v1"
MARKET_CAP_TO_TOTAL_EQUITY_VERSION = "market_cap_to_total_equity_v1"
MARKET_CAP_TO_TTM_REVENUE_VERSION = "market_cap_to_ttm_revenue_v1"
SIMPLIFIED_ENTERPRISE_VALUE_VERSION = "simplified_enterprise_value_v1"
SIMPLIFIED_EV_TO_TTM_EBITDA_VERSION = "simplified_ev_to_ttm_ebitda_v1"
SIMPLIFIED_EV_TO_TTM_REVENUE_VERSION = "simplified_ev_to_ttm_revenue_v1"
VALUATION_FEATURE_BUNDLE_VERSION = "valuation_feature_bundle_v1"


@dataclass(frozen=True, slots=True)
class SimplifiedEnterpriseValue:
    """Market capitalization plus the controlled debt-less-cash bridge only."""

    value: Decimal | None
    unit: str
    market_cap: Decimal | None
    total_debt: Decimal | None
    cash_and_equivalents: Decimal | None
    net_debt: Decimal | None
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str
    market_bar: PointInTimeMarketBar | None
    balance_sheet_snapshot: InstantFinancialSnapshot | None


type ValuationEvidence = (
    PointInTimeMarketBar
    | TrailingTwelveMonthValue
    | InstantFinancialSnapshot
    | SimplifiedEnterpriseValue
)


@dataclass(frozen=True, slots=True)
class ValuationMultipleValue:
    """One auditable valuation ratio and only the evidence it actually uses."""

    code: str
    value: Decimal | None
    unit: str
    numerator_value: Decimal | None
    numerator_unit: str | None
    denominator_value: Decimal | None
    denominator_unit: str | None
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str
    evidence: tuple[ValuationEvidence, ...]


@dataclass(frozen=True, slots=True)
class ValuationFeatureBundle:
    """Independent deterministic valuation primitives for one explicit context."""

    market_provider_dataset_id: UUID
    financial_provider_dataset_id: UUID
    company_id: UUID
    security_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    market_on_or_before: date | None
    as_of: datetime
    market_bar: PointInTimeMarketBar | None
    ttm_revenue: TrailingTwelveMonthValue | None
    ttm_pat: TrailingTwelveMonthValue | None
    ttm_ebitda: TrailingTwelveMonthValue | None
    equity_snapshot: InstantFinancialSnapshot | None
    net_debt_snapshot: InstantFinancialSnapshot | None
    simplified_enterprise_value: SimplifiedEnterpriseValue
    market_cap_to_ttm_pat: ValuationMultipleValue
    market_cap_to_total_equity: ValuationMultipleValue
    market_cap_to_ttm_revenue: ValuationMultipleValue
    simplified_ev_to_ttm_ebitda: ValuationMultipleValue
    simplified_ev_to_ttm_revenue: ValuationMultipleValue
    algorithm_version: str


class ValuationFeaturePrimitives:
    """Compose approved PIT readers into conservative valuation primitives."""

    def __init__(
        self,
        session: Session,
        market_reader: PointInTimeMarketReader,
        ttm_normalizer: TrailingTwelveMonthNormalizer,
        snapshot_reader: InstantFinancialSnapshotReader,
    ) -> None:
        self._session = session
        self._market_reader = market_reader
        self._ttm_normalizer = ttm_normalizer
        self._snapshot_reader = snapshot_reader

    def features_as_of(
        self,
        *,
        market_provider_dataset_id: UUID,
        financial_provider_dataset_id: UUID,
        security_id: UUID,
        company_id: UUID,
        filing_scope: str,
        ending_fiscal_year: int,
        ending_fiscal_quarter: int,
        interval: str,
        as_of: datetime,
        market_on_or_before: date | None = None,
    ) -> ValuationFeatureBundle:
        """Return all Phase 3H-A features for one declared PIT identity."""

        cutoff = self._knowledge_cutoff(as_of)
        self._validate_security_company(security_id, company_id)
        if not filing_scope:
            raise ValueError("filing_scope must not be empty")
        if ending_fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("ending_fiscal_quarter must be between 1 and 4")

        market_bar = self._market_reader.latest_market_bar_as_of(
            provider_dataset_id=market_provider_dataset_id,
            security_id=security_id,
            interval=interval,
            as_of=cutoff,
            on_or_before=market_on_or_before,
        )
        ttms = {
            metric: self._ttm_normalizer.ttm_as_of(
                provider_dataset_id=financial_provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                metric_code=metric,
                fiscal_year=ending_fiscal_year,
                fiscal_quarter=ending_fiscal_quarter,
                as_of=cutoff,
            )
            for metric in ("revenue", "pat", "ebitda_reported")
        }
        ttm_revenue = ttms["revenue"]
        ttm_pat = ttms["pat"]
        ttm_ebitda = ttms["ebitda_reported"]
        for metric, value in ttms.items():
            if value is not None:
                self._validate_ttm(
                    value,
                    metric_code=metric,
                    provider_dataset_id=financial_provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    fiscal_year=ending_fiscal_year,
                    fiscal_quarter=ending_fiscal_quarter,
                    as_of=cutoff,
                )
        period_ends = {value.period_end for value in ttms.values() if value is not None}
        if len(period_ends) > 1:
            raise ValueError("TTM values for one endpoint must share period_end")

        equity_snapshot = self._snapshot(
            financial_provider_dataset_id,
            company_id,
            filing_scope,
            ending_fiscal_year,
            ending_fiscal_quarter,
            ("total_equity",),
            cutoff,
        )
        net_debt_snapshot = self._snapshot(
            financial_provider_dataset_id,
            company_id,
            filing_scope,
            ending_fiscal_year,
            ending_fiscal_quarter,
            ("total_debt", "cash_and_equivalents"),
            cutoff,
        )

        simplified_ev = self._simplified_ev(market_bar, net_debt_snapshot, cutoff)
        market_cap = market_bar.market_cap if market_bar is not None else None
        pat_multiple = self._market_cap_multiple(
            "market_cap_to_ttm_pat",
            market_bar,
            ttm_pat,
            "missing_ttm_pat",
            "non_positive_ttm_pat",
            MARKET_CAP_TO_TTM_PAT_VERSION,
            cutoff,
        )
        revenue_multiple = self._market_cap_multiple(
            "market_cap_to_ttm_revenue",
            market_bar,
            ttm_revenue,
            "missing_ttm_revenue",
            "non_positive_ttm_revenue",
            MARKET_CAP_TO_TTM_REVENUE_VERSION,
            cutoff,
        )
        equity = self._snapshot_value(equity_snapshot, "total_equity")
        equity_multiple = self._ratio(
            code="market_cap_to_total_equity",
            numerator=market_cap,
            numerator_unit="INR" if market_bar is not None else None,
            denominator=equity,
            denominator_unit="INR" if equity_snapshot is not None else None,
            numerator_warning=self._market_cap_warning(market_bar),
            denominator_warning=(
                "missing_total_equity"
                if equity is None
                else "non_positive_total_equity"
                if equity <= 0
                else None
            ),
            evidence=self._evidence(market_bar, equity_snapshot),
            available_at=self._available_at(market_bar, equity_snapshot),
            algorithm_version=MARKET_CAP_TO_TOTAL_EQUITY_VERSION,
            as_of=cutoff,
        )
        ev_ebitda = self._ev_multiple(
            "simplified_ev_to_ttm_ebitda",
            simplified_ev,
            ttm_ebitda,
            "missing_ttm_ebitda",
            "non_positive_ttm_ebitda",
            SIMPLIFIED_EV_TO_TTM_EBITDA_VERSION,
            cutoff,
        )
        ev_revenue = self._ev_multiple(
            "simplified_ev_to_ttm_revenue",
            simplified_ev,
            ttm_revenue,
            "missing_ttm_revenue",
            "non_positive_ttm_revenue",
            SIMPLIFIED_EV_TO_TTM_REVENUE_VERSION,
            cutoff,
        )
        self._validate_feature_period_end(net_debt_snapshot, ttm_ebitda)
        self._validate_feature_period_end(net_debt_snapshot, ttm_revenue)
        return ValuationFeatureBundle(
            market_provider_dataset_id=market_provider_dataset_id,
            financial_provider_dataset_id=financial_provider_dataset_id,
            company_id=company_id,
            security_id=security_id,
            filing_scope=filing_scope,
            ending_fiscal_year=ending_fiscal_year,
            ending_fiscal_quarter=ending_fiscal_quarter,
            market_on_or_before=market_on_or_before,
            as_of=cutoff,
            market_bar=market_bar,
            ttm_revenue=ttm_revenue,
            ttm_pat=ttm_pat,
            ttm_ebitda=ttm_ebitda,
            equity_snapshot=equity_snapshot,
            net_debt_snapshot=net_debt_snapshot,
            simplified_enterprise_value=simplified_ev,
            market_cap_to_ttm_pat=pat_multiple,
            market_cap_to_total_equity=equity_multiple,
            market_cap_to_ttm_revenue=revenue_multiple,
            simplified_ev_to_ttm_ebitda=ev_ebitda,
            simplified_ev_to_ttm_revenue=ev_revenue,
            algorithm_version=VALUATION_FEATURE_BUNDLE_VERSION,
        )

    def _snapshot(
        self,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        metrics: tuple[str, ...],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        snapshot = self._snapshot_reader.snapshot_for_fiscal_endpoint_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            metric_codes=metrics,
            as_of=as_of,
        )
        if snapshot is not None:
            self._validate_snapshot(
                snapshot,
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                as_of=as_of,
            )
        return snapshot

    def _validate_security_company(self, security_id: UUID, company_id: UUID) -> None:
        security = self._session.get(Security, security_id)
        if security is None:
            raise ValueError("unknown security_id")
        if security.company_id != company_id:
            raise ValueError("security_id does not belong to company_id")

    @staticmethod
    def _validate_ttm(
        value: TrailingTwelveMonthValue,
        *,
        metric_code: str,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> None:
        if (
            value.metric_code != metric_code
            or value.provider_dataset_id != provider_dataset_id
            or value.company_id != company_id
            or value.filing_scope != filing_scope
            or value.ending_fiscal_year != fiscal_year
            or value.ending_fiscal_quarter != fiscal_quarter
            or value.as_of != as_of
        ):
            raise ValueError("TTM evidence does not match the requested valuation context")
        if value.unit != "INR" or not isinstance(value.value, Decimal):
            raise ValueError("TTM evidence must be an exact INR Decimal")

    @staticmethod
    def _validate_snapshot(
        snapshot: InstantFinancialSnapshot,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> None:
        if (
            snapshot.provider_dataset_id != provider_dataset_id
            or snapshot.company_id != company_id
            or snapshot.filing_scope != filing_scope
            or snapshot.fiscal_period.fiscal_year != fiscal_year
            or snapshot.fiscal_period.fiscal_quarter != fiscal_quarter
            or snapshot.as_of != as_of
        ):
            raise ValueError("instant snapshot does not match the requested valuation context")
        if any(
            component.unit != "INR" or not isinstance(component.value, Decimal)
            for component in snapshot.components
        ):
            raise ValueError("instant snapshot evidence must be exact INR Decimals")

    @staticmethod
    def _validate_feature_period_end(
        snapshot: InstantFinancialSnapshot | None,
        ttm: TrailingTwelveMonthValue | None,
    ) -> None:
        if (
            snapshot is not None
            and ttm is not None
            and snapshot.fiscal_period.period_end != ttm.period_end
        ):
            raise ValueError("snapshot and TTM evidence must share one economic period_end")

    @staticmethod
    def _snapshot_value(snapshot: InstantFinancialSnapshot | None, metric: str) -> Decimal | None:
        if snapshot is None:
            return None
        component = next((item for item in snapshot.components if item.metric_code == metric), None)
        if component is None:
            raise ValueError(f"snapshot is missing required component {metric}")
        if component.unit != "INR" or not isinstance(component.value, Decimal):
            raise ValueError("instant snapshot evidence must be an exact INR Decimal")
        return component.value

    @classmethod
    def _simplified_ev(
        cls,
        market_bar: PointInTimeMarketBar | None,
        snapshot: InstantFinancialSnapshot | None,
        as_of: datetime,
    ) -> SimplifiedEnterpriseValue:
        market_cap = market_bar.market_cap if market_bar is not None else None
        debt = cls._snapshot_value(snapshot, "total_debt")
        cash = cls._snapshot_value(snapshot, "cash_and_equivalents")
        warnings = list(cls._warning_tuple(cls._market_cap_warning(market_bar)))
        if snapshot is None:
            warnings.append("missing_net_debt_snapshot")
        net_debt = debt - cash if debt is not None and cash is not None else None
        value = (
            market_cap + net_debt
            if not warnings and market_cap is not None and net_debt is not None
            else None
        )
        return SimplifiedEnterpriseValue(
            value=value,
            unit="INR",
            market_cap=market_cap,
            total_debt=debt,
            cash_and_equivalents=cash,
            net_debt=net_debt,
            warnings=tuple(warnings),
            as_of=as_of,
            available_at=cls._available_at(market_bar, snapshot),
            algorithm_version=SIMPLIFIED_ENTERPRISE_VALUE_VERSION,
            market_bar=market_bar,
            balance_sheet_snapshot=snapshot,
        )

    @classmethod
    def _market_cap_multiple(
        cls,
        code: str,
        market_bar: PointInTimeMarketBar | None,
        ttm: TrailingTwelveMonthValue | None,
        missing_warning: str,
        non_positive_warning: str,
        version: str,
        as_of: datetime,
    ) -> ValuationMultipleValue:
        denominator = ttm.value if ttm is not None else None
        return cls._ratio(
            code=code,
            numerator=market_bar.market_cap if market_bar is not None else None,
            numerator_unit="INR" if market_bar is not None else None,
            denominator=denominator,
            denominator_unit=ttm.unit if ttm is not None else None,
            numerator_warning=cls._market_cap_warning(market_bar),
            denominator_warning=(
                missing_warning
                if denominator is None
                else non_positive_warning
                if denominator <= 0
                else None
            ),
            evidence=cls._evidence(market_bar, ttm),
            available_at=cls._available_at(market_bar, ttm),
            algorithm_version=version,
            as_of=as_of,
        )

    @classmethod
    def _ev_multiple(
        cls,
        code: str,
        simplified_ev: SimplifiedEnterpriseValue,
        ttm: TrailingTwelveMonthValue | None,
        missing_warning: str,
        non_positive_warning: str,
        version: str,
        as_of: datetime,
    ) -> ValuationMultipleValue:
        denominator = ttm.value if ttm is not None else None
        numerator_warning = (
            "non_positive_simplified_enterprise_value"
            if simplified_ev.value is not None and simplified_ev.value <= 0
            else simplified_ev.warnings[0]
            if simplified_ev.warnings
            else None
        )
        return cls._ratio(
            code=code,
            numerator=simplified_ev.value,
            numerator_unit="INR",
            denominator=denominator,
            denominator_unit=ttm.unit if ttm is not None else None,
            numerator_warning=numerator_warning,
            denominator_warning=(
                missing_warning
                if denominator is None
                else non_positive_warning
                if denominator <= 0
                else None
            ),
            evidence=cls._evidence(simplified_ev, ttm),
            available_at=cls._available_at(simplified_ev, ttm),
            algorithm_version=version,
            as_of=as_of,
        )

    @staticmethod
    def _ratio(
        *,
        code: str,
        numerator: Decimal | None,
        numerator_unit: str | None,
        denominator: Decimal | None,
        denominator_unit: str | None,
        numerator_warning: str | None,
        denominator_warning: str | None,
        evidence: tuple[ValuationEvidence, ...],
        available_at: datetime | None,
        algorithm_version: str,
        as_of: datetime,
    ) -> ValuationMultipleValue:
        warnings = tuple(
            warning for warning in (numerator_warning, denominator_warning) if warning is not None
        )
        value = (
            numerator / denominator
            if not warnings and numerator is not None and denominator is not None
            else None
        )
        return ValuationMultipleValue(
            code=code,
            value=value,
            unit="ratio",
            numerator_value=numerator,
            numerator_unit=numerator_unit,
            denominator_value=denominator,
            denominator_unit=denominator_unit,
            warnings=warnings,
            as_of=as_of,
            available_at=available_at,
            algorithm_version=algorithm_version,
            evidence=evidence,
        )

    @staticmethod
    def _market_cap_warning(bar: PointInTimeMarketBar | None) -> str | None:
        if bar is None or bar.market_cap is None:
            return "missing_market_cap"
        if bar.market_cap <= 0:
            return "non_positive_market_cap"
        return None

    @staticmethod
    def _warning_tuple(warning: str | None) -> tuple[str, ...]:
        return () if warning is None else (warning,)

    @staticmethod
    def _evidence(*values: ValuationEvidence | None) -> tuple[ValuationEvidence, ...]:
        return tuple(value for value in values if value is not None)

    @staticmethod
    def _available_at(*values: ValuationEvidence | None) -> datetime | None:
        timestamps = [
            value.available_at for value in values if value is not None and value.available_at
        ]
        return max(timestamps) if timestamps else None

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
