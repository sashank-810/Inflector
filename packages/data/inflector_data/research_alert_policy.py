"""Versioned policy for factual Production L notification projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from inflector_data.opportunity_change_policy import load_opportunity_change_policy


@dataclass(frozen=True, slots=True)
class ResearchAlertPolicy:
    code: str
    version: str
    opportunity_change_policy_code: str
    opportunity_change_policy_asset: str
    opportunity_change_policy_checksum_sha256: str
    eligibility_semantics_version: str
    included_trigger_codes: tuple[str, ...]
    payload_schema_version: str
    deduplication_semantics: str
    ordering_semantics: str
    no_severity_semantics: str
    allowed_change_codes: frozenset[str]
    checksum_sha256: str


def canonical_json_sha256(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_research_alert_policy(
    path: Path, *, repository_root: Path
) -> ResearchAlertPolicy:
    value = _object(path, "research alert policy")
    expected = {
        "research_alert_policy_code",
        "policy_version",
        "opportunity_change_policy_code",
        "opportunity_change_policy_asset",
        "opportunity_change_policy_checksum_sha256",
        "eligibility_semantics_version",
        "included_trigger_codes",
        "payload_schema_version",
        "deduplication_semantics",
        "ordering_semantics",
        "no_severity_semantics",
    }
    if set(value) != expected:
        raise ValueError("research alert policy fields are incomplete or unsupported")
    required = {
        "research_alert_policy_code": "production_research_alert_policy_v1",
        "policy_version": "1",
        "opportunity_change_policy_code": "production_opportunity_change_v1",
        "eligibility_semantics_version": "configured_change_code_intersection_v1",
        "payload_schema_version": "research_notification_payload_v1",
        "deduplication_semantics": (
            "policy_change_run_item_matched_triggers_payload_schema_v1"
        ),
        "ordering_semantics": "policy_trigger_order_then_identity_v1",
        "no_severity_semantics": "peer_events_without_severity_v1",
    }
    for field, expected_value in required.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"research alert {field} is unsupported")

    root = repository_root.resolve()
    asset = _text(value, "opportunity_change_policy_asset")
    candidate = (root / asset).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("bound change policy must remain inside repository") from error
    change = load_opportunity_change_policy(candidate, repository_root=root)
    expected_change_checksum = _sha(value, "opportunity_change_policy_checksum_sha256")
    if (
        change.code != required["opportunity_change_policy_code"]
        or change.checksum_sha256 != expected_change_checksum
    ):
        raise ValueError("bound Production L change policy checksum mismatch")

    triggers = _strings(value, "included_trigger_codes")
    allowed = frozenset(change.change_code_order)
    if not set(triggers) <= allowed:
        raise ValueError("research alert policy contains unknown L change codes")
    if not triggers:
        raise ValueError("research alert policy must include at least one trigger")
    return ResearchAlertPolicy(
        code=required["research_alert_policy_code"],
        version=required["policy_version"],
        opportunity_change_policy_code=required["opportunity_change_policy_code"],
        opportunity_change_policy_asset=asset,
        opportunity_change_policy_checksum_sha256=expected_change_checksum,
        eligibility_semantics_version=required["eligibility_semantics_version"],
        included_trigger_codes=triggers,
        payload_schema_version=required["payload_schema_version"],
        deduplication_semantics=required["deduplication_semantics"],
        ordering_semantics=required["ordering_semantics"],
        no_severity_semantics=required["no_severity_semantics"],
        allowed_change_codes=allowed,
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


def _strings(value: dict[str, Any], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list):
        raise ValueError(f"{field} must be a string list")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result
