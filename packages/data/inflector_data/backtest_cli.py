"""CLI for strict PIT snapshot, forward-outcome, and descriptive analysis stages."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from inflector_data.backtest_analysis import summarize_backtest
from inflector_data.backtest_outcomes import build_forward_outcomes
from inflector_data.backtest_policy import (
    load_backtest_policy,
    load_historical_availability_manifest,
)
from inflector_data.historical_dataset import (
    build_historical_dataset,
    normalize_symbols,
)
from inflector_data.research_profile import load_research_profile

LATEST_MIGRATION = "20261003_0020"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strict knowledge-time V5 backtesting")
    commands = parser.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("build-dataset")
    _database_policy_arguments(dataset)
    dataset.add_argument("--availability-manifest", type=Path, required=True)
    dataset.add_argument("--research-profile", type=Path, required=True)
    dataset.add_argument("--model-family", required=True)
    dataset.add_argument("--symbols-file", type=Path, required=True)
    dataset.add_argument("--max-symbols", type=int, default=25)
    dataset.add_argument("--from-cutoff", type=datetime.fromisoformat, required=True)
    dataset.add_argument("--to-cutoff", type=datetime.fromisoformat, required=True)

    outcomes = commands.add_parser("build-outcomes")
    _database_policy_arguments(outcomes)
    outcomes.add_argument("--research-profile", type=Path, required=True)
    outcomes.add_argument("--run-id", type=UUID, required=True)
    outcomes.add_argument("--outcome-data-cutoff", type=datetime.fromisoformat, required=True)

    summary = commands.add_parser("summarize")
    _database_policy_arguments(summary)
    summary.add_argument("--run-id", type=UUID, required=True)
    summary.add_argument("--export-csv", type=Path)
    return parser


def _database_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backtest-policy", type=Path, required=True)


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    _preflight(session)
    root = Path(__file__).resolve().parents[3]
    policy = load_backtest_policy(args.backtest_policy, repository_root=root)
    if args.command == "build-dataset":
        manifest = load_historical_availability_manifest(args.availability_manifest)
        profile = load_research_profile(args.research_profile)
        if args.max_symbols < 1 or args.max_symbols > policy.maximum_symbols:
            raise ValueError(f"max-symbols must be between 1 and {policy.maximum_symbols}")
        symbols = normalize_symbols(
            args.symbols_file.read_text(encoding="utf-8-sig").splitlines(),
            maximum=args.max_symbols,
        )
        result = build_historical_dataset(
            session,
            policy=policy,
            manifest=manifest,
            research_profile=profile,
            model_family=args.model_family,
            symbols=symbols,
            cutoff_start=args.from_cutoff,
            cutoff_end=args.to_cutoff,
            repository_root=root,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "build-outcomes":
        profile = load_research_profile(args.research_profile)
        result = build_forward_outcomes(
            session,
            run_id=args.run_id,
            policy=policy,
            research_profile=profile,
            outcome_data_cutoff=args.outcome_data_cutoff,
        )
        return {"status": "completed", **asdict(result)}
    result = summarize_backtest(
        session,
        run_id=args.run_id,
        policy=policy,
        export_csv=args.export_csv,
    )
    return {
        "status": "completed",
        "run_id": result.run_id,
        "summary": result.summary,
        "export_csv": result.export_path,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            payload = _execute(args, session)
        print(json.dumps(payload, default=_json_default, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _preflight(session: Session) -> None:
    head = session.scalar(text("SELECT version_num FROM alembic_version"))
    if head != LATEST_MIGRATION:
        raise ValueError(f"database migration head must be {LATEST_MIGRATION}; found {head!r}")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


if __name__ == "__main__":
    sys.exit(main())
