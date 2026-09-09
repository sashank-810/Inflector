"""Deterministic reported-unit normalization and financial validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from inflector_core.providers import FinancialRecord
from inflector_data.validation import ValidationIssue

INR_SCALES = {
    "ones": Decimal("1"),
    "thousand": Decimal("1000"),
    "lakh": Decimal("100000"),
    "million": Decimal("1000000"),
    "crore": Decimal("10000000"),
}
METRICS = (
    ("revenue", "income", "monetary", "duration"),
    ("operating_revenue", "income", "monetary", "duration"),
    ("other_income", "income", "monetary", "duration"),
    ("operating_profit", "income", "monetary", "duration"),
    ("ebitda_reported", "income", "monetary", "duration"),
    ("ebit", "income", "monetary", "duration"),
    ("finance_cost", "income", "monetary", "duration"),
    ("exceptional_items", "income", "monetary", "duration"),
    ("profit_before_tax", "income", "monetary", "duration"),
    ("tax_expense", "income", "monetary", "duration"),
    ("pat", "income", "monetary", "duration"),
    ("eps_basic", "income", "per_share", "duration"),
    ("eps_diluted", "income", "per_share", "duration"),
    ("total_assets", "balance_sheet", "monetary", "instant"),
    ("total_liabilities", "balance_sheet", "monetary", "instant"),
    ("total_equity", "balance_sheet", "monetary", "instant"),
    ("total_debt", "balance_sheet", "monetary", "instant"),
    ("cash_and_equivalents", "balance_sheet", "monetary", "instant"),
    ("inventory", "balance_sheet", "monetary", "instant"),
    ("trade_receivables", "balance_sheet", "monetary", "instant"),
    ("trade_payables", "balance_sheet", "monetary", "instant"),
    ("cash_flow_from_operations", "cash_flow", "monetary", "duration"),
    ("cash_flow_from_investing", "cash_flow", "monetary", "duration"),
    ("cash_flow_from_financing", "cash_flow", "monetary", "duration"),
    ("capex_reported", "cash_flow", "monetary", "duration"),
)


@dataclass(frozen=True, slots=True)
class NormalizedValue:
    value: Decimal | None
    unit: str | None


def validate_financial(record: FinancialRecord) -> list[ValidationIssue]:
    """Validate record fields that do not need database lookups."""

    issues = [ValidationIssue(code, code.replace("_", " ")) for code in record.parse_errors]
    required = {
        "missing_company_identity": record.company_legal_name,
        "missing_filing_identity": record.filing_external_id,
        "missing_filing_type": record.filing_type,
        "missing_filing_scope": record.filing_scope,
        "missing_period_kind": record.period_kind,
        "missing_period_start": record.period_start,
        "missing_period_end": record.period_end,
        "missing_fiscal_year": record.fiscal_year,
        "missing_metric_code": record.metric_code,
        "missing_reported_value": record.reported_value,
        "missing_reported_unit": record.reported_unit,
        "missing_reported_scale": record.reported_scale,
    }
    issues.extend(
        ValidationIssue(code, code.replace("_", " "))
        for code, value in required.items()
        if value is None
    )
    if record.filing_scope is not None and record.filing_scope not in {
        "standalone",
        "consolidated",
    }:
        issues.append(
            ValidationIssue(
                "invalid_filing_scope", "filing scope must be standalone or consolidated"
            )
        )
    if record.period_kind is not None and record.period_kind not in {
        "quarter",
        "half_year",
        "nine_month",
        "annual",
    }:
        issues.append(
            ValidationIssue("invalid_period_kind", "unsupported reported fiscal period kind")
        )
    if (
        record.period_start is not None
        and record.period_end is not None
        and record.period_end < record.period_start
    ):
        issues.append(
            ValidationIssue("period_end_before_start", "period end cannot precede period start")
        )
    if record.fiscal_quarter is not None and record.fiscal_quarter not in {1, 2, 3, 4}:
        issues.append(
            ValidationIssue("invalid_fiscal_quarter", "fiscal quarter must be 1 through 4")
        )
    if record.reported_unit == "INR" and record.reported_scale not in INR_SCALES:
        issues.append(ValidationIssue("invalid_scale", "INR monetary scale is unsupported"))
    if record.reported_unit not in {"INR", "INR/share", "shares", "percentage", "ratio"}:
        issues.append(ValidationIssue("invalid_unit", "reported unit is unsupported"))
    if record.reported_unit == "INR" and record.reported_currency not in {None, "INR"}:
        issues.append(
            ValidationIssue("unsupported_currency", "foreign currency conversion is not supported")
        )
    return issues


def normalize_financial(record: FinancialRecord) -> NormalizedValue:
    """Normalize only INR monetary amounts; all other supported units stay explicit."""

    assert record.reported_value is not None and record.reported_unit is not None
    if record.reported_unit == "INR":
        assert record.reported_scale is not None
        return NormalizedValue(record.reported_value * INR_SCALES[record.reported_scale], "INR")
    return NormalizedValue(None, None)
