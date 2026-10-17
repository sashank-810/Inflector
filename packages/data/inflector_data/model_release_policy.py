"""Strict immutable Production U model-release governance policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from inflector_data.historical_evaluation_policy import canonical_json_sha256

POLICY_CODE = "production_model_release_v1"
POLICY_CHECKSUM = "7094654224d0e305ae46de8babfec4c0c5368936605b807eeca0468a15ec1691"
CHAMPION_CHECKSUM = "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
S2_CHECKSUM = "17642f0e05640464b0166bc82740882e6fda5c99e62ca555c19d7be30ebe85e0"
T_POLICY_CODE = "production_multibagger_temporal_holdout_v1"
T_CHECKSUM = "309547a20c01abcece498c76727abf032d643efde6207dab2667519bc28ab45b"
RELEASE_STATES = (
    "candidate_created",
    "shadow_ready",
    "shadow_running",
    "burn_in_complete",
    "activation_approved",
    "active",
    "rolled_back",
    "rejected",
)
T_DECISIONS = (
    "CHAMPION_RETAINED_NO_CHALLENGER",
    "CHALLENGER_FAILS_HOLDOUT",
    "CHALLENGER_PASSES_HOLDOUT",
)


@dataclass(frozen=True, slots=True)
class ModelReleasePolicy:
    code: str
    version: str
    checksum_sha256: str
    champion_checksum_sha256: str
    s2_checksum_sha256: str
    t_policy_code: str
    t_checksum_sha256: str
    release_states: tuple[str, ...]
    allowed_t_decisions: tuple[str, ...]
    shadow_requirements: dict[str, object]
    burn_in_requirements: dict[str, object]
    activation_requirements: dict[str, object]
    rollback_rules: dict[str, object]

    @property
    def minimum_completed_cycles(self) -> int:
        return _integer(self.burn_in_requirements, "minimum_completed_cycles")

    @property
    def minimum_wall_clock_hours(self) -> int:
        return _integer(self.burn_in_requirements, "minimum_wall_clock_hours")

    @property
    def maximum_p95_latency_ms(self) -> Decimal:
        value = self.burn_in_requirements["maximum_p95_latency_ms"]
        if not isinstance(value, str):
            raise ValueError("burn-in latency threshold must be an exact Decimal string")
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValueError("burn-in latency threshold is malformed") from error


def load_model_release_policy(path: Path) -> ModelReleasePolicy:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("model release policy is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("model release policy must be an object")
    expected = {
        "model_release_policy_code",
        "policy_version",
        "champion_scoring_configuration_checksum_sha256",
        "challenger_development_policy_checksum_sha256",
        "temporal_holdout_policy_code",
        "temporal_holdout_policy_checksum_sha256",
        "release_states",
        "allowed_t_decisions",
        "shadow_requirements",
        "burn_in_requirements",
        "activation_requirements",
        "rollback_rules",
        "fixture_evidence",
        "automatic_promotion",
        "future_outcome_burn_in",
        "model_tuning",
        "model_training",
    }
    if set(value) != expected:
        raise ValueError("model release policy fields are not exact")
    exact = {
        "model_release_policy_code": POLICY_CODE,
        "champion_scoring_configuration_checksum_sha256": CHAMPION_CHECKSUM,
        "challenger_development_policy_checksum_sha256": S2_CHECKSUM,
        "temporal_holdout_policy_code": T_POLICY_CODE,
        "temporal_holdout_policy_checksum_sha256": T_CHECKSUM,
        "fixture_evidence": "forbidden_for_activation",
        "automatic_promotion": "forbidden",
        "future_outcome_burn_in": "forbidden",
        "model_tuning": "forbidden",
        "model_training": "forbidden",
    }
    for field, required in exact.items():
        if value[field] != required:
            raise ValueError(f"model release policy {field} binding mismatch")
    if value["policy_version"] != "1.0.0":
        raise ValueError("model release policy version mismatch")
    if tuple(value["release_states"]) != RELEASE_STATES:
        raise ValueError("model release states are not exact")
    if tuple(value["allowed_t_decisions"]) != T_DECISIONS:
        raise ValueError("model release T decisions are not exact")
    shadow = _mapping(value, "shadow_requirements")
    if shadow != {
        "same_source_state_required": True,
        "rankability_delta_required": 0,
        "coverage_delta_required": 0,
        "production_notifications_forbidden": True,
    }:
        raise ValueError("model release shadow requirements are not exact")
    burn_in = _mapping(value, "burn_in_requirements")
    required_burn_fields = {
        "minimum_completed_cycles",
        "minimum_wall_clock_hours",
        "maximum_p95_latency_ms",
        "maximum_scheduler_failures",
        "maximum_integrity_violations",
        "maximum_score_definition_drifts",
        "maximum_rankability_drifts",
        "maximum_coverage_drifts",
        "maximum_missing_component_events",
        "maximum_production_health_regressions",
        "maximum_notification_contamination_events",
    }
    if set(burn_in) != required_burn_fields:
        raise ValueError("model release burn-in requirements are not exact")
    if _integer(burn_in, "minimum_completed_cycles") < 1:
        raise ValueError("burn-in requires completed cycles")
    if _integer(burn_in, "minimum_wall_clock_hours") < 1:
        raise ValueError("burn-in requires wall-clock duration")
    for field in required_burn_fields - {
        "minimum_completed_cycles",
        "minimum_wall_clock_hours",
        "maximum_p95_latency_ms",
    }:
        if _integer(burn_in, field) != 0:
            raise ValueError(f"burn-in {field} must remain zero")
    activation = _mapping(value, "activation_requirements")
    if activation != {
        "authoritative_t_pass_required_for_challenger": True,
        "completed_burn_in_required": True,
        "persisted_manual_approval_required": True,
        "active_champion_required": True,
    }:
        raise ValueError("model release activation requirements are not exact")
    rollback = _mapping(value, "rollback_rules")
    if rollback != {
        "previous_accepted_release_required": True,
        "immutable_history_required": True,
        "automatic_operational_safety_rollback_allowed": True,
        "automatic_promotion_allowed": False,
        "allowed_reason_codes": [
            "integrity_violation",
            "score_definition_mismatch",
            "missing_component_state",
            "production_error_rate_threshold",
            "latency_threshold_violation",
            "scheduler_research_failure_threshold",
            "notification_contamination",
            "manual_emergency_rollback",
        ],
    }:
        raise ValueError("model release rollback rules are not exact")
    checksum = canonical_json_sha256(value)
    if checksum != POLICY_CHECKSUM:
        raise ValueError("model release policy canonical checksum mismatch")
    policy = ModelReleasePolicy(
        code=POLICY_CODE,
        version="1.0.0",
        checksum_sha256=checksum,
        champion_checksum_sha256=CHAMPION_CHECKSUM,
        s2_checksum_sha256=S2_CHECKSUM,
        t_policy_code=T_POLICY_CODE,
        t_checksum_sha256=T_CHECKSUM,
        release_states=RELEASE_STATES,
        allowed_t_decisions=T_DECISIONS,
        shadow_requirements=shadow,
        burn_in_requirements=burn_in,
        activation_requirements=activation,
        rollback_rules=rollback,
    )
    _ = policy.maximum_p95_latency_ms
    return policy


def _mapping(value: dict[str, object], field: str) -> dict[str, object]:
    item = value[field]
    if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
        raise ValueError(f"model release {field} must be an object")
    return dict(item)


def _integer(value: dict[str, object], field: str) -> int:
    item = value[field]
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"model release {field} must be an integer")
    return item
