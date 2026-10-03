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
    delivery_history_calendar_lookback_days: int | None
    automatic_financial_endpoint: bool
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
    opportunity_monitoring_enabled: bool
    opportunity_discovery_policy_asset: str | None
    opportunity_discovery_policy_code: str | None
    opportunity_discovery_policy_checksum_sha256: str | None
    opportunity_change_policy_asset: str | None
    opportunity_change_policy_code: str | None
    opportunity_change_policy_checksum_sha256: str | None
    opportunity_discovery_cutoff_semantics: str | None
    opportunity_change_baseline_semantics: str | None
    opportunity_change_no_baseline_behavior: str | None
    notification_projection_enabled: bool
    research_alert_policy_asset: str | None
    research_alert_policy_code: str | None
    research_alert_policy_checksum_sha256: str | None
    notification_projection_semantics: str | None
    notification_no_change_run_behavior: str | None
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
    supported = set(expected)
    supported.add("delivery_history_calendar_lookback_days")
    supported_auto = {*supported, "automatic_financial_endpoint"}
    monitoring_fields = {
        "opportunity_monitoring_enabled",
        "opportunity_discovery_policy_asset",
        "opportunity_discovery_policy_code",
        "opportunity_discovery_policy_checksum_sha256",
        "opportunity_change_policy_asset",
        "opportunity_change_policy_code",
        "opportunity_change_policy_checksum_sha256",
        "opportunity_discovery_cutoff_semantics",
        "opportunity_change_baseline_semantics",
        "opportunity_change_no_baseline_behavior",
    }
    supported_monitoring = {*supported_auto, *monitoring_fields}
    notification_fields = {
        "notification_projection_enabled",
        "research_alert_policy_asset",
        "research_alert_policy_code",
        "research_alert_policy_checksum_sha256",
        "notification_projection_semantics",
        "notification_no_change_run_behavior",
    }
    supported_notifications = {*supported_monitoring, *notification_fields}
    if frozenset(value) not in {
        frozenset(expected),
        frozenset(supported),
        frozenset(supported_auto),
        frozenset(supported_monitoring),
        frozenset(supported_notifications),
    }:
        raise ValueError("operations profile fields are incomplete or unsupported")
    timezone = _text(value, "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("operations profile timezone is unknown") from error
    scheduled_time = _text(value, "scheduled_local_time")
    _validate_time(scheduled_time)
    lookback = _integer(value, "market_history_calendar_lookback_days", minimum=1, maximum=150)
    delivery_lookback = (
        _integer(value, "delivery_history_calendar_lookback_days", minimum=1, maximum=150)
        if "delivery_history_calendar_lookback_days" in value
        else None
    )
    maximum_symbols = _integer(value, "maximum_symbols_per_cycle", minimum=1, maximum=100)
    recent_hard_limit = _integer(value, "recent_runs_hard_limit", minimum=1, maximum=100)
    recent_default = _integer(value, "recent_runs_limit", minimum=1, maximum=recent_hard_limit)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    monitoring_enabled = (
        _boolean(value, "opportunity_monitoring_enabled")
        if "opportunity_monitoring_enabled" in value
        else False
    )
    if monitoring_enabled:
        _exact_text(
            value,
            "opportunity_discovery_policy_code",
            "production_opportunity_discovery_v1",
        )
        _exact_text(
            value, "opportunity_change_policy_code", "production_opportunity_change_v1"
        )
        _sha256(value, "opportunity_discovery_policy_checksum_sha256")
        _sha256(value, "opportunity_change_policy_checksum_sha256")
        _exact_text(
            value,
            "opportunity_discovery_cutoff_semantics",
            "operational_knowledge_cutoff_v1",
        )
        _exact_text(
            value,
            "opportunity_change_baseline_semantics",
            "latest_prior_compatible_completed_monitoring_stage_v1",
        )
        _exact_text(
            value,
            "opportunity_change_no_baseline_behavior",
            "complete_without_change_run_v1",
        )
    notification_enabled = (
        _boolean(value, "notification_projection_enabled")
        if "notification_projection_enabled" in value
        else False
    )
    if notification_enabled:
        if not monitoring_enabled:
            raise ValueError("notification projection requires opportunity monitoring")
        _exact_text(
            value,
            "research_alert_policy_code",
            "production_research_alert_policy_v1",
        )
        _sha256(value, "research_alert_policy_checksum_sha256")
        _exact_text(
            value,
            "notification_projection_semantics",
            "configured_l_change_trigger_projection_v1",
        )
        _exact_text(
            value,
            "notification_no_change_run_behavior",
            "complete_without_outbox_rows_v1",
        )
    return ProductionOperationsProfile(
        operations_profile_code=_text(value, "operations_profile_code"),
        profile_version=_text(value, "profile_version"),
        timezone=timezone,
        scheduled_local_time=scheduled_time,
        market_history_calendar_lookback_days=lookback,
        delivery_history_calendar_lookback_days=delivery_lookback,
        automatic_financial_endpoint=(
            _boolean(value, "automatic_financial_endpoint")
            if "automatic_financial_endpoint" in value
            else False
        ),
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
        opportunity_monitoring_enabled=monitoring_enabled,
        opportunity_discovery_policy_asset=(
            _text(value, "opportunity_discovery_policy_asset")
            if monitoring_enabled
            else None
        ),
        opportunity_discovery_policy_code=(
            _text(value, "opportunity_discovery_policy_code")
            if monitoring_enabled
            else None
        ),
        opportunity_discovery_policy_checksum_sha256=(
            _sha256(value, "opportunity_discovery_policy_checksum_sha256")
            if monitoring_enabled
            else None
        ),
        opportunity_change_policy_asset=(
            _text(value, "opportunity_change_policy_asset")
            if monitoring_enabled
            else None
        ),
        opportunity_change_policy_code=(
            _text(value, "opportunity_change_policy_code")
            if monitoring_enabled
            else None
        ),
        opportunity_change_policy_checksum_sha256=(
            _sha256(value, "opportunity_change_policy_checksum_sha256")
            if monitoring_enabled
            else None
        ),
        opportunity_discovery_cutoff_semantics=(
            _text(value, "opportunity_discovery_cutoff_semantics")
            if monitoring_enabled
            else None
        ),
        opportunity_change_baseline_semantics=(
            _text(value, "opportunity_change_baseline_semantics")
            if monitoring_enabled
            else None
        ),
        opportunity_change_no_baseline_behavior=(
            _text(value, "opportunity_change_no_baseline_behavior")
            if monitoring_enabled
            else None
        ),
        notification_projection_enabled=notification_enabled,
        research_alert_policy_asset=(
            _text(value, "research_alert_policy_asset")
            if notification_enabled
            else None
        ),
        research_alert_policy_code=(
            _text(value, "research_alert_policy_code")
            if notification_enabled
            else None
        ),
        research_alert_policy_checksum_sha256=(
            _sha256(value, "research_alert_policy_checksum_sha256")
            if notification_enabled
            else None
        ),
        notification_projection_semantics=(
            _text(value, "notification_projection_semantics")
            if notification_enabled
            else None
        ),
        notification_no_change_run_behavior=(
            _text(value, "notification_no_change_run_behavior")
            if notification_enabled
            else None
        ),
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


def _sha256(value: dict[str, Any], field: str) -> str:
    item = _text(value, field)
    if len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return item


def _validate_time(value: str) -> None:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ValueError("scheduled_local_time must use HH:MM")
    hour, minute = (int(part) for part in parts)
    if hour > 23 or minute > 59:
        raise ValueError("scheduled_local_time must be a valid local time")
