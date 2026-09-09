"""Deterministic corporate-action validation; no adjustment math."""

from __future__ import annotations

from inflector_core.providers import CorporateActionRecord
from inflector_data.validation import ValidationIssue

ACTION_TYPES = {
    "split",
    "bonus",
    "cash_dividend",
    "rights",
    "symbol_change",
    "security_replacement",
}


def validate_corporate_action(record: CorporateActionRecord) -> list[ValidationIssue]:
    issues = [ValidationIssue(code, code.replace("_", " ")) for code in record.parse_errors]
    if not record.security_isin:
        issues.append(ValidationIssue("missing_security_identity", "security ISIN is required"))
    if record.action_type not in ACTION_TYPES:
        issues.append(ValidationIssue("invalid_action_type", "unsupported corporate action type"))
    if record.action_type in {"split", "bonus", "rights"}:
        if record.ratio_numerator is None or record.ratio_denominator is None:
            issues.append(ValidationIssue("missing_ratio", "action ratio is required"))
        elif record.ratio_numerator <= 0 or record.ratio_denominator <= 0:
            issues.append(ValidationIssue("invalid_ratio", "ratio components must be positive"))
    if (
        record.action_type in {"split", "bonus", "rights", "symbol_change", "security_replacement"}
        and not record.effective_date
    ):
        issues.append(ValidationIssue("missing_effective_date", "effective date is required"))
    if record.action_type == "cash_dividend":
        if record.cash_amount is None or record.cash_amount <= 0:
            issues.append(
                ValidationIssue("invalid_dividend_amount", "cash dividend must be positive")
            )
        if record.cash_currency != "INR" or record.cash_unit != "INR/share":
            issues.append(
                ValidationIssue("invalid_dividend_unit", "only INR/share dividends are supported")
            )
        if not record.ex_date and not record.effective_date:
            issues.append(
                ValidationIssue(
                    "missing_dividend_anchor",
                    "cash dividend requires an ex date or effective date",
                )
            )
    if record.action_type == "rights":
        if record.subscription_price is None or record.subscription_price <= 0:
            issues.append(
                ValidationIssue(
                    "invalid_subscription_price", "rights subscription price must be positive"
                )
            )
        if record.subscription_currency != "INR":
            issues.append(
                ValidationIssue("invalid_subscription_currency", "only INR rights are supported")
            )
    if record.ex_date and record.effective_date and record.effective_date < record.ex_date:
        issues.append(
            ValidationIssue("invalid_action_date_order", "effective date cannot precede ex date")
        )
    if record.action_type == "symbol_change" and (not record.exchange or not record.new_symbol):
        issues.append(
            ValidationIssue(
                "missing_symbol_change_terms", "symbol change requires exchange and new symbol"
            )
        )
    if record.action_type == "security_replacement" and not record.successor_isin:
        issues.append(
            ValidationIssue(
                "missing_successor_isin", "security replacement requires successor ISIN"
            )
        )
    return issues
