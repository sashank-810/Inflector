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

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from inflector_data.archive import LocalRawObjectStore
from inflector_data.backtest_analysis import summarize_backtest
from inflector_data.backtest_outcomes import build_forward_outcomes
from inflector_data.backtest_policy import (
    load_backtest_policy,
    load_historical_availability_manifest,
)
from inflector_data.historical_activation_policy import (
    load_historical_evaluation_activation_policy,
    load_multibagger_error_analysis_policy,
    load_multibagger_temporal_split_policy,
)
from inflector_data.historical_dataset import (
    build_historical_dataset,
    normalize_symbols,
)
from inflector_data.historical_evaluation_activation import (
    build_multibagger_error_analysis,
    build_multibagger_temporal_split,
    historical_evaluation_readiness,
    verify_historical_session_calendar,
)
from inflector_data.historical_evaluation_policy import (
    load_historical_universe_policy,
    load_multibagger_evaluation_policy,
    load_multibagger_outcome_policy,
)
from inflector_data.historical_universe import (
    HistoricalUniverseEvidence,
    build_historical_universe,
    historical_universe_summary,
    shard_historical_universe,
)
from inflector_data.multibagger_evaluation import (
    build_multibagger_evaluation,
    inspect_multibagger_errors,
    load_evaluation_manifest,
    summarize_multibagger_evaluation,
)
from inflector_data.multibagger_labels import (
    build_multibagger_labels,
    multibagger_label_summary,
)
from inflector_data.nse_historical_mii import evidence_for_ingestion
from inflector_data.research_profile import load_research_profile
from inflector_database.historical_evaluation_repository import (
    HistoricalEvaluationRepository,
)
from inflector_database.models import (
    IngestionRun,
    MultibaggerErrorAnalysisRow,
    MultibaggerErrorAnalysisRun,
)

LATEST_MIGRATION = "20261006_0025"


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

    evaluation = commands.add_parser("build-multibagger-evaluation")
    evaluation.add_argument("--database-url", required=True)
    evaluation.add_argument("--evaluation-policy", type=Path, required=True)
    evaluation.add_argument("--cohort-manifest", type=Path, required=True)
    evaluation.add_argument("--completed-at", type=datetime.fromisoformat, required=True)

    evaluation_summary = commands.add_parser("summarize-multibagger-evaluation")
    evaluation_summary.add_argument("--database-url", required=True)
    evaluation_summary.add_argument("--evaluation-policy", type=Path, required=True)
    evaluation_summary.add_argument("--run-id", type=UUID, required=True)

    errors = commands.add_parser("inspect-multibagger-errors")
    errors.add_argument("--database-url", required=True)
    errors.add_argument("--evaluation-policy", type=Path, required=True)
    errors.add_argument("--run-id", type=UUID, required=True)
    errors.add_argument("--class", dest="error_class", choices=("fp", "fn"))

    activation = commands.add_parser("activate-historical-universe")
    activation.add_argument("--database-url", required=True)
    activation.add_argument("--raw-root", type=Path, required=True)
    activation.add_argument("--activation-policy", type=Path, required=True)
    activation.add_argument("--universe-policy", type=Path, required=True)
    activation.add_argument("--source-ingestion-run-id", type=UUID, required=True)
    activation.add_argument("--cutoff", type=datetime.fromisoformat, required=True)
    activation.add_argument("--completed-at", type=datetime.fromisoformat, required=True)

    calendar = commands.add_parser("verify-historical-session-calendar")
    calendar.add_argument("--database-url", required=True)
    calendar.add_argument("--activation-policy", type=Path, required=True)
    calendar.add_argument("--session-artifact-acquisition-id", type=UUID, required=True)
    calendar.add_argument("--benchmark-dataset-id", type=UUID, required=True)
    calendar.add_argument("--from-date", type=date.fromisoformat, required=True)
    calendar.add_argument("--to-date", type=date.fromisoformat, required=True)
    calendar.add_argument("--raw-root", type=Path, required=True)
    calendar.add_argument("--source-cutoff", type=datetime.fromisoformat, required=True)

    readiness = commands.add_parser("historical-evaluation-readiness")
    readiness.add_argument("--database-url", required=True)
    readiness.add_argument("--activation-policy", type=Path, required=True)
    readiness.add_argument("--split-policy", type=Path, required=True)
    readiness.add_argument("--evaluation-run-id", type=UUID, required=True)
    readiness.add_argument("--session-calendar-run-id", type=UUID, required=True)

    split = commands.add_parser("build-multibagger-temporal-split")
    split.add_argument("--database-url", required=True)
    split.add_argument("--split-policy", type=Path, required=True)
    split.add_argument("--evaluation-run-id", type=UUID, required=True)
    split.add_argument("--session-calendar-run-id", type=UUID, required=True)
    split.add_argument("--completed-at", type=datetime.fromisoformat, required=True)

    analysis = commands.add_parser("build-multibagger-error-analysis")
    analysis.add_argument("--database-url", required=True)
    analysis.add_argument("--error-analysis-policy", type=Path, required=True)
    analysis.add_argument("--temporal-split-run-id", type=UUID, required=True)
    analysis.add_argument("--completed-at", type=datetime.fromisoformat, required=True)

    analysis_summary = commands.add_parser("summarize-multibagger-error-analysis")
    analysis_summary.add_argument("--database-url", required=True)
    analysis_summary.add_argument("--error-analysis-policy", type=Path, required=True)
    analysis_summary.add_argument("--run-id", type=UUID, required=True)

    inspect_analysis = commands.add_parser("inspect-multibagger-error-cohort")
    inspect_analysis.add_argument("--database-url", required=True)
    inspect_analysis.add_argument("--error-analysis-policy", type=Path, required=True)
    inspect_analysis.add_argument("--run-id", type=UUID, required=True)
    inspect_analysis.add_argument("--split", choices=("development", "validation"))
    inspect_analysis.add_argument("--class", dest="error_class", choices=("fp", "fn"))
    return parser


def _database_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--backtest-policy", type=Path, required=True)


def _execute(args: argparse.Namespace, session: Session) -> dict[str, object]:
    _preflight(session)
    root = Path(__file__).resolve().parents[3]
    if args.command == "activate-historical-universe":
        activation_policy = load_historical_evaluation_activation_policy(
            args.activation_policy, repository_root=root
        )
        universe_policy = load_historical_universe_policy(args.universe_policy)
        ingestion = session.get(IngestionRun, args.source_ingestion_run_id)
        if ingestion is None or ingestion.status != "completed":
            raise ValueError("completed historical MII ingestion run is unavailable")
        evidence = evidence_for_ingestion(
            session,
            archive=LocalRawObjectStore(args.raw_root),
            ingestion_run_id=ingestion.id,
            maximum_decompressed_bytes=activation_policy.maximum_mii_decompressed_bytes,
        )
        result = build_historical_universe(
            session,
            policy=universe_policy,
            cutoff=args.cutoff,
            source_provider_dataset_id=ingestion.provider_dataset_id,
            source_ingestion_run_id=ingestion.id,
            evidence=evidence,
            completed_at=args.completed_at,
        )
        run = HistoricalEvaluationRepository(session).get_universe_run(result.run_id)
        assert run is not None
        shards = shard_historical_universe(tuple(run.members), policy=universe_policy)
        return {
            "status": "completed",
            **asdict(result),
            "activation_policy_checksum_sha256": activation_policy.checksum_sha256,
            "shards": [asdict(item) for item in shards],
        }
    if args.command == "verify-historical-session-calendar":
        policy = load_historical_evaluation_activation_policy(
            args.activation_policy, repository_root=root
        )
        result = verify_historical_session_calendar(
            session,
            policy=policy,
            source_artifact_acquisition_id=args.session_artifact_acquisition_id,
            archive=LocalRawObjectStore(args.raw_root),
            benchmark_provider_dataset_id=args.benchmark_dataset_id,
            requested_from_date=args.from_date,
            requested_to_date=args.to_date,
            source_cutoff=args.source_cutoff,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "historical-evaluation-readiness":
        policy = load_historical_evaluation_activation_policy(
            args.activation_policy, repository_root=root
        )
        return historical_evaluation_readiness(
            session,
            policy=policy,
            split_policy=load_multibagger_temporal_split_policy(args.split_policy),
            source_evaluation_run_id=args.evaluation_run_id,
            session_calendar_run_id=args.session_calendar_run_id,
        )
    if args.command == "build-multibagger-temporal-split":
        policy = load_multibagger_temporal_split_policy(args.split_policy)
        result = build_multibagger_temporal_split(
            session,
            policy=policy,
            source_evaluation_run_id=args.evaluation_run_id,
            session_calendar_run_id=args.session_calendar_run_id,
            completed_at=args.completed_at,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "build-multibagger-error-analysis":
        policy = load_multibagger_error_analysis_policy(args.error_analysis_policy)
        result = build_multibagger_error_analysis(
            session,
            policy=policy,
            temporal_split_run_id=args.temporal_split_run_id,
            completed_at=args.completed_at,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "summarize-multibagger-error-analysis":
        policy = load_multibagger_error_analysis_policy(args.error_analysis_policy)
        run = session.get(MultibaggerErrorAnalysisRun, args.run_id)
        if (
            run is None
            or run.status != "completed"
            or run.error_policy_checksum_sha256 != policy.checksum_sha256
        ):
            raise ValueError("completed error analysis run binding is unavailable")
        return {
            "status": "completed",
            "run_id": str(run.id),
            "run_key_sha256": run.run_key_sha256,
            "summary": run.summary_json,
        }
    if args.command == "inspect-multibagger-error-cohort":
        policy = load_multibagger_error_analysis_policy(args.error_analysis_policy)
        run = session.get(MultibaggerErrorAnalysisRun, args.run_id)
        if run is None or run.error_policy_checksum_sha256 != policy.checksum_sha256:
            raise ValueError("error analysis run binding is unavailable")
        statement = select(MultibaggerErrorAnalysisRow).where(
            MultibaggerErrorAnalysisRow.multibagger_error_analysis_run_id == run.id
        )
        if args.split is not None:
            statement = statement.where(MultibaggerErrorAnalysisRow.split_code == args.split)
        if args.error_class is not None:
            statement = statement.where(
                MultibaggerErrorAnalysisRow.confusion_class == args.error_class
            )
        rows = tuple(
            session.scalars(
                statement.order_by(
                    MultibaggerErrorAnalysisRow.historical_cutoff,
                    MultibaggerErrorAnalysisRow.historical_symbol,
                    MultibaggerErrorAnalysisRow.outcome_contract_code,
                    MultibaggerErrorAnalysisRow.selection_contract_code,
                    MultibaggerErrorAnalysisRow.evaluation_view,
                )
            )
        )
        return {
            "status": "completed",
            "errors": [
                {
                    "split": item.split_code,
                    "cutoff": item.historical_cutoff,
                    "symbol": item.historical_symbol,
                    "outcome_contract": item.outcome_contract_code,
                    "selection_contract": item.selection_contract_code,
                    "evaluation_view": item.evaluation_view,
                    "confusion_class": item.confusion_class,
                    "rankable": item.rankable,
                    "unranked_reason": item.unranked_reason,
                    "final_score": item.final_score,
                    "display_order": item.display_order,
                    "rank_positions_below_boundary": item.rank_positions_below_boundary,
                    "score_gap_to_boundary": item.score_gap_to_boundary,
                    "endpoint_return": item.endpoint_return,
                    "peak_price_multiple": item.peak_price_multiple,
                    "maximum_drawdown": item.maximum_drawdown,
                    "error_signature": item.error_signature,
                }
                for item in rows
            ],
        }
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
    if args.command == "build-multibagger-evaluation":
        policy = load_multibagger_evaluation_policy(args.evaluation_policy, repository_root=root)
        result = build_multibagger_evaluation(
            session,
            policy=policy,
            bundles=load_evaluation_manifest(args.cohort_manifest),
            completed_at=args.completed_at,
        )
        return {"status": "completed", **asdict(result)}
    if args.command == "summarize-multibagger-evaluation":
        policy = load_multibagger_evaluation_policy(args.evaluation_policy, repository_root=root)
        result = summarize_multibagger_evaluation(session, run_id=args.run_id)
        return {"status": "completed", "policy_checksum_sha256": policy.checksum_sha256, **result}
    if args.command == "inspect-multibagger-errors":
        policy = load_multibagger_evaluation_policy(args.evaluation_policy, repository_root=root)
        result = inspect_multibagger_errors(
            session, run_id=args.run_id, confusion_class=args.error_class
        )
        return {
            "status": "completed",
            "policy_checksum_sha256": policy.checksum_sha256,
            "errors": result,
        }
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
