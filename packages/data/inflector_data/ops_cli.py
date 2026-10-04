"""Production operations CLI for planned, leased, resumable current-research cycles."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from inflector_data.operations_profile import load_operations_profile
from inflector_data.production_health import production_health
from inflector_data.production_operations import (
    AcceptedProductionStageExecutor,
    CycleInputs,
    CyclePlan,
    ProductionCycleService,
    cycle_inputs_from_persisted,
    cycle_summary,
    doctor,
    json_safe,
    normalize_symbols,
    plan_cycle,
)
from inflector_data.research_profile import load_research_profile
from inflector_database.base import Base
from inflector_database.models import OperationalRun
from inflector_database.operations_repository import OperationsRepository

LATEST_MIGRATION = "20261003_0022"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inflector production operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="read-only production preflight")
    _cycle_arguments(doctor_parser, automatic=False)

    doctor_auto = subparsers.add_parser(
        "doctor-auto", help="read-only automatic-endpoint production preflight"
    )
    _cycle_arguments(doctor_auto, automatic=True)

    run_parser = subparsers.add_parser("run-cycle", help="execute one explicit production cycle")
    _cycle_arguments(run_parser, automatic=False)

    run_auto = subparsers.add_parser(
        "run-cycle-auto", help="execute one per-company automatic-endpoint cycle"
    )
    _cycle_arguments(run_auto, automatic=True)

    resume = subparsers.add_parser("resume-run", help="explicitly resume one failed/stale run")
    resume.add_argument("--database-url", required=True)
    resume.add_argument("--run-id", type=UUID, required=True)

    status = subparsers.add_parser("status", help="inspect operational run state")
    status.add_argument("--database-url", required=True)
    target = status.add_mutually_exclusive_group()
    target.add_argument("--run-id", type=UUID)
    target.add_argument("--last", type=int)

    health = subparsers.add_parser(
        "health", help="read-only accepted-pipeline readiness and operational health"
    )
    health.add_argument("--database-url")
    health.add_argument(
        "--repository-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    health.add_argument("--observed-at", type=datetime.fromisoformat, required=True)
    health.add_argument("--raw-root", type=Path)
    health.add_argument("--gdelt-raw-root", type=Path)
    health.add_argument("--model-family")

    subparsers.add_parser("burn-in", help="run a deterministic no-network ledger burn-in")
    return parser


def _cycle_arguments(parser: argparse.ArgumentParser, *, automatic: bool) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--operations-profile", type=Path, required=True)
    parser.add_argument("--research-profile", type=Path, required=True)
    parser.add_argument("--model-family", required=True)
    parser.add_argument("--symbols-file", type=Path, required=True)
    if not automatic:
        parser.add_argument("--fiscal-year", type=int, required=True)
        parser.add_argument("--fiscal-quarter", type=int, choices=(1, 2, 3, 4), required=True)
    parser.add_argument("--cycle-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--nse-license-class", required=True)
    parser.add_argument("--gdelt-raw-root", type=Path)
    parser.add_argument("--gdelt-license-class")
    parser.add_argument("--model-semantic-version", required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--model-effective-from", type=datetime.fromisoformat, required=True)


def _plan(args: argparse.Namespace) -> CyclePlan:
    operations_profile = load_operations_profile(args.operations_profile)
    research_profile = load_research_profile(args.research_profile)
    symbols = normalize_symbols(
        args.symbols_file, maximum=operations_profile.maximum_symbols_per_cycle
    )
    return plan_cycle(
        CycleInputs(
            operations_profile_path=args.operations_profile,
            research_profile_path=args.research_profile,
            model_family=args.model_family,
            symbols_file=args.symbols_file,
            symbols=symbols,
            fiscal_year=getattr(args, "fiscal_year", None),
            fiscal_quarter=getattr(args, "fiscal_quarter", None),
            cycle_at=args.cycle_at,
            raw_root=args.raw_root,
            nse_license_class=args.nse_license_class,
            gdelt_raw_root=args.gdelt_raw_root,
            gdelt_license_class=args.gdelt_license_class,
            model_semantic_version=args.model_semantic_version,
            git_sha=args.git_sha,
            model_effective_from=args.model_effective_from,
        ),
        operations_profile,
        research_profile,
    )


def _resume_plan(run: OperationalRun) -> CyclePlan:
    inputs = cycle_inputs_from_persisted(run.inputs_json)
    return plan_cycle(
        inputs,
        load_operations_profile(inputs.operations_profile_path),
        load_research_profile(inputs.research_profile_path),
    )


def _execute(args: argparse.Namespace, session: Session) -> tuple[dict[str, object], int]:
    if args.command in {"doctor", "doctor-auto", "run-cycle", "run-cycle-auto"}:
        plan = _plan(args)
        if args.command in {"doctor", "doctor-auto"}:
            return doctor(session, plan=plan), 0
        return ProductionCycleService(
            session,
            AcceptedProductionStageExecutor(),
            clock=lambda: datetime.now(UTC),
        ).run(plan, owner_token=str(uuid4()))

    if args.command == "resume-run":
        run = OperationsRepository(session).get_run(args.run_id)
        if run is None:
            raise ValueError("operational run not found")
        plan = _resume_plan(run)
        return ProductionCycleService(
            session,
            AcceptedProductionStageExecutor(),
            clock=lambda: datetime.now(UTC),
        ).resume(run.id, plan, owner_token=str(uuid4()))

    repository = OperationsRepository(session)
    now = datetime.now(UTC)
    if args.run_id is not None:
        run = repository.get_run(args.run_id)
        if run is None:
            raise ValueError("operational run not found")
        return {"status": "completed", "run": cycle_summary(run, at=now)}, 0
    limit = args.last if args.last is not None else 1
    if limit < 1 or limit > 100:
        raise ValueError("--last must be between 1 and 100")
    runs = repository.recent_runs(limit)
    return {
        "status": "completed",
        "runs": [cycle_summary(run, at=now) for run in runs],
    }, 0


class _BurnInExecutor:
    def execute(
        self, stage: str, *, session: Session, plan: CyclePlan
    ) -> tuple[dict[str, object], bool]:
        del session
        if stage == "current_research":
            return {
                "status": "completed",
                "results": [
                    {
                        "status": "completed",
                        "symbol": symbol,
                        "snapshot_id": None,
                        "snapshot_status": "partial_component_set",
                        "final_score": None,
                    }
                    for symbol in plan.inputs.symbols
                ],
            }, True
        return {"status": "completed", "fixture": "production-shaped-no-network"}, True


def _burn_in() -> tuple[dict[str, object], int]:
    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="inflector-ops-burn-in-") as directory:
        workspace = Path(directory)
        symbols_file = workspace / "symbols.txt"
        symbols_file.write_text("AAA\nBBB\n", encoding="utf-8")
        raw_root = workspace / "raw"
        gdelt_root = workspace / "gdelt"
        raw_root.mkdir()
        gdelt_root.mkdir()
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:version)"),
                {"version": LATEST_MIGRATION},
            )
        factory = sessionmaker(bind=engine, autoflush=False)
        cycle_at = datetime(2026, 10, 2, 14, 30, tzinfo=UTC)
        operations_path = root / "config/operations/production_operations_v1.json"
        research_path = root / "config/research/production_research_v1.json"
        profile = load_operations_profile(operations_path)
        plan = plan_cycle(
            CycleInputs(
                operations_profile_path=operations_path,
                research_profile_path=research_path,
                model_family="fictional_burn_in_v1",
                symbols_file=symbols_file,
                symbols=("AAA", "BBB"),
                fiscal_year=2025,
                fiscal_quarter=4,
                cycle_at=cycle_at,
                raw_root=raw_root,
                nse_license_class="fictional-reviewed-source-terms",
                gdelt_raw_root=gdelt_root,
                gdelt_license_class="fictional-public-api-terms",
                model_semantic_version="1.0.0",
                git_sha="0" * 40,
                model_effective_from=cycle_at,
            ),
            profile,
            load_research_profile(research_path),
        )
        ticks = iter(cycle_at + timedelta(seconds=value) for value in range(100))
        with factory() as session:
            service = ProductionCycleService(session, _BurnInExecutor(), clock=lambda: next(ticks))
            first, first_code = service.run(plan, owner_token="burn-in-owner-1")
            repeated, repeat_code = service.run(plan, owner_token="burn-in-owner-2")
        engine.dispose()
    return {
        "status": "completed" if first_code == 0 and repeat_code == 0 else "failed",
        "network_used": False,
        "first_run": first,
        "exact_rerun": repeated,
    }, 0 if first_code == 0 and repeat_code == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "burn-in":
        payload, exit_code = _burn_in()
        print(json.dumps(json_safe(payload), sort_keys=True))
        return exit_code
    if args.command == "health":
        return _health(args)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            payload, exit_code = _execute(args, session)
        print(json.dumps(json_safe(payload), sort_keys=True))
        return exit_code
    except Exception as error:
        print(
            json.dumps(
                {"status": "failed", "error_code": type(error).__name__, "error": str(error)},
                sort_keys=True,
            )
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _health(args: argparse.Namespace) -> int:
    database_url = args.database_url or os.getenv("INFLECTOR_PRODUCTION_DATABASE_URL")
    raw_root_value = args.raw_root or _optional_path("INFLECTOR_PRODUCTION_RAW_ROOT")
    gdelt_root_value = args.gdelt_raw_root or _optional_path("INFLECTOR_GDELT_RAW_ROOT")
    model_family = args.model_family or os.getenv("INFLECTOR_MODEL_FAMILY")
    engine: Engine | None = None
    try:
        if database_url is None:
            payload = production_health(
                repository_root=args.repository_root,
                observed_at=args.observed_at,
                session=None,
                raw_root=raw_root_value,
                gdelt_raw_root=gdelt_root_value,
                model_family=model_family,
                telegram_bot_token_present=bool(
                    os.getenv("INFLECTOR_TELEGRAM_BOT_TOKEN")
                ),
                telegram_chat_id_present=bool(os.getenv("INFLECTOR_TELEGRAM_CHAT_ID")),
            )
        else:
            engine = create_engine(database_url, pool_pre_ping=True)
            factory = sessionmaker(bind=engine, autoflush=False)
            with factory() as session:
                payload = production_health(
                    repository_root=args.repository_root,
                    observed_at=args.observed_at,
                    session=session,
                    raw_root=raw_root_value,
                    gdelt_raw_root=gdelt_root_value,
                    model_family=model_family,
                    telegram_bot_token_present=bool(
                        os.getenv("INFLECTOR_TELEGRAM_BOT_TOKEN")
                    ),
                    telegram_chat_id_present=bool(
                        os.getenv("INFLECTOR_TELEGRAM_CHAT_ID")
                    ),
                )
    except Exception as error:
        payload = {
            "status": "broken",
            "read_only": True,
            "error_code": type(error).__name__,
        }
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps(json_safe(payload), sort_keys=True))
    return {"ready": 0, "configuration_unavailable": 2}.get(str(payload["status"]), 1)


def _optional_path(environment_name: str) -> Path | None:
    value = os.getenv(environment_name)
    return Path(value) if value else None


if __name__ == "__main__":
    sys.exit(main())
