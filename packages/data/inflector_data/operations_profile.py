"""Versioned operational policy for bounded production-cycle execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class ProductionOperationsProfile:
    operations_profile_code: str
    profile_version: str
    timezone: str
    scheduled_local_time: str
    market_history_calendar_lookback_days: int
    maximum_symbols_per_cycle: int
    gdelt_enabled: bool
    stage_max_attempts: int
    run_lease_seconds: int
    stale_run_policy: str
    run_missed_jobs_when_available: bool
    forbid_multiple_instances: bool
    request_delay_seconds: int
    maximum_financial_filings: int
    maximum_announcements: int
    maximum_documents: int
    recent_runs_limit: int
    recent_runs_hard_limit: int
    checksum_sha256: str


def load_operations_profile(path: Path) -> ProductionOperationsProfile:
    """Load, strictly validate, and canonically fingerprint an operations profile."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("operations profile is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("operations profile root must be an object")
    expected = {
        "operations_profile_code",
        "profile_version",
        "timezone",
        "scheduled_local_time",
        "market_history_calendar_lookback_days",
        "maximum_symbols_per_cycle",
        "gdelt_enabled",
        "stage_max_attempts",
        "run_lease_seconds",
        "stale_run_policy",
        "run_missed_jobs_when_available",
        "forbid_multiple_instances",
        "request_delay_seconds",
        "maximum_financial_filings",
        "maximum_announcements",
        "maximum_documents",
        "recent_runs_limit",
        "recent_runs_hard_limit",
    }
    if set(value) != expected:
        raise ValueError("operations profile fields are incomplete or unsupported")
    timezone = _text(value, "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("operations profile timezone is unknown") from error
    scheduled_time = _text(value, "scheduled_local_time")
    _validate_time(scheduled_time)
    lookback = _integer(value, "market_history_calendar_lookback_days", minimum=1, maximum=150)
    maximum_symbols = _integer(value, "maximum_symbols_per_cycle", minimum=1, maximum=100)
    recent_hard_limit = _integer(value, "recent_runs_hard_limit", minimum=1, maximum=100)
    recent_default = _integer(value, "recent_runs_limit", minimum=1, maximum=recent_hard_limit)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ProductionOperationsProfile(
        operations_profile_code=_text(value, "operations_profile_code"),
        profile_version=_text(value, "profile_version"),
        timezone=timezone,
        scheduled_local_time=scheduled_time,
        market_history_calendar_lookback_days=lookback,
        maximum_symbols_per_cycle=maximum_symbols,
        gdelt_enabled=_boolean(value, "gdelt_enabled"),
        stage_max_attempts=_integer(value, "stage_max_attempts", minimum=1, maximum=5),
        run_lease_seconds=_integer(value, "run_lease_seconds", minimum=60, maximum=86400),
        stale_run_policy=_exact_text(
            value, "stale_run_policy", "explicit_resume_after_lease_expiry"
        ),
        run_missed_jobs_when_available=_boolean(value, "run_missed_jobs_when_available"),
        forbid_multiple_instances=_boolean(value, "forbid_multiple_instances"),
        request_delay_seconds=_integer(value, "request_delay_seconds", minimum=0, maximum=10),
        maximum_financial_filings=_integer(
            value, "maximum_financial_filings", minimum=1, maximum=100
        ),
        maximum_announcements=_integer(value, "maximum_announcements", minimum=1, maximum=1000),
        maximum_documents=_integer(value, "maximum_documents", minimum=0, maximum=500),
        recent_runs_limit=recent_default,
        recent_runs_hard_limit=recent_hard_limit,
        checksum_sha256=sha256(canonical).hexdigest(),
    )


def _text(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item or item != item.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return item


def _exact_text(value: dict[str, Any], field: str, expected: str) -> str:
    item = _text(value, field)
    if item != expected:
        raise ValueError(f"{field} must be {expected}")
    return item


def _integer(value: dict[str, Any], field: str, *, minimum: int, maximum: int) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return item


def _boolean(value: dict[str, Any], field: str) -> bool:
    item = value.get(field)
    if not isinstance(item, bool):
        raise ValueError(f"{field} must be a boolean")
    return item


def _validate_time(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ValueError("scheduled_local_time must use HH:MM")
    hour, minute = (int(part) for part in parts)
    if hour > 23 or minute > 59:
        raise ValueError("scheduled_local_time must be a valid local time")
