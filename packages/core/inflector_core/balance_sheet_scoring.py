"""Pure Phase 4D-C Balance Sheet component scoring primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from inflector_core.component_scoring import (
    PIECEWISE_LINEAR_CURVE_VERSION,
    score_piecewise_linear,
)
from inflector_core.scoring_policy import (
    BalanceSheetScoringPolicy,
    FinancialContextSelection,
    InflectionScoringPolicy,
    PiecewiseLinearScoringCurve,
)

if TYPE_CHECKING:
    from inflector_data.capital_features import (
        DebtToEquityValue,
        InterestCoverageValue,
        NetDebtValue,
    )
    from inflector_data.ttm import TrailingTwelveMonthValue

BALANCE_SHEET_COMPONENT_VERSION = "balance_sheet_component_v1"

BALANCE_SHEET_SUBFACTOR_ORDER = (
    "net_debt_to_ebitda",
    "debt_to_equity",
    "interest_coverage",
)


@dataclass(frozen=True, slots=True)
class BalanceSheetEvidence:
    company_id: UUID
    context: FinancialContextSelection
    fiscal_year: int
    fiscal_quarter: int
    as_of: datetime
    net_debt: NetDebtValue | None = None
    ttm_ebitda: TrailingTwelveMonthValue | None = None
    debt_to_equity: DebtToEquityValue | None = None
    interest_coverage: InterestCoverageValue | None = None


@dataclass(frozen=True, slots=True)
class NetDebtToEbitdaEvidence:
    net_debt: NetDebtValue
    ttm_ebitda: TrailingTwelveMonthValue


@dataclass(frozen=True, slots=True)
class BalanceSheetSubfactorScore:
    code: str
    raw_value: Decimal
    raw_unit: str
    normalized_raw_value: Decimal | None
    normalized_raw_unit: str | None
    scoring_value: Decimal
    scoring_unit: str
    transform_code: str
    normalized_score: Decimal
    configured_weight: Decimal
    effective_weight: Decimal
    contribution: Decimal
    input_available_at: datetime
    evidence_type: str
    evidence: object
    curve_algorithm_version: str


@dataclass(frozen=True, slots=True)
class BalanceSheetComponentScore:
    company_id: UUID
    provider_dataset_id: UUID
    filing_scope: str
    ending_fiscal_year: int
    ending_fiscal_quarter: int
    score: Decimal | None
    unit: str
    weight_coverage: Decimal
    available_weight: Decimal
    subfactors: tuple[BalanceSheetSubfactorScore, ...]
    missing_subfactors: tuple[str, ...]
    warnings: tuple[str, ...]
    as_of: datetime
    available_at: datetime | None
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class _RawSubfactor:
    code: str
    raw_value: Decimal
    raw_unit: str
    normalized_raw_value: Decimal | None
    normalized_raw_unit: str | None
    scoring_value: Decimal
    scoring_unit: str
    transform_code: str
    weight: Decimal
    available_at: datetime
    evidence: object
    curve: PiecewiseLinearScoringCurve


class BalanceSheetComponentScorer:
    """Score coherent leverage and coverage evidence through explicit transforms."""

    def score(
        self,
        *,
        evidence: BalanceSheetEvidence,
        policy: InflectionScoringPolicy,
    ) -> BalanceSheetComponentScore:
        cutoff = self._aware_utc(evidence.as_of, "as_of")
        scoring = policy.balance_sheet
        if scoring is None:
            raise ValueError("balance-sheet scoring policy is not configured")

        self._validate_evidence(evidence, cutoff)
        raw_by_code = self._extract_raw_subfactors(evidence, scoring)
        weights = scoring.subfactor_weights
        missing = tuple(
            code
            for code in BALANCE_SHEET_SUBFACTOR_ORDER
            if getattr(weights, code) > 0 and code not in raw_by_code
        )
        available_weight = sum(
            (
                raw_by_code[code].weight
                for code in BALANCE_SHEET_SUBFACTOR_ORDER
                if code in raw_by_code
            ),
            Decimal("0"),
        )
        sufficient = available_weight >= scoring.minimum_weight_coverage
        subfactors = tuple(
            self._score_subfactor(raw_by_code[code], available_weight, sufficient)
            for code in BALANCE_SHEET_SUBFACTOR_ORDER
            if code in raw_by_code
        )
        component_score = (
            sum((item.contribution for item in subfactors), Decimal("0")) if sufficient else None
        )
        if component_score is not None and not Decimal("0") <= component_score <= Decimal("100"):
            raise AssertionError("component score escaped the validated 0-100 range")

        return BalanceSheetComponentScore(
            company_id=evidence.company_id,
            provider_dataset_id=evidence.context.provider_dataset_id,
            filing_scope=evidence.context.filing_scope,
            ending_fiscal_year=evidence.fiscal_year,
            ending_fiscal_quarter=evidence.fiscal_quarter,
            score=component_score,
            unit="score_0_100",
            weight_coverage=available_weight,
            available_weight=available_weight,
            subfactors=subfactors,
            missing_subfactors=missing,
            warnings=() if sufficient else ("insufficient_subfactor_coverage",),
            as_of=cutoff,
            available_at=max(
                (item.input_available_at for item in subfactors),
                default=None,
            ),
            algorithm_version=BALANCE_SHEET_COMPONENT_VERSION,
        )

    @staticmethod
    def _score_subfactor(
        raw: _RawSubfactor,
        available_weight: Decimal,
        sufficient: bool,
    ) -> BalanceSheetSubfactorScore:
        normalized = score_piecewise_linear(raw.scoring_value, raw.curve)
        effective = raw.weight / available_weight if sufficient else Decimal("0")
        return BalanceSheetSubfactorScore(
            code=raw.code,
            raw_value=raw.raw_value,
            raw_unit=raw.raw_unit,
            normalized_raw_value=raw.normalized_raw_value,
            normalized_raw_unit=raw.normalized_raw_unit,
            scoring_value=raw.scoring_value,
            scoring_unit=raw.scoring_unit,
            transform_code=raw.transform_code,
            normalized_score=normalized,
            configured_weight=raw.weight,
            effective_weight=effective,
            contribution=normalized * effective,
            input_available_at=raw.available_at,
            evidence_type=type(raw.evidence).__name__,
            evidence=raw.evidence,
            curve_algorithm_version=PIECEWISE_LINEAR_CURVE_VERSION,
        )

    def _validate_evidence(self, bundle: BalanceSheetEvidence, cutoff: datetime) -> None:
        items = (
            bundle.net_debt,
            bundle.ttm_ebitda,
            bundle.debt_to_equity,
            bundle.interest_coverage,
        )
        for item in items:
            if item is None:
                continue
            if item.provider_dataset_id != bundle.context.provider_dataset_id:
                raise ValueError("evidence provider does not match selected context")
            if item.filing_scope != bundle.context.filing_scope:
                raise ValueError("evidence filing scope does not match selected context")
            if item.company_id != bundle.company_id:
                raise ValueError("evidence company does not match scoring company")
            if self._aware_utc(item.as_of, "evidence as_of") != cutoff:
                raise ValueError("evidence as_of does not match scoring cutoff")

        self._validate_ttm_ebitda(bundle)
        self._validate_endpoints(bundle)
        self._validate_period_ends(bundle)
        self._validate_stock_period_identity(bundle)
        self._validate_debt_to_equity(bundle.debt_to_equity)

    @staticmethod
    def _validate_ttm_ebitda(bundle: BalanceSheetEvidence) -> None:
        ttm = bundle.ttm_ebitda
        if ttm is None:
            return
        if ttm.metric_code != "ebitda_reported":
            raise ValueError("TTM EBITDA metric_code must be ebitda_reported")
        if ttm.unit != "INR":
            raise ValueError("TTM EBITDA unit must be INR")
        if not isinstance(ttm.value, Decimal):
            raise ValueError("TTM EBITDA value must be Decimal")

    @staticmethod
    def _validate_endpoints(bundle: BalanceSheetEvidence) -> None:
        requested = (bundle.fiscal_year, bundle.fiscal_quarter)
        for stock in (bundle.net_debt, bundle.debt_to_equity):
            if stock is None:
                continue
            period = stock.snapshot.fiscal_period
            if (period.fiscal_year, period.fiscal_quarter) != requested:
                raise ValueError("stock evidence endpoint does not match requested endpoint")
        for flow in (bundle.ttm_ebitda, bundle.interest_coverage):
            if (
                flow is not None
                and (
                    flow.ending_fiscal_year,
                    flow.ending_fiscal_quarter,
                )
                != requested
            ):
                raise ValueError("flow evidence endpoint does not match requested endpoint")

    @staticmethod
    def _validate_period_ends(bundle: BalanceSheetEvidence) -> None:
        period_ends: list[date] = []
        if bundle.net_debt is not None:
            period_ends.append(bundle.net_debt.snapshot.fiscal_period.period_end)
        if bundle.debt_to_equity is not None:
            period_ends.append(bundle.debt_to_equity.snapshot.fiscal_period.period_end)
        if bundle.ttm_ebitda is not None:
            period_ends.append(bundle.ttm_ebitda.period_end)
        if bundle.interest_coverage is not None:
            period_ends.append(bundle.interest_coverage.ttm_ebit.period_end)
        if len(set(period_ends)) > 1:
            raise ValueError("evidence period_end values do not match")

    @staticmethod
    def _validate_stock_period_identity(bundle: BalanceSheetEvidence) -> None:
        if bundle.net_debt is None or bundle.debt_to_equity is None:
            return
        if (
            bundle.net_debt.snapshot.fiscal_period.id
            != bundle.debt_to_equity.snapshot.fiscal_period.id
        ):
            raise ValueError("stock evidence must share one fiscal period id")

    @staticmethod
    def _validate_debt_to_equity(item: DebtToEquityValue | None) -> None:
        if item is None or item.value is None:
            return
        if item.debt < 0 or item.equity <= 0 or item.value < 0:
            raise ValueError("defined debt/equity evidence is structurally inconsistent")

    def _extract_raw_subfactors(
        self,
        bundle: BalanceSheetEvidence,
        scoring: BalanceSheetScoringPolicy,
    ) -> dict[str, _RawSubfactor]:
        weights = scoring.subfactor_weights
        raw: dict[str, _RawSubfactor] = {}

        def add(
            *,
            code: str,
            raw_value: Decimal | None,
            raw_unit: str,
            normalized_raw_value: Decimal | None,
            normalized_raw_unit: str | None,
            scoring_value: Decimal | None,
            transform_code: str,
            available_at: datetime,
            source: object,
            curve: PiecewiseLinearScoringCurve,
        ) -> None:
            weight = getattr(weights, code)
            if weight == 0 or raw_value is None or scoring_value is None:
                return
            raw[code] = _RawSubfactor(
                code=code,
                raw_value=raw_value,
                raw_unit=raw_unit,
                normalized_raw_value=normalized_raw_value,
                normalized_raw_unit=normalized_raw_unit,
                scoring_value=scoring_value,
                scoring_unit="ratio",
                transform_code=transform_code,
                weight=weight,
                available_at=self._aware_utc(available_at, f"{code} available_at"),
                evidence=source,
                curve=curve,
            )

        if bundle.net_debt is not None and bundle.ttm_ebitda is not None:
            ebitda = bundle.ttm_ebitda.value
            ratio = bundle.net_debt.value / ebitda if ebitda > 0 else None
            evidence = NetDebtToEbitdaEvidence(bundle.net_debt, bundle.ttm_ebitda)
            add(
                code="net_debt_to_ebitda",
                raw_value=bundle.net_debt.value if ratio is not None else None,
                raw_unit="INR",
                normalized_raw_value=ratio,
                normalized_raw_unit="ratio" if ratio is not None else None,
                scoring_value=-ratio if ratio is not None else None,
                transform_code="negate_net_debt_over_ttm_ebitda",
                available_at=max(
                    bundle.net_debt.available_at,
                    bundle.ttm_ebitda.available_at,
                ),
                source=evidence,
                curve=scoring.net_debt_to_ebitda_signal_curve,
            )
        if bundle.debt_to_equity is not None:
            value = bundle.debt_to_equity.value
            add(
                code="debt_to_equity",
                raw_value=value,
                raw_unit="ratio",
                normalized_raw_value=None,
                normalized_raw_unit=None,
                scoring_value=-value if value is not None else None,
                transform_code="negate_debt_to_equity",
                available_at=bundle.debt_to_equity.available_at,
                source=bundle.debt_to_equity,
                curve=scoring.debt_to_equity_signal_curve,
            )
        if bundle.interest_coverage is not None:
            value = bundle.interest_coverage.value
            add(
                code="interest_coverage",
                raw_value=value,
                raw_unit="ratio",
                normalized_raw_value=None,
                normalized_raw_unit=None,
                scoring_value=value,
                transform_code="identity",
                available_at=bundle.interest_coverage.available_at,
                source=bundle.interest_coverage,
                curve=scoring.interest_coverage_curve,
            )
        return raw

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)


def score_balance_sheet_component(
    *,
    evidence: BalanceSheetEvidence,
    policy: InflectionScoringPolicy,
) -> BalanceSheetComponentScore:
    return BalanceSheetComponentScorer().score(evidence=evidence, policy=policy)
