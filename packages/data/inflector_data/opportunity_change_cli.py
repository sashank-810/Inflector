"""CLI for deterministic comparison of completed Production K discovery runs."""

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

from inflector_data.opportunity_change import (
    IncompatibleDiscoveryRunsError,
    build_opportunity_change,
)
from inflector_data.opportunity_change_policy import load_opportunity_change_policy
from inflector_data.opportunity_change_summary import summarize_opportunity_change

LATEST_MIGRATION = "20261003_0020"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Production K research-state comparison")
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare")
    _common(compare)
    compare.add_argument("--baseline-run-id", type=UUID, required=True)
    compare.add_argument("--current-run-id", type=UUID, required=True)
    summary = commands.add_parser("summarize")
    _common(summary)
    summary.add_argument("--change-run-id", type=UUID, required=True)
    summary.add_argument("--changed-only", action="store_true")
    summary.add_argument("--change-code")
    summary.add_argument("--limit", type=int)
    summary.add_argument("--export-csv", type=Path)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--change-policy", type=Path, required=True)


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    _preflight(session)
    root = Path(__file__).resolve().parents[3]
    policy = load_opportunity_change_policy(args.change_policy, repository_root=root)
    if args.command == "compare":
        result = build_opportunity_change(
            session,
            policy=policy,
            baseline_run_id=args.baseline_run_id,
            current_run_id=args.current_run_id,
        )
        return {"status": "completed", **asdict(result)}
    result = summarize_opportunity_change(
        session,
        run_id=args.change_run_id,
        policy=policy,
        changed_only=args.changed_only,
        change_code=args.change_code,
        limit=args.limit,
        export_csv=args.export_csv,
    )
    return {
        "status": "completed",
        "run_id": result.run_id,
        "summary": result.summary,
        "items": result.items,
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
    except IncompatibleDiscoveryRunsError as error:
        print(json.dumps({"status": error.status, "error": str(error)}, sort_keys=True))
        return 1
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
