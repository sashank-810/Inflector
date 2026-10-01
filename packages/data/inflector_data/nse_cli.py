"""Explicit production CLI for official NSE archive-first ingestion."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.nse_http import (
    NSEAcquisitionError,
    NSEHttpClient,
    NSESourceNotAvailableError,
)
from inflector_data.nse_providers import (
    NSE_BENCHMARK_DATASET_CODE,
    NSE_EQUITY_UNIVERSE_URL,
    NSE_MARKET_DATASET_CODE,
    NSE_UNIVERSE_DATASET_CODE,
    HttpNSEArtifactSource,
    LocalNSEArtifactSource,
    NSEBenchmarkDataProvider,
    NSEMarketDataProvider,
    NSEUniverseProvider,
    nse_benchmark_url,
    nse_market_url,
)
from inflector_data.service import IngestionResult, IngestionService
from inflector_database.models import Company, DataProvider, ProviderDataset, Security

NSE_PROVIDER_CODE = "nse_official"
SYNTHETIC_LICENCE_MARKER = "synthetic-development-only"
SYNTHETIC_PROVIDER_CODE = "synthetic_csv"
SYNTHETIC_COMPANY_NAMES = frozenset(
    {
        "Aranya Engineering Limited",
        "Suryanet Components Limited",
        "Pragati Industrial Systems Limited",
    }
)
SYNTHETIC_ISINS = frozenset({"INE0ARA01018", "INE0SUR01015", "INE0PRA01012"})


class ProductionPreflightError(RuntimeError):
    """The explicitly supplied target is not a clean production database."""


class _ProductionProvider(Protocol):
    @property
    def skipped_rows(self) -> int: ...

    @property
    def source_uri(self) -> str | None: ...

    @property
    def retrieved_at(self) -> datetime | None: ...


def production_preflight(session: Session, licence_class: str) -> None:
    """Fail before mutation when development identities or metadata are present."""

    if not licence_class.strip():
        raise ProductionPreflightError("license class must be non-empty")
    synthetic_provider = session.scalar(
        select(DataProvider.id).where(
            (DataProvider.code == SYNTHETIC_PROVIDER_CODE)
            | (DataProvider.licence_name == SYNTHETIC_LICENCE_MARKER)
        )
    )
    synthetic_dataset = session.scalar(
        select(ProviderDataset.id).where(
            ProviderDataset.licence_class == SYNTHETIC_LICENCE_MARKER
        )
    )
    synthetic_company = session.scalar(
        select(Company.id).where(Company.legal_name.in_(SYNTHETIC_COMPANY_NAMES))
    )
    synthetic_security = session.scalar(
        select(Security.id).where(Security.isin.in_(SYNTHETIC_ISINS))
    )
    if any(
        value is not None
        for value in (
            synthetic_provider,
            synthetic_dataset,
            synthetic_company,
            synthetic_security,
        )
    ):
        raise ProductionPreflightError(
            "production ingestion refuses a database containing synthetic-development markers"
        )

    nse_provider = session.scalar(
        select(DataProvider).where(DataProvider.code == NSE_PROVIDER_CODE)
    )
    if nse_provider is not None:
        if nse_provider.licence_name != licence_class:
            raise ProductionPreflightError(
                "license class differs from existing nse_official provider metadata"
            )
        dataset_mismatch = session.scalar(
            select(ProviderDataset.id).where(
                ProviderDataset.provider_id == nse_provider.id,
                ProviderDataset.licence_class != licence_class,
            )
        )
        if dataset_mismatch is not None:
            raise ProductionPreflightError(
                "license class differs from an existing nse_official dataset"
            )


def _metadata(dataset_code: str, licence_class: str) -> ProviderMetadata:
    return ProviderMetadata(
        provider_code=NSE_PROVIDER_CODE,
        provider_type="https",
        dataset_code=dataset_code,
        licence_class=licence_class,
        licence_reference="Caller-supplied classification; official NSE source terms apply.",
        redistributable=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive-first official NSE production ingestion"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("ingest-universe", "ingest-market", "ingest-benchmarks", "ingest-daily"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--database-url", required=True)
        subparser.add_argument("--raw-root", type=Path, required=True)
        subparser.add_argument("--license-class", required=True)
        if command in {"ingest-market", "ingest-benchmarks", "ingest-daily"}:
            subparser.add_argument("--date", type=date.fromisoformat, required=True)
        if command == "ingest-universe":
            _local_arguments(subparser, "")
        elif command == "ingest-market":
            _local_arguments(subparser, "")
        elif command == "ingest-benchmarks":
            _local_arguments(subparser, "")
        else:
            for prefix in ("universe", "market", "benchmark"):
                _local_arguments(subparser, prefix)
    return parser


def _local_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    option_prefix = f"{prefix}-" if prefix else ""
    destination_prefix = f"{prefix}_" if prefix else ""
    parser.add_argument(
        f"--{option_prefix}file",
        dest=f"{destination_prefix}file",
        type=Path,
        help="already-downloaded official artifact",
    )
    parser.add_argument(
        f"--{option_prefix}source-uri",
        dest=f"{destination_prefix}source_uri",
        help="explicit official source URI for the local artifact",
    )


def _source(
    *,
    local_file: Path | None,
    source_uri: str | None,
    live_uri: str,
    client: NSEHttpClient,
) -> HttpNSEArtifactSource | LocalNSEArtifactSource:
    if local_file is None:
        if source_uri is not None:
            raise ValueError("--source-uri requires --file")
        return HttpNSEArtifactSource(client, live_uri)
    if source_uri is None or not source_uri.strip():
        raise ValueError("local official artifacts require an explicit --source-uri")
    return LocalNSEArtifactSource(local_file, source_uri)


def _result_summary(
    stage: str,
    provider: _ProductionProvider,
    result: IngestionResult,
) -> dict[str, object]:
    retrieved_at = provider.retrieved_at
    return {
        "stage": stage,
        "status": result.status,
        "source_uri": provider.source_uri,
        "retrieved_at": retrieved_at.isoformat() if retrieved_at is not None else None,
        "run_id": str(result.run_id),
        "records_received": result.records_received,
        "records_accepted": result.records_accepted,
        "records_quarantined": result.records_quarantined,
        "records_duplicated": result.records_duplicated,
        "provider_skipped_rows": provider.skipped_rows,
    }


def _failure_summary(stage: str, error: Exception) -> dict[str, object]:
    status = (
        "source_not_available"
        if isinstance(error, NSESourceNotAvailableError)
        else "failed"
    )
    return {
        "stage": stage,
        "status": status,
        "source_uri": None,
        "retrieved_at": None,
        "run_id": None,
        "records_received": 0,
        "records_accepted": 0,
        "records_quarantined": 0,
        "records_duplicated": 0,
        "provider_skipped_rows": 0,
        "error": str(error),
    }


def _run_stage(
    *,
    stage: str,
    provider: _ProductionProvider,
    ingest: Callable[[Any], IngestionResult],
) -> tuple[dict[str, object], bool]:
    try:
        result = ingest(provider)
        return _result_summary(stage, provider, result), True
    except (NSEAcquisitionError, OSError, ValueError, RuntimeError) as error:
        summary = _failure_summary(stage, error)
        summary["source_uri"] = provider.source_uri
        retrieved_at = provider.retrieved_at
        summary["retrieved_at"] = retrieved_at.isoformat() if retrieved_at is not None else None
        return summary, False


def _execute(args: argparse.Namespace, session: Session) -> tuple[list[dict[str, object]], bool]:
    production_preflight(session, args.license_class)
    service = IngestionService(session, LocalRawObjectStore(args.raw_root))
    client = NSEHttpClient()
    trading_date: date | None = getattr(args, "date", None)

    stages: list[
        tuple[str, _ProductionProvider, Callable[[Any], IngestionResult]]
    ] = []
    if args.command in {"ingest-universe", "ingest-daily"}:
        file_value = (
            getattr(args, "universe_file", None)
            if args.command == "ingest-daily"
            else args.file
        )
        uri_value = (
            getattr(args, "universe_source_uri", None)
            if args.command == "ingest-daily"
            else args.source_uri
        )
        provider = NSEUniverseProvider(
            _source(
                local_file=file_value,
                source_uri=uri_value,
                live_uri=NSE_EQUITY_UNIVERSE_URL,
                client=client,
            ),
            _metadata(NSE_UNIVERSE_DATASET_CODE, args.license_class),
        )
        stages.append(("universe", provider, service.ingest_universe))
    if args.command in {"ingest-market", "ingest-daily"}:
        assert trading_date is not None
        file_value = (
            getattr(args, "market_file", None)
            if args.command == "ingest-daily"
            else args.file
        )
        uri_value = (
            getattr(args, "market_source_uri", None)
            if args.command == "ingest-daily"
            else args.source_uri
        )
        market_provider = NSEMarketDataProvider(
            _source(
                local_file=file_value,
                source_uri=uri_value,
                live_uri=nse_market_url(trading_date),
                client=client,
            ),
            _metadata(NSE_MARKET_DATASET_CODE, args.license_class),
            trading_date,
        )
        stages.append(("market", market_provider, service.ingest_market_data))
    if args.command in {"ingest-benchmarks", "ingest-daily"}:
        assert trading_date is not None
        file_value = (
            getattr(args, "benchmark_file", None)
            if args.command == "ingest-daily"
            else args.file
        )
        uri_value = (
            getattr(args, "benchmark_source_uri", None)
            if args.command == "ingest-daily"
            else args.source_uri
        )
        benchmark_provider = NSEBenchmarkDataProvider(
            _source(
                local_file=file_value,
                source_uri=uri_value,
                live_uri=nse_benchmark_url(trading_date),
                client=client,
            ),
            _metadata(NSE_BENCHMARK_DATASET_CODE, args.license_class),
            trading_date,
        )
        stages.append(("benchmarks", benchmark_provider, service.ingest_benchmark_data))

    summaries: list[dict[str, object]] = []
    for stage, provider, ingest in stages:
        summary, succeeded = _run_stage(stage=stage, provider=provider, ingest=ingest)
        summaries.append(summary)
        if not succeeded:
            return summaries, False
    return summaries, True


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            stages, succeeded = _execute(args, session)
        print(json.dumps({"status": "completed" if succeeded else "failed", "stages": stages}))
        return 0 if succeeded else 1
    except (ProductionPreflightError, ValueError, OSError) as error:
        print(
            json.dumps(
                {"status": "failed", "stages": [], "error": str(error)}
            )
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
