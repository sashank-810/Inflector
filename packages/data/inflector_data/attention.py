"""Structural validation for external attention evidence without interpretation."""

from __future__ import annotations

from datetime import UTC, datetime
from re import fullmatch

from inflector_core.providers import AttentionObservationRecord
from inflector_data.validation import ValidationIssue

ATTENTION_METRIC_CODES = frozenset({"news_mentions_count", "analyst_coverage_count"})
ATTENTION_COVERAGE_STATUSES = frozenset({"complete", "partial", "unknown"})


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def validate_attention(
    record: AttentionObservationRecord,
    *,
    available_at: datetime | None,
) -> list[ValidationIssue]:
    """Validate a provider-reported count and its exact measurement semantics."""

    issues = [ValidationIssue(code, code.replace("_", " ")) for code in record.parse_errors]
    if not record.company_legal_name and not record.security_isin:
        issues.append(
            ValidationIssue(
                "missing_company_identity", "company legal name or security ISIN is required"
            )
        )
    if record.metric_code is None:
        issues.append(ValidationIssue("missing_attention_metric", "attention metric is required"))
    elif record.metric_code not in ATTENTION_METRIC_CODES:
        issues.append(
            ValidationIssue("unsupported_attention_metric", "attention metric is unsupported")
        )
    if record.reported_count is None:
        issues.append(ValidationIssue("missing_attention_count", "reported count is required"))
    elif isinstance(record.reported_count, bool) or not isinstance(record.reported_count, int):
        issues.append(
            ValidationIssue("invalid_attention_count", "reported count must be an integer")
        )
    elif record.reported_count < 0:
        issues.append(
            ValidationIssue("negative_attention_count", "reported count cannot be negative")
        )
    if record.reported_unit != "count":
        issues.append(ValidationIssue("invalid_attention_unit", "reported unit must be count"))
    if record.scope_code is None or not record.scope_code.strip():
        issues.append(ValidationIssue("missing_attention_scope", "attention scope is required"))
    elif record.scope_code != record.scope_code.strip():
        issues.append(ValidationIssue("invalid_attention_scope", "attention scope must be trimmed"))
    if record.methodology_version is None or not record.methodology_version.strip():
        issues.append(
            ValidationIssue("missing_attention_methodology", "attention methodology is required")
        )
    elif record.methodology_version != record.methodology_version.strip():
        issues.append(
            ValidationIssue(
                "invalid_attention_methodology", "attention methodology must be trimmed"
            )
        )
    if (
        record.measurement_definition_sha256 is None
        or fullmatch(r"[0-9a-fA-F]{64}", record.measurement_definition_sha256) is None
    ):
        issues.append(
            ValidationIssue(
                "invalid_measurement_definition_sha256",
                "measurement definition must be a SHA-256 hexadecimal digest",
            )
        )
    if record.coverage_status not in ATTENTION_COVERAGE_STATUSES:
        issues.append(
            ValidationIssue(
                "invalid_attention_coverage_status", "attention coverage status is unsupported"
            )
        )

    available_utc = _aware_utc(available_at)
    if available_utc is None:
        issues.append(
            ValidationIssue(
                "missing_attention_available_at",
                "explicit timezone-aware source availability is required",
            )
        )
    if record.metric_code == "news_mentions_count":
        if record.observation_date is not None:
            issues.append(
                ValidationIssue(
                    "invalid_news_observation_date", "news observations cannot have a snapshot date"
                )
            )
        start = _aware_utc(record.window_start_at)
        end = _aware_utc(record.window_end_at)
        if (
            start is None
            or end is None
            or start >= end
            or (available_utc is not None and end > available_utc)
        ):
            issues.append(
                ValidationIssue(
                    "invalid_news_window",
                    "news observations require a valid window ending by source availability",
                )
            )
    elif record.metric_code == "analyst_coverage_count":
        if record.observation_date is None or (
            available_utc is not None and record.observation_date > available_utc.date()
        ):
            issues.append(
                ValidationIssue(
                    "invalid_analyst_snapshot_date",
                    "analyst snapshot date must exist and not exceed source availability",
                )
            )
        if record.window_start_at is not None or record.window_end_at is not None:
            issues.append(
                ValidationIssue(
                    "invalid_analyst_window",
                    "analyst observations cannot have a measurement window",
                )
            )
    return issues
