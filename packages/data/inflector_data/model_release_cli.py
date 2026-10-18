"""Deterministic operator CLI for Production U model release governance."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from inflector_data.historical_activation_policy import (
    load_historical_evaluation_activation_policy,
    load_multibagger_temporal_split_policy,
)
from inflector_data.model_release import (
    activate_release,
    bootstrap_active_champion,
    complete_burn_in,
    create_activation_approval,
    create_challenger_release_candidate,
    production_release_readiness,
    record_shadow_cycle,
    release_history,
    resolve_active_release,
    rollback_release,
    start_shadow_run,
)
from inflector_data.model_release_policy import load_model_release_policy
from inflector_data.multibagger_challenger_policy import (
    load_multibagger_challenger_development_policy,
)
from inflector_data.multibagger_holdout_policy import load_multibagger_temporal_holdout_policy

LATEST_MIGRATION = "20261009_0028"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inflector Production U model release governance")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "model-release-readiness",
        "bootstrap-active-champion",
        "create-release-candidate",
        "start-shadow-run",
        "record-shadow-cycle",
        "complete-burn-in",
        "create-activation-approval",
        "activate-release",
        "rollback-release",
        "show-active-release",
        "release-history",
    ):
        command = commands.add_parser(name)
        command.add_argument("--database-url", required=True)
        command.add_argument("--release-policy", type=Path, required=True)
    commands.choices["bootstrap-active-champion"].add_argument(
        "--activated-at", type=datetime.fromisoformat, required=True
    )
    commands.choices["create-release-candidate"].add_argument(
        "--holdout-run-id", type=UUID, required=True
    )
    commands.choices["start-shadow-run"].add_argument(
        "--release-candidate-id", type=UUID, required=True
    )
    commands.choices["start-shadow-run"].add_argument(
        "--started-at", type=datetime.fromisoformat, required=True
    )
    shadow = commands.choices["record-shadow-cycle"]
    shadow.add_argument("--shadow-run-id", type=UUID, required=True)
    shadow.add_argument("--cycle-key", required=True)
    shadow.add_argument("--snapshot-id", type=UUID, required=True)
    shadow.add_argument("--observed-at", type=datetime.fromisoformat, required=True)
    shadow.add_argument("--latency-ms", type=Decimal, required=True)
    burn = commands.choices["complete-burn-in"]
    burn.add_argument("--shadow-run-id", type=UUID, required=True)
    burn.add_argument("--completed-at", type=datetime.fromisoformat, required=True)
    approval = commands.choices["create-activation-approval"]
    approval.add_argument("--release-candidate-id", type=UUID, required=True)
    approval.add_argument("--burn-in-run-id", type=UUID, required=True)
    approval.add_argument("--approver-provenance", required=True)
    approval.add_argument("--approved-at", type=datetime.fromisoformat, required=True)
    activation = commands.choices["activate-release"]
    activation.add_argument("--release-candidate-id", type=UUID, required=True)
    activation.add_argument("--approval-id", type=UUID, required=True)
    activation.add_argument("--activated-at", type=datetime.fromisoformat, required=True)
    rollback = commands.choices["rollback-release"]
    rollback.add_argument("--reason-code", required=True)
    rollback.add_argument("--health-evidence-json", type=Path, required=True)
    rollback.add_argument("--rolled-back-at", type=datetime.fromisoformat, required=True)
    readiness = commands.choices["model-release-readiness"]
    readiness.add_argument("--repository-root", type=Path, required=True)
    readiness.add_argument("--observed-at", type=datetime.fromisoformat, required=True)
    readiness.add_argument("--raw-root", type=Path, required=True)
    readiness.add_argument("--gdelt-raw-root", type=Path, required=True)
    readiness.add_argument("--model-family", required=True)
    for name in (
        "model-release-readiness",
        "create-release-candidate",
        "start-shadow-run",
        "activate-release",
    ):
        command = commands.choices[name]
        command.add_argument("--t-policy", type=Path, required=True)
        command.add_argument("--s2-policy", type=Path, required=True)
        command.add_argument("--activation-policy", type=Path, required=True)
        command.add_argument("--split-policy", type=Path, required=True)
    return parser


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    policy = load_model_release_policy(args.release_policy)
    command = args.command
    if command == "model-release-readiness":
        t_policy, s2_policy, activation_policy, split_policy = _upstream(args)
        return production_release_readiness(
            session,
            policy=policy,
            t_policy=t_policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
            repository_root=args.repository_root,
            observed_at=args.observed_at,
            raw_root=args.raw_root,
            gdelt_raw_root=args.gdelt_raw_root,
            model_family=args.model_family,
            telegram_bot_token_present=bool(
                os.getenv("INFLECTOR_TELEGRAM_BOT_TOKEN", "").strip()
            ),
            telegram_chat_id_present=bool(
                os.getenv("INFLECTOR_TELEGRAM_CHAT_ID", "").strip()
            ),
        )
    if command == "bootstrap-active-champion":
        row = bootstrap_active_champion(session, policy=policy, activated_at=args.activated_at)
        return {"status": "ready", "deployment_id": str(row.id), "subject": "champion"}
    if command == "create-release-candidate":
        t_policy, s2_policy, activation_policy, split_policy = _upstream(args)
        row = create_challenger_release_candidate(
            session,
            policy=policy,
            t_policy=t_policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
            holdout_run_id=args.holdout_run_id,
        )
        session.commit()
        return {"status": row.status, "release_candidate_id": str(row.id)}
    if command == "start-shadow-run":
        t_policy, s2_policy, activation_policy, split_policy = _upstream(args)
        row = start_shadow_run(
            session,
            policy=policy,
            t_policy=t_policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
            release_candidate_id=args.release_candidate_id,
            started_at=args.started_at,
        )
        return {"status": row.status, "shadow_run_id": str(row.id)}
    if command == "record-shadow-cycle":
        row = record_shadow_cycle(
            session,
            policy=policy,
            shadow_run_id=args.shadow_run_id,
            cycle_key=args.cycle_key,
            snapshot_id=args.snapshot_id,
            observed_at=args.observed_at,
            latency_ms=args.latency_ms,
        )
        return {"status": "recorded", "shadow_event_id": str(row.id)}
    if command == "complete-burn-in":
        row = complete_burn_in(
            session,
            policy=policy,
            shadow_run_id=args.shadow_run_id,
            completed_at=args.completed_at,
        )
        return {"status": row.status, "burn_in_run_id": str(row.id)}
    if command == "create-activation-approval":
        row = create_activation_approval(
            session,
            policy=policy,
            release_candidate_id=args.release_candidate_id,
            burn_in_run_id=args.burn_in_run_id,
            approver_provenance=args.approver_provenance,
            approved_at=args.approved_at,
        )
        return {"status": "approved", "approval_id": str(row.id)}
    if command == "activate-release":
        t_policy, s2_policy, activation_policy, split_policy = _upstream(args)
        row = activate_release(
            session,
            policy=policy,
            t_policy=t_policy,
            s2_policy=s2_policy,
            activation_policy=activation_policy,
            split_policy=split_policy,
            release_candidate_id=args.release_candidate_id,
            approval_id=args.approval_id,
            activated_at=args.activated_at,
        )
        return {"status": "active", "deployment_id": str(row.id), "subject": "challenger"}
    if command == "rollback-release":
        row = rollback_release(
            session,
            policy=policy,
            reason_code=args.reason_code,
            health_evidence=_json_object(args.health_evidence_json),
            rolled_back_at=args.rolled_back_at,
        )
        return {"status": "rolled_back", "rollback_id": str(row.id)}
    if command == "show-active-release":
        pointer, deployment, candidate = resolve_active_release(session, policy=policy)
        return {
            "status": "active",
            "subject": pointer.subject_code,
            "deployment_id": str(deployment.id),
            "release_candidate_id": str(candidate.id),
            "scoring_definition_checksum_sha256": (pointer.scoring_definition_checksum_sha256),
        }
    if command == "release-history":
        return {"status": "ready", "deployments": release_history(session, policy=policy)}
    raise ValueError("unsupported model release command")


def _upstream(args: argparse.Namespace):
    root = Path(__file__).resolve().parents[3]
    return (
        load_multibagger_temporal_holdout_policy(args.t_policy),
        load_multibagger_challenger_development_policy(args.s2_policy),
        load_historical_evaluation_activation_policy(args.activation_policy, repository_root=root),
        load_multibagger_temporal_split_policy(args.split_policy),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        with sessionmaker(bind=engine, autoflush=False)() as session:
            head = session.scalar(text("SELECT version_num FROM alembic_version"))
            if head != LATEST_MIGRATION:
                raise ValueError(
                    f"database migration head must be {LATEST_MIGRATION}; found {head!r}"
                )
            payload = _execute(args, session)
        print(json.dumps(payload, default=_json_default, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("health evidence must be a JSON object")
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


if __name__ == "__main__":
    sys.exit(main())
