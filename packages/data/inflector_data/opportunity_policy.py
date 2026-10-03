"""Versioned policy for deterministic current V5 opportunity discovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class OpportunityDiscoveryPolicy:
    code: str
    version: str
    scoring_policy_asset: str
    scoring_policy_asset_checksum_sha256: str
    research_profile_asset: str
    research_profile_checksum_sha256: str
    financial_primitive_policy_checksum_sha256: str
    financial_endpoint_policy_checksum_sha256: str
    snapshot_selection_version: str
    snapshot_selection_semantics: str
    ranking_version: str
    ranking_key: str
    maximum_snapshot_age_days: int
    freshness_semantics: str
    score_tie_semantics: str
    display_tie_order: tuple[str, ...]
    universe_mode: str
    maximum_symbols: int
    unranked_reason_priority: tuple[str, ...]
    checksum_sha256: str


def canonical_json_sha256(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_opportunity_discovery_policy(
    path: Path, *, repository_root: Path
) -> OpportunityDiscoveryPolicy:
    value = _object(path, "opportunity discovery policy")
    expected = {
        "opportunity_discovery_policy_code",
        "policy_version",
        "scoring_policy_asset",
        "scoring_policy_asset_checksum_sha256",
        "research_profile_asset",
        "research_profile_checksum_sha256",
        "financial_primitive_policy_checksum_sha256",
        "financial_endpoint_policy_checksum_sha256",
        "snapshot_selection_version",
        "snapshot_selection_semantics",
        "ranking_version",
        "ranking_key",
        "maximum_snapshot_age_days",
        "freshness_semantics",
        "score_tie_semantics",
        "display_tie_order",
        "universe_mode",
        "maximum_symbols",
        "unranked_reason_priority",
    }
    if set(value) != expected:
        raise ValueError("opportunity discovery policy fields are incomplete or unsupported")
    exact = {
        "snapshot_selection_version": "latest_visible_bound_score_snapshot_v1",
        "snapshot_selection_semantics": (
            "latest_knowledge_cutoff_per_security_at_or_before_discovery_cutoff"
        ),
        "ranking_version": "v5_final_score_dense_rank_v1",
        "ranking_key": "final_score_desc",
        "freshness_semantics": "calendar_date_age_binary_gate_v1",
        "score_tie_semantics": "dense_equal_final_score_rank_v1",
        "universe_mode": "explicit_symbols_file",
    }
    for field, required in exact.items():
        if _text(value, field) != required:
            raise ValueError(f"opportunity discovery {field} is unsupported")
    maximum_age = _positive_int(value, "maximum_snapshot_age_days")
    if maximum_age != 7:
        raise ValueError("Production K V1 maximum snapshot age must be seven calendar days")
    maximum_symbols = _positive_int(value, "maximum_symbols")
    if maximum_symbols > 5000:
        raise ValueError("opportunity discovery maximum_symbols must not exceed 5000")
    display_ties = _strings(value, "display_tie_order")
    if display_ties != ("symbol_asc", "security_id_asc"):
        raise ValueError("opportunity display tie order is unsupported")
    reasons = _strings(value, "unranked_reason_priority")
    required_reasons = {
        "identity_unavailable",
        "no_visible_snapshot",
        "configuration_mismatch",
        "ambiguous_latest_snapshot",
        "invalid_snapshot_state",
        "stale_snapshot",
        "partial_score",
        "v5_ineligible",
    }
    if set(reasons) != required_reasons:
        raise ValueError("opportunity unranked reason policy is incomplete")
    root = repository_root.resolve()
    _verify_asset(
        root,
        _text(value, "scoring_policy_asset"),
        _sha(value, "scoring_policy_asset_checksum_sha256"),
    )
    _verify_asset(
        root,
        _text(value, "research_profile_asset"),
        _sha(value, "research_profile_checksum_sha256"),
    )
    return OpportunityDiscoveryPolicy(
        code=_text(value, "opportunity_discovery_policy_code"),
        version=_text(value, "policy_version"),
        scoring_policy_asset=_text(value, "scoring_policy_asset"),
        scoring_policy_asset_checksum_sha256=_sha(
            value, "scoring_policy_asset_checksum_sha256"
        ),
        research_profile_asset=_text(value, "research_profile_asset"),
        research_profile_checksum_sha256=_sha(value, "research_profile_checksum_sha256"),
        financial_primitive_policy_checksum_sha256=_sha(
            value, "financial_primitive_policy_checksum_sha256"
        ),
        financial_endpoint_policy_checksum_sha256=_sha(
            value, "financial_endpoint_policy_checksum_sha256"
        ),
        snapshot_selection_version=exact["snapshot_selection_version"],
        snapshot_selection_semantics=exact["snapshot_selection_semantics"],
        ranking_version=exact["ranking_version"],
        ranking_key=exact["ranking_key"],
        maximum_snapshot_age_days=maximum_age,
        freshness_semantics=exact["freshness_semantics"],
        score_tie_semantics=exact["score_tie_semantics"],
        display_tie_order=display_ties,
        universe_mode=exact["universe_mode"],
        maximum_symbols=maximum_symbols,
        unranked_reason_priority=reasons,
        checksum_sha256=canonical_json_sha256(value),
    )


def _verify_asset(root: Path, relative: str, expected: str) -> None:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("opportunity policy asset must remain inside repository") from error
    actual = canonical_json_sha256(_object(path, "bound opportunity policy asset"))
    if actual != expected:
        raise ValueError(f"bound opportunity policy asset checksum mismatch: {relative}")


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


def _positive_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return item


def _strings(value: dict[str, Any], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or not item:
        raise ValueError(f"{field} must be a non-empty string list")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result
