"""Factual final launch decision from machine evidence and operator attestations."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CODE_ATTESTATIONS = ("github_ci_success",)
ENVIRONMENT_ATTESTATIONS = (
    "production_database_connected",
    "pre_migration_backup_completed",
    "restore_verification_completed",
    "migration_head_verified",
    "persistent_storage_verified",
    "production_environment_installed",
    "provider_acquisition_completed",
    "research_v4_initialized",
    "champion_bootstrap_completed",
    "model_release_readiness_passed",
    "controlled_cycle_completed",
    "release_binding_verified",
    "champion_score_parity_verified",
    "telegram_projection_without_network_verified",
    "reviewed_delivery_selected",
    "telegram_single_message_smoke_passed",
    "telegram_already_delivered_retry_verified",
    "api_health_passed",
    "web_health_passed",
    "caddy_https_passed",
    "systemd_units_validated",
    "recurring_timer_disabled",
    "monitoring_ready",
    "rollback_runbook_reviewed",
)


class LaunchGateError(RuntimeError):
    pass


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LaunchGateError(f"{path.name} must contain one JSON object")
    return value


def decide_launch(
    *, expected_sha: str, preflight: dict[str, Any], attestations: dict[str, Any]
) -> dict[str, object]:
    if SHA_PATTERN.fullmatch(expected_sha) is None:
        raise LaunchGateError("expected SHA must be exactly 40 lowercase hexadecimal characters")
    integrity_blockers: list[str] = []
    if preflight.get("expected_sha") != expected_sha:
        integrity_blockers.append("preflight_sha_mismatch")
    if attestations.get("reviewed_git_sha") != expected_sha:
        integrity_blockers.append("attested_sha_mismatch")
    if preflight.get("blocker_class") == "integrity":
        integrity_blockers.append("preflight_integrity_failure")
    if integrity_blockers:
        decision = "NO_GO_INTEGRITY"
        blockers = integrity_blockers
    else:
        code_blockers = [name for name in CODE_ATTESTATIONS if attestations.get(name) is not True]
        if not str(attestations.get("github_ci_run_id", "")).strip():
            code_blockers.append("github_ci_run_id")
        if code_blockers:
            decision = "NO_GO_CODE_OR_CI"
            blockers = code_blockers
        else:
            environment_blockers = [
                name for name in ENVIRONMENT_ATTESTATIONS if attestations.get(name) is not True
            ]
            if preflight.get("status") != "pass":
                environment_blockers.insert(0, "production_preflight")
            if environment_blockers:
                decision = "NO_GO_ENVIRONMENT"
                blockers = environment_blockers
            else:
                decision = "GO_FOR_CONTROLLED_PRODUCTION_LAUNCH"
                blockers = []
    return {
        "status": decision,
        "expected_sha": expected_sha,
        "blockers": blockers,
        "automatic_side_effects_performed": False,
        "recurring_timer_automatically_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--preflight-report", type=Path, required=True)
    parser.add_argument("--operator-attestations", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = decide_launch(
            expected_sha=args.expected_sha,
            preflight=_object(args.preflight_report),
            attestations=_object(args.operator_attestations),
        )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "GO_FOR_CONTROLLED_PRODUCTION_LAUNCH" else 1
    except (LaunchGateError, OSError, ValueError) as error:
        print(json.dumps({"status": "NO_GO_INTEGRITY", "error_code": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
