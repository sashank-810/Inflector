"""Versioned reliability policy for Production O notification delivery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from inflector_data.research_alert_policy import load_research_alert_policy


@dataclass(frozen=True, slots=True)
class NotificationDeliveryPolicy:
    code: str
    version: str
    research_alert_policy_code: str
    research_alert_policy_asset: str
    research_alert_policy_checksum_sha256: str
    transport_code: str
    rendering_version: str
    target_code: str
    delivery_identity_semantics: str
    claim_semantics: str
    claim_lease_seconds: int
    maximum_attempts: int
    retry_schedule_seconds: tuple[int, ...]
    retryable_http_statuses: frozenset[int]
    retryable_http_status_classes: tuple[str, ...]
    permanent_http_statuses: frozenset[int]
    retryable_telegram_error_codes: frozenset[int]
    retryable_telegram_error_code_classes: tuple[str, ...]
    permanent_telegram_error_codes: frozenset[int]
    malformed_response_semantics: str
    ordering_semantics: str
    maximum_messages_per_cycle: int
    network_timeout_seconds: int
    checksum_sha256: str

    def retry_delay_seconds(self, attempt_number: int) -> int:
        if not 1 <= attempt_number < self.maximum_attempts:
            raise ValueError("retry delay requires a non-terminal attempt number")
        return self.retry_schedule_seconds[attempt_number - 1]

    def is_retryable_http_status(self, status: int) -> bool:
        return status in self.retryable_http_statuses or (
            "5xx" in self.retryable_http_status_classes and 500 <= status <= 599
        )

    def is_retryable_telegram_error_code(self, code: int) -> bool:
        return code in self.retryable_telegram_error_codes or (
            "5xx" in self.retryable_telegram_error_code_classes
            and 500 <= code <= 599
        )


def canonical_json_sha256(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_notification_delivery_policy(
    path: Path, *, repository_root: Path
) -> NotificationDeliveryPolicy:
    value = _object(path, "notification delivery policy")
    expected = {
        "notification_delivery_policy_code",
        "policy_version",
        "research_alert_policy_code",
        "research_alert_policy_asset",
        "research_alert_policy_checksum_sha256",
        "transport_code",
        "rendering_version",
        "target_code",
        "delivery_identity_semantics",
        "claim_semantics",
        "claim_lease_seconds",
        "maximum_attempts",
        "retry_schedule_seconds",
        "retryable_http_statuses",
        "retryable_http_status_classes",
        "permanent_http_statuses",
        "retryable_telegram_error_codes",
        "retryable_telegram_error_code_classes",
        "permanent_telegram_error_codes",
        "malformed_response_semantics",
        "ordering_semantics",
        "maximum_messages_per_cycle",
        "network_timeout_seconds",
    }
    if set(value) != expected:
        raise ValueError("notification delivery policy fields are incomplete or unsupported")
    required = {
        "notification_delivery_policy_code": "production_notification_delivery_v1",
        "policy_version": "1",
        "research_alert_policy_code": "production_research_alert_policy_v1",
        "transport_code": "telegram_bot_api_v1",
        "rendering_version": "research_notification_telegram_v1",
        "target_code": "primary_telegram",
        "delivery_identity_semantics": "outbox_policy_transport_target_renderer_v1",
        "claim_semantics": "transactional_lease_with_expiry_recovery_v1",
        "malformed_response_semantics": "retryable_failure",
        "ordering_semantics": "due_time_then_created_at_then_delivery_key_v1",
    }
    for field, expected_value in required.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"notification delivery {field} is unsupported")
    lease_seconds = _integer(value, "claim_lease_seconds", minimum=30, maximum=3600)
    maximum_attempts = _integer(value, "maximum_attempts", minimum=1, maximum=10)
    schedule = _integers(value, "retry_schedule_seconds")
    if len(schedule) != maximum_attempts - 1 or any(item <= 0 for item in schedule):
        raise ValueError("retry schedule must define every non-terminal attempt")
    retryable_statuses = frozenset(_integers(value, "retryable_http_statuses"))
    retryable_classes = _strings(value, "retryable_http_status_classes")
    if retryable_classes != ("5xx",):
        raise ValueError("only the explicit 5xx retry class is supported")
    permanent_statuses = frozenset(_integers(value, "permanent_http_statuses"))
    if retryable_statuses & permanent_statuses:
        raise ValueError("retryable and permanent HTTP statuses overlap")
    if retryable_statuses != {429} or permanent_statuses != {400, 401, 403}:
        raise ValueError("Telegram HTTP failure semantics are unsupported")
    retryable_telegram_codes = frozenset(
        _integers(value, "retryable_telegram_error_codes")
    )
    retryable_telegram_classes = _strings(
        value, "retryable_telegram_error_code_classes"
    )
    permanent_telegram_codes = frozenset(
        _integers(value, "permanent_telegram_error_codes")
    )
    if retryable_telegram_classes != ("5xx",):
        raise ValueError("only the explicit Telegram 5xx retry class is supported")
    if retryable_telegram_codes & permanent_telegram_codes:
        raise ValueError("retryable and permanent Telegram error codes overlap")
    if (
        retryable_telegram_codes != {429}
        or permanent_telegram_codes != {400, 401, 403}
    ):
        raise ValueError("Telegram provider error-code semantics are unsupported")
    root = repository_root.resolve()
    asset = _text(value, "research_alert_policy_asset")
    candidate = (root / asset).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("bound research alert policy must remain inside repository") from error
    alert_policy = load_research_alert_policy(candidate, repository_root=root)
    expected_alert_checksum = _sha(value, "research_alert_policy_checksum_sha256")
    if (
        alert_policy.code != required["research_alert_policy_code"]
        or alert_policy.checksum_sha256 != expected_alert_checksum
    ):
        raise ValueError("bound Production N alert policy checksum mismatch")
    return NotificationDeliveryPolicy(
        code=required["notification_delivery_policy_code"],
        version=required["policy_version"],
        research_alert_policy_code=required["research_alert_policy_code"],
        research_alert_policy_asset=asset,
        research_alert_policy_checksum_sha256=expected_alert_checksum,
        transport_code=required["transport_code"],
        rendering_version=required["rendering_version"],
        target_code=required["target_code"],
        delivery_identity_semantics=required["delivery_identity_semantics"],
        claim_semantics=required["claim_semantics"],
        claim_lease_seconds=lease_seconds,
        maximum_attempts=maximum_attempts,
        retry_schedule_seconds=schedule,
        retryable_http_statuses=retryable_statuses,
        retryable_http_status_classes=retryable_classes,
        permanent_http_statuses=permanent_statuses,
        retryable_telegram_error_codes=retryable_telegram_codes,
        retryable_telegram_error_code_classes=retryable_telegram_classes,
        permanent_telegram_error_codes=permanent_telegram_codes,
        malformed_response_semantics=required["malformed_response_semantics"],
        ordering_semantics=required["ordering_semantics"],
        maximum_messages_per_cycle=_integer(
            value, "maximum_messages_per_cycle", minimum=1, maximum=500
        ),
        network_timeout_seconds=_integer(
            value, "network_timeout_seconds", minimum=1, maximum=60
        ),
        checksum_sha256=canonical_json_sha256(value),
    )


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _text(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or item != item.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return item


def _sha(value: dict[str, Any], field: str) -> str:
    item = _text(value, field)
    if len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return item


def _integer(value: dict[str, Any], field: str, *, minimum: int, maximum: int) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return item


def _integers(value: dict[str, Any], field: str) -> tuple[int, ...]:
    item = value.get(field)
    if not isinstance(item, list) or any(
        isinstance(entry, bool) or not isinstance(entry, int) for entry in item
    ):
        raise ValueError(f"{field} must be an integer list")
    return tuple(item)


def _strings(value: dict[str, Any], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list):
        raise ValueError(f"{field} must be a string list")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result
