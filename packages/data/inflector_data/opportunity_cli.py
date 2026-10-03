"""CLI for deterministic persisted V5 opportunity discovery and export."""

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

from inflector_data.opportunity_discovery import (
    build_opportunity_discovery,
    normalize_discovery_symbols,
)
from inflector_data.opportunity_policy import load_opportunity_discovery_policy
from inflector_data.opportunity_summary import summarize_opportunity_discovery
from inflector_data.research_profile import load_research_profile

LATEST_MIGRATION = "20261003_0022"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Current V5 opportunity discovery")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-ranking")
    _database_policy_arguments(build)
    build.add_argument("--research-profile", type=Path, required=True)
    build.add_argument("--model-family", required=True)
    build.add_argument("--discovery-cutoff", type=datetime.fromisoformat, required=True)
    build.add_argument("--symbols-file", type=Path, required=True)
    build.add_argument("--max-symbols", type=int, default=5000)

    summary = commands.add_parser("summarize")
    _database_policy_arguments(summary)
    summary.add_argument("--run-id", type=UUID, required=True)
    summary.add_argument("--limit", type=int)
    summary.add_argument("--export-csv", type=Path)
    return parser


def _database_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--discovery-policy", type=Path, required=True)


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    _preflight(session)
    root = Path(__file__).resolve().parents[3]
    policy = load_opportunity_discovery_policy(args.discovery_policy, repository_root=root)
    if args.command == "build-ranking":
        if args.max_symbols < 1 or args.max_symbols > policy.maximum_symbols:
            raise ValueError(f"max-symbols must be between 1 and {policy.maximum_symbols}")
        symbols = normalize_discovery_symbols(
            args.symbols_file.read_text(encoding="utf-8-sig").splitlines(),
            maximum=args.max_symbols,
        )
        profile = load_research_profile(args.research_profile)
        result = build_opportunity_discovery(
            session,
            policy=policy,
            research_profile=profile,
            model_family=args.model_family,
            symbols=symbols,
            discovery_cutoff=args.discovery_cutoff,
            repository_root=root,
        )
        return {"status": "completed", **asdict(result)}
    result = summarize_opportunity_discovery(
        session,
        run_id=args.run_id,
        policy=policy,
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
