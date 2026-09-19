"""Deterministic ingestion validation rules without persistence concerns."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from re import fullmatch

from inflector_core.providers import BenchmarkBarRecord, MarketBarRecord, UniverseRecord


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
    if record.market_cap is not None:
        if not isinstance(record.market_cap, Decimal):
            issues.append(
                ValidationIssue("invalid_market_cap_type", "market_cap must be a Decimal")
            )
        elif record.market_cap < Decimal("0"):
            issues.append(ValidationIssue("negative_market_cap", "market_cap cannot be negative"))
    if record.delivery_quantity is not None:
        if isinstance(record.delivery_quantity, bool) or not isinstance(
            record.delivery_quantity, int
        ):
            issues.append(
                ValidationIssue(
                    "invalid_delivery_quantity_type", "delivery_quantity must be an integer"
                )
            )
        elif record.delivery_quantity < 0:
            issues.append(
                ValidationIssue(
                    "negative_delivery_quantity", "delivery_quantity cannot be negative"
                )
            )
    if record.delivery_percentage is not None:
        if not isinstance(record.delivery_percentage, Decimal):
            issues.append(
                ValidationIssue(
                    "invalid_delivery_percentage_type",
                    "delivery_percentage must be a Decimal fraction",
                )
            )
        elif not Decimal("0") <= record.delivery_percentage <= Decimal("1"):
            issues.append(
                ValidationIssue(
                    "invalid_delivery_percentage",
                    "delivery_percentage must be a fraction between 0 and 1",
                )
            )
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


def validate_benchmark(record: BenchmarkBarRecord) -> list[ValidationIssue]:
    """Validate one provider-neutral daily benchmark observation."""

    issues = [ValidationIssue(error, error.replace("_", " ")) for error in record.parse_errors]
    required = {
        "missing_benchmark_code": record.benchmark_code,
        "missing_benchmark_display_name": record.benchmark_display_name,
        "missing_currency": record.currency,
        "invalid_date": record.trading_date,
        "missing_interval": record.interval,
        "missing_open": record.open_value,
        "missing_high": record.high_value,
        "missing_low": record.low_value,
        "missing_close": record.close_value,
    }
    issues.extend(
        ValidationIssue(code, code.replace("_", " "))
        for code, value in required.items()
        if value is None or (isinstance(value, str) and not value.strip())
    )
    if record.currency is not None and fullmatch(r"[A-Z]{3}", record.currency) is None:
        issues.append(
            ValidationIssue("invalid_currency", "currency must be a three-letter uppercase code")
        )
    if record.interval is not None and record.interval != "1d":
        issues.append(ValidationIssue("unsupported_interval", "only daily (1d) bars are supported"))
    named_values = (
        ("open", record.open_value),
        ("high", record.high_value),
        ("low", record.low_value),
        ("close", record.close_value),
    )
    issues.extend(
        ValidationIssue(
            f"invalid_{field_name}_type", f"{field_name} value must be a Decimal"
        )
        for field_name, value in named_values
        if value is not None and not isinstance(value, Decimal)
    )
    values = tuple(value for _, value in named_values if isinstance(value, Decimal))
    if any(value < Decimal("0") for value in values):
        issues.append(ValidationIssue("negative_benchmark_value", "OHLC values cannot be negative"))
    if isinstance(record.high_value, Decimal) and isinstance(record.low_value, Decimal):
        if record.high_value < record.low_value:
            issues.append(
                ValidationIssue("high_less_than_low", "high value cannot be lower than low value")
            )
        for field_name, value in (("open", record.open_value), ("close", record.close_value)):
            if isinstance(value, Decimal) and not record.low_value <= value <= record.high_value:
                issues.append(
                    ValidationIssue(
                        f"{field_name}_outside_range",
                        f"{field_name} value must be between low and high",
                    )
                )
    return issues
