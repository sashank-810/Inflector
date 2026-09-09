"""Deterministic ingestion validation rules without persistence concerns."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from inflector_core.providers import MarketBarRecord, UniverseRecord


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A deterministic, explainable validation failure."""

    rule_code: str
    message: str
    severity: str = "error"


def validate_universe(record: UniverseRecord) -> list[ValidationIssue]:
    """Validate only identity fields necessary for Phase 2A normalization."""

    issues = [ValidationIssue(error, error.replace("_", " ")) for error in record.parse_errors]
    required = {
        "missing_legal_name": (record.legal_name, "legal_name is required"),
        "missing_display_name": (record.display_name, "display_name is required"),
        "missing_isin": (record.isin, "isin is required"),
        "missing_exchange": (record.exchange, "exchange is required"),
        "missing_symbol": (record.symbol, "symbol is required"),
        "missing_valid_from": (record.valid_from, "valid_from is required"),
    }
    issues.extend(
        ValidationIssue(code, message) for code, (value, message) in required.items() if not value
    )
    if (
        record.valid_from is not None
        and record.valid_to is not None
        and record.valid_to < record.valid_from
    ):
        issues.append(
            ValidationIssue("valid_to_before_valid_from", "valid_to cannot be before valid_from")
        )
    return issues


def validate_price(record: MarketBarRecord) -> list[ValidationIssue]:
    """Validate a daily OHLCV observation before it can become a price fact."""

    issues = [ValidationIssue(error, error.replace("_", " ")) for error in record.parse_errors]
    required = {
        "missing_security_identity": record.security_isin,
        "invalid_date": record.trading_date,
        "missing_interval": record.interval,
        "missing_open": record.open_price,
        "missing_high": record.high_price,
        "missing_low": record.low_price,
        "missing_close": record.close_price,
        "missing_volume": record.volume,
    }
    issues.extend(
        ValidationIssue(code, code.replace("_", " "))
        for code, value in required.items()
        if value is None
    )
    if record.interval is not None and record.interval != "1d":
        issues.append(ValidationIssue("unsupported_interval", "only daily (1d) bars are supported"))
    prices = (record.open_price, record.high_price, record.low_price, record.close_price)
    if any(value is not None and value < Decimal("0") for value in prices):
        issues.append(ValidationIssue("negative_price", "prices cannot be negative"))
    if record.volume is not None and record.volume < 0:
        issues.append(ValidationIssue("negative_volume", "volume cannot be negative"))
    if (
        record.high_price is not None
        and record.low_price is not None
        and record.high_price < record.low_price
    ):
        issues.append(
            ValidationIssue("high_less_than_low", "high price cannot be lower than low price")
        )
    if record.low_price is not None and record.high_price is not None:
        for field_name, value in (("open", record.open_price), ("close", record.close_price)):
            if value is not None and not record.low_price <= value <= record.high_price:
                issues.append(
                    ValidationIssue(
                        f"{field_name}_outside_range",
                        f"{field_name} price must be between low and high",
                    )
                )
    return issues
