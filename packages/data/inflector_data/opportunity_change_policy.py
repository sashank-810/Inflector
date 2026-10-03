"""Versioned policy for deterministic Production K run comparison."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class OpportunityChangePolicy:
    code: str
    version: str
    discovery_policy_code: str
    discovery_policy_asset: str
    discovery_policy_checksum_sha256: str
    comparison_version: str
    score_change_semantics: str
    rank_change_semantics: str
    component_change_semantics: str
    universe_membership_semantics: str
    materiality_semantics: str
    presentation_order_semantics: str
    maximum_summary_limit: int
    change_code_order: tuple[str, ...]
    neutral_change_codes: frozenset[str]
    checksum_sha256: str


def canonical_json_sha256(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_opportunity_change_policy(
    path: Path, *, repository_root: Path
) -> OpportunityChangePolicy:
    value = _object(path, "opportunity change policy")
    expected = {
        "opportunity_change_policy_code",
        "policy_version",
        "discovery_policy_code",
        "discovery_policy_asset",
        "discovery_policy_checksum_sha256",
        "comparison_version",
        "score_change_semantics",
        "rank_change_semantics",
        "component_change_semantics",
        "universe_membership_semantics",
        "materiality_semantics",
        "presentation_order_semantics",
        "maximum_summary_limit",
        "change_code_order",
        "neutral_change_codes",
    }
    if set(value) != expected:
        raise ValueError("opportunity change policy fields are incomplete or unsupported")
    required = {
        "opportunity_change_policy_code": "production_opportunity_change_v1",
        "policy_version": "1",
        "discovery_policy_code": "production_opportunity_discovery_v1",
        "comparison_version": "opportunity_discovery_run_comparison_v1",
        "score_change_semantics": "exact_decimal_nonzero_change_v1",
        "rank_change_semantics": "baseline_dense_rank_minus_current_dense_rank_v1",
        "component_change_semantics": "persisted_top_level_component_state_v1",
        "universe_membership_semantics": (
            "security_id_then_unavailable_identity_symbol_v1"
        ),
        "materiality_semantics": "persist_every_exact_research_state_change_v1",
        "presentation_order_semantics": (
            "identity_then_current_symbol_then_baseline_symbol_v1"
        ),
    }
    for field, expected_value in required.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"opportunity change {field} is unsupported")
    maximum = _positive_int(value, "maximum_summary_limit")
    if maximum > 5000:
        raise ValueError("opportunity change maximum_summary_limit must not exceed 5000")
    change_codes = _strings(value, "change_code_order")
    required_codes = {
        "entered_compared_universe",
        "left_compared_universe",
        "symbol_changed",
        "snapshot_changed",
        "snapshot_unchanged",
        "became_rankable",
        "lost_rankability",
        "remained_rankable",
        "remained_unranked",
        "score_increased",
        "score_decreased",
        "score_unchanged",
        "rank_moved_up",
        "rank_moved_down",
        "rank_unchanged",
        "coverage_completed",
        "coverage_regressed",
        "coverage_changed",
        "components_gained",
        "components_lost",
        "component_scores_changed",
        "component_contributions_changed",
        "became_eligible",
        "became_ineligible",
        "eligibility_unchanged",
        "became_stale",
        "recovered_from_stale",
        "confidence_changed",
        "unranked_reason_changed",
    }
    if set(change_codes) != required_codes:
        raise ValueError("opportunity change code policy is incomplete")
    neutral = frozenset(_strings(value, "neutral_change_codes"))
    if not neutral or not neutral < required_codes:
        raise ValueError("opportunity neutral change codes are invalid")
    root = repository_root.resolve()
    asset = _text(value, "discovery_policy_asset")
    expected_checksum = _sha(value, "discovery_policy_checksum_sha256")
    candidate = (root / asset).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("bound discovery policy must remain inside repository") from error
    if canonical_json_sha256(_object(candidate, "bound discovery policy")) != expected_checksum:
        raise ValueError("bound Production K discovery policy checksum mismatch")
    return OpportunityChangePolicy(
        code=required["opportunity_change_policy_code"],
        version=required["policy_version"],
        discovery_policy_code=required["discovery_policy_code"],
        discovery_policy_asset=asset,
        discovery_policy_checksum_sha256=expected_checksum,
        comparison_version=required["comparison_version"],
        score_change_semantics=required["score_change_semantics"],
        rank_change_semantics=required["rank_change_semantics"],
        component_change_semantics=required["component_change_semantics"],
        universe_membership_semantics=required["universe_membership_semantics"],
        materiality_semantics=required["materiality_semantics"],
        presentation_order_semantics=required["presentation_order_semantics"],
        maximum_summary_limit=maximum,
        change_code_order=change_codes,
        neutral_change_codes=neutral,
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
