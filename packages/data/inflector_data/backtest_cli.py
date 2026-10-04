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
from inflector_data.historical_evaluation_policy import (
    load_historical_universe_policy,
    load_multibagger_outcome_policy,
)
from inflector_data.historical_universe import (
    HistoricalUniverseEvidence,
    build_historical_universe,
    historical_universe_summary,
)
from inflector_data.multibagger_labels import (
    build_multibagger_labels,
    multibagger_label_summary,
)
from inflector_data.research_profile import load_research_profile
from inflector_database.historical_evaluation_repository import (
    HistoricalEvaluationRepository,
)

LATEST_MIGRATION = "20261004_0023"


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

    universe = commands.add_parser("build-historical-universe")
    universe.add_argument("--database-url", required=True)
    universe.add_argument("--universe-policy", type=Path, required=True)
    universe.add_argument("--source-dataset-id", type=UUID, required=True)
    universe.add_argument("--source-ingestion-run-id", type=UUID, required=True)
    universe.add_argument("--cutoff", type=datetime.fromisoformat, required=True)
    universe.add_argument("--completed-at", type=datetime.fromisoformat, required=True)
    universe.add_argument("--evidence-json", type=Path, required=True)

    inspect_universe = commands.add_parser("inspect-historical-universe")
    inspect_universe.add_argument("--database-url", required=True)
    inspect_universe.add_argument("--universe-policy", type=Path, required=True)
    inspect_universe.add_argument("--run-id", type=UUID, required=True)

    labels = commands.add_parser("build-multibagger-labels")
    labels.add_argument("--database-url", required=True)
    labels.add_argument("--multibagger-policy", type=Path, required=True)
    labels.add_argument("--research-profile", type=Path, required=True)
    labels.add_argument("--backtest-run-id", type=UUID, required=True)
    labels.add_argument("--outcome-data-cutoff", type=datetime.fromisoformat, required=True)

    label_summary = commands.add_parser("summarize-multibagger-labels")
    label_summary.add_argument("--database-url", required=True)
    label_summary.add_argument("--multibagger-policy", type=Path, required=True)
    label_summary.add_argument("--run-id", type=UUID, required=True)
    label_summary.add_argument("--contract")
    return parser


def _database_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backtest-policy", type=Path, required=True)


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    _preflight(session)
    root = Path(__file__).resolve().parents[3]
    if args.command == "build-historical-universe":
        policy = load_historical_universe_policy(args.universe_policy)
        result = build_historical_universe(
            session,
            policy=policy,
            cutoff=args.cutoff,
            source_provider_dataset_id=args.source_dataset_id,
            source_ingestion_run_id=args.source_ingestion_run_id,
            evidence=_load_universe_evidence(args.evidence_json),
            completed_at=args.completed_at,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "inspect-historical-universe":
        policy = load_historical_universe_policy(args.universe_policy)
        run = HistoricalEvaluationRepository(session).get_universe_run(args.run_id)
        if run is None:
            raise ValueError("historical universe run is unavailable")
        if run.status != "completed":
            raise ValueError("historical universe run is not completed")
        if run.universe_policy_checksum_sha256 != policy.checksum_sha256:
            raise ValueError("historical universe policy binding mismatch")
        return {
            "status": "completed",
            **historical_universe_summary(run, policy=policy),
        }
    if args.command == "build-multibagger-labels":
        policy = load_multibagger_outcome_policy(args.multibagger_policy, repository_root=root)
        result = build_multibagger_labels(
            session,
            backtest_run_id=args.backtest_run_id,
            policy=policy,
            research_profile=load_research_profile(args.research_profile),
            outcome_data_cutoff=args.outcome_data_cutoff,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "summarize-multibagger-labels":
        policy = load_multibagger_outcome_policy(args.multibagger_policy, repository_root=root)
        run = HistoricalEvaluationRepository(session).get_label_run(args.run_id)
        if run is None:
            raise ValueError("multibagger label run is unavailable")
        if run.status != "completed":
            raise ValueError("multibagger label run is not completed")
        if run.label_policy_checksum_sha256 != policy.checksum_sha256:
            raise ValueError("multibagger label policy binding mismatch")
        payload = multibagger_label_summary(run)
        if args.contract is not None:
            valid = {item.code for item in policy.contracts}
            if args.contract not in valid:
                raise ValueError("requested multibagger contract is unsupported")
            summary = dict(run.summary_json)
            raw_contracts = summary.get("contracts", {})
            if not isinstance(raw_contracts, dict):
                raise ValueError("multibagger label summary contracts are malformed")
            contracts = {str(key): value for key, value in raw_contracts.items()}
            summary["contracts"] = {args.contract: contracts.get(args.contract)}
            payload["summary"] = summary
        return {"status": "completed", **payload}
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


def _load_universe_evidence(path: Path) -> tuple[HistoricalUniverseEvidence, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("historical universe evidence projection is not valid JSON") from error
    if not isinstance(value, list):
        raise ValueError("historical universe evidence projection must be an array")
    result: list[HistoricalUniverseEvidence] = []
    expected = {"source_record_id", "semantic_row"}
    for item in value:
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("historical universe evidence row is malformed")
        semantic_row = item["semantic_row"]
        if not isinstance(semantic_row, dict) or any(
            not isinstance(key, str) or not isinstance(raw, str)
            for key, raw in semantic_row.items()
        ):
            raise ValueError("historical universe semantic source row is malformed")
        try:
            result.append(
                HistoricalUniverseEvidence(
                    source_record_id=UUID(str(item["source_record_id"])),
                    semantic_row=dict(semantic_row),
                )
            )
        except (ValueError, TypeError) as error:
            raise ValueError("historical universe evidence row values are invalid") from error
    return tuple(result)


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
