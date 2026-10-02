"""Explicit production CLI for official NSE archive-first ingestion."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
)
from inflector_core.providers import ProviderMetadata
from inflector_data.announcement_pit import PointInTimeAnnouncementReader
from inflector_data.archive import LocalRawObjectStore
from inflector_data.business_event_detection import BusinessEventDetectionService
from inflector_data.business_event_pit import PointInTimeBusinessEventReader
from inflector_data.business_event_quantitative import (
    BusinessEventQuantitativeDerivationService,
)
from inflector_data.document_extractors import PyPdfTextExtractor
from inflector_data.document_fetchers import NSEOfficialDocumentFetcher
from inflector_data.document_services import (
    DocumentAcquisitionService,
    DocumentTextExtractionService,
)
from inflector_data.document_text import DocumentExtractionIdentity, DocumentTextReader
from inflector_data.nse_corporate_filings import (
    NSE_ANNOUNCEMENT_DATASET_CODE,
    NSE_CORPORATE_ACTION_DATASET_CODE,
    NSEAnnouncementProvider,
    NSECorporateActionProvider,
    nse_announcements_url,
    nse_corporate_actions_url,
    nse_equity_identities,
)
from inflector_data.nse_delivery import (
    NSE_DELIVERY_DATASET_CODE,
    NSEMarketDeliveryProvider,
    nse_delivery_url,
)
from inflector_data.nse_financials import (
    NSE_FINANCIAL_DATASET_CODE,
    NSE_INTEGRATED_FINANCIAL_MAPPING_VERSION,
    NSE_INTENTIONALLY_UNMAPPED_TARGET_METRICS,
    NSEFinancialFiling,
    NSEIntegratedFinancialDiscovery,
    NSEIntegratedFinancialsProvider,
)
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
from inflector_database.business_event_quantitative_repository import (
    BusinessEventQuantitativeRepository,
)
from inflector_database.business_event_repository import BusinessEventRepository
from inflector_database.ingestion_repository import IngestionRepository
from inflector_database.models import (
    Announcement,
    Company,
    DataProvider,
    ExchangeListing,
    ProviderDataset,
    Security,
)

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
        select(ProviderDataset.id).where(ProviderDataset.licence_class == SYNTHETIC_LICENCE_MARKER)
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
    parser = argparse.ArgumentParser(description="Archive-first official NSE production ingestion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "ingest-universe",
        "ingest-market",
        "ingest-market-range",
        "ingest-delivery-range",
        "ingest-benchmarks",
        "ingest-daily",
        "ingest-financials",
        "ingest-corporate-actions",
        "ingest-announcements",
        "ingest-catalyst-evidence",
    ):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--database-url", required=True)
        subparser.add_argument("--raw-root", type=Path, required=True)
        subparser.add_argument("--license-class", required=True)
        if command in {"ingest-market", "ingest-benchmarks", "ingest-daily"}:
            subparser.add_argument("--date", type=date.fromisoformat, required=True)
        if command in {"ingest-market-range", "ingest-delivery-range"}:
            subparser.add_argument("--from-date", type=date.fromisoformat, required=True)
            subparser.add_argument("--to-date", type=date.fromisoformat, required=True)
            subparser.add_argument("--request-delay-seconds", type=float, default=1.0)
        if command == "ingest-financials":
            targets = subparser.add_mutually_exclusive_group(required=True)
            targets.add_argument("--symbol")
            targets.add_argument("--symbols-file", type=Path)
            subparser.add_argument("--max-symbols", type=int, default=25)
            subparser.add_argument("--max-filings", type=int, default=20)
            subparser.add_argument("--from-date", type=date.fromisoformat)
            subparser.add_argument("--to-date", type=date.fromisoformat)
            subparser.add_argument("--request-delay-seconds", type=float, default=1.0)
        elif command in {
            "ingest-corporate-actions",
            "ingest-announcements",
            "ingest-catalyst-evidence",
        }:
            subparser.add_argument("--from-date", type=date.fromisoformat, required=True)
            subparser.add_argument("--to-date", type=date.fromisoformat, required=True)
            subparser.add_argument("--symbol")
            _local_arguments(subparser, "")
            if command != "ingest-corporate-actions":
                subparser.add_argument("--max-announcements", type=int, default=100)
            if command == "ingest-catalyst-evidence":
                subparser.add_argument("--max-documents", type=int, default=100)
        elif command == "ingest-universe":
            _local_arguments(subparser, "")
        elif command == "ingest-market":
            _local_arguments(subparser, "")
        elif command == "ingest-benchmarks":
            _local_arguments(subparser, "")
        elif command == "ingest-daily":
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
    status = "source_not_available" if isinstance(error, NSESourceNotAvailableError) else "failed"
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

    stages: list[tuple[str, _ProductionProvider, Callable[[Any], IngestionResult]]] = []
    if args.command in {"ingest-universe", "ingest-daily"}:
        file_value = (
            getattr(args, "universe_file", None) if args.command == "ingest-daily" else args.file
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
            getattr(args, "market_file", None) if args.command == "ingest-daily" else args.file
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
            getattr(args, "benchmark_file", None) if args.command == "ingest-daily" else args.file
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


def _execute_market_range(
    args: argparse.Namespace, session: Session
) -> tuple[list[dict[str, object]], bool]:
    """Attempt both exact daily artifacts for every requested calendar date."""

    production_preflight(session, args.license_class)
    if args.to_date < args.from_date:
        raise ValueError("to-date must not precede from-date")
    day_count = (args.to_date - args.from_date).days + 1
    if day_count > 150:
        raise ValueError("market range must not exceed 150 calendar days")
    if args.request_delay_seconds < 0 or args.request_delay_seconds > 10:
        raise ValueError("request-delay-seconds must be between 0 and 10")

    service = IngestionService(session, LocalRawObjectStore(args.raw_root))
    client = NSEHttpClient()
    attempts: list[dict[str, object]] = []
    operational_failure = False
    for index in range(day_count):
        requested_date = args.from_date + timedelta(days=index)
        providers: tuple[tuple[str, _ProductionProvider, Callable[[Any], IngestionResult]], ...] = (
            (
                "market",
                NSEMarketDataProvider(
                    HttpNSEArtifactSource(client, nse_market_url(requested_date)),
                    _metadata(NSE_MARKET_DATASET_CODE, args.license_class),
                    requested_date,
                ),
                service.ingest_market_data,
            ),
            (
                "benchmark",
                NSEBenchmarkDataProvider(
                    HttpNSEArtifactSource(client, nse_benchmark_url(requested_date)),
                    _metadata(NSE_BENCHMARK_DATASET_CODE, args.license_class),
                    requested_date,
                ),
                service.ingest_benchmark_data,
            ),
        )
        for stage, provider, ingest in providers:
            summary, succeeded = _run_stage(stage=stage, provider=provider, ingest=ingest)
            summary["requested_date"] = requested_date.isoformat()
            attempts.append(summary)
            if not succeeded and summary["status"] != "source_not_available":
                operational_failure = True
        if args.request_delay_seconds and index < day_count - 1:
            time.sleep(args.request_delay_seconds)

    def count(stage: str, status: str) -> int:
        return sum(item["stage"] == stage and item["status"] == status for item in attempts)

    aggregate = {
        "stage": "market_range",
        "status": "failed" if operational_failure else "completed",
        "from_date": args.from_date.isoformat(),
        "to_date": args.to_date.isoformat(),
        "requested_calendar_dates": day_count,
        "market_successes": count("market", "completed"),
        "market_source_not_available": count("market", "source_not_available"),
        "market_failures": sum(
            item["stage"] == "market"
            and item["status"] not in {"completed", "source_not_available"}
            for item in attempts
        ),
        "benchmark_successes": count("benchmark", "completed"),
        "benchmark_source_not_available": count("benchmark", "source_not_available"),
        "benchmark_failures": sum(
            item["stage"] == "benchmark"
            and item["status"] not in {"completed", "source_not_available"}
            for item in attempts
        ),
        "records_accepted": sum(cast(int, item["records_accepted"]) for item in attempts),
        "records_duplicated": sum(cast(int, item["records_duplicated"]) for item in attempts),
        "records_quarantined": sum(cast(int, item["records_quarantined"]) for item in attempts),
        "attempts": attempts,
    }
    return [aggregate], not operational_failure


def _delivery_identity_map(session: Session) -> dict[str, str]:
    rows = session.execute(
        select(ExchangeListing.symbol, Security.isin)
        .join(Security, ExchangeListing.security_id == Security.id)
        .where(
            ExchangeListing.exchange == "NSE",
            ExchangeListing.valid_to.is_(None),
            ExchangeListing.status == "active",
            Security.security_type == "equity",
            Security.status == "active",
        )
        .order_by(ExchangeListing.symbol, Security.isin)
    ).all()
    identities: dict[str, str] = {}
    for symbol, isin in rows:
        normalized = symbol.strip().upper()
        existing = identities.get(normalized)
        if existing is not None and existing != isin:
            raise ProductionPreflightError("current NSE symbol identity is ambiguous")
        identities[normalized] = isin
    return identities


def _execute_delivery_range(
    args: argparse.Namespace, session: Session
) -> tuple[list[dict[str, object]], bool]:
    """Attempt the exact official delivery artifact for every requested date."""

    production_preflight(session, args.license_class)
    if args.to_date < args.from_date:
        raise ValueError("to-date must not precede from-date")
    day_count = (args.to_date - args.from_date).days + 1
    if day_count > 150:
        raise ValueError("delivery range must not exceed 150 calendar days")
    if args.request_delay_seconds < 0 or args.request_delay_seconds > 10:
        raise ValueError("request-delay-seconds must be between 0 and 10")
    identities = _delivery_identity_map(session)
    IngestionRepository(session).ensure_dataset(
        _metadata(NSE_DELIVERY_DATASET_CODE, args.license_class)
    )
    session.commit()
    service = IngestionService(session, LocalRawObjectStore(args.raw_root))
    client = NSEHttpClient()
    attempts: list[dict[str, object]] = []
    operational_failure = False
    for index in range(day_count):
        requested_date = args.from_date + timedelta(days=index)
        provider = NSEMarketDeliveryProvider(
            HttpNSEArtifactSource(client, nse_delivery_url(requested_date)),
            _metadata(NSE_DELIVERY_DATASET_CODE, args.license_class),
            requested_date,
            identities,
        )
        summary, succeeded = _run_stage(
            stage="delivery", provider=provider, ingest=service.ingest_market_delivery
        )
        summary["requested_date"] = requested_date.isoformat()
        attempts.append(summary)
        if not succeeded and summary["status"] != "source_not_available":
            operational_failure = True
        if args.request_delay_seconds and index < day_count - 1:
            time.sleep(args.request_delay_seconds)
    aggregate = {
        "stage": "delivery_range",
        "status": "failed" if operational_failure else "completed",
        "from_date": args.from_date.isoformat(),
        "to_date": args.to_date.isoformat(),
        "requested_calendar_dates": day_count,
        "successes": sum(item["status"] == "completed" for item in attempts),
        "source_not_available": sum(item["status"] == "source_not_available" for item in attempts),
        "failures": sum(item["status"] == "failed" for item in attempts),
        "records_accepted": sum(cast(int, item["records_accepted"]) for item in attempts),
        "records_duplicated": sum(cast(int, item["records_duplicated"]) for item in attempts),
        "records_quarantined": sum(cast(int, item["records_quarantined"]) for item in attempts),
        "provider_skipped_rows": sum(cast(int, item["provider_skipped_rows"]) for item in attempts),
        "attempts": attempts,
    }
    return [aggregate], not operational_failure


def _financial_symbols(args: argparse.Namespace) -> tuple[str, ...]:
    if args.max_symbols < 1 or args.max_symbols > 100:
        raise ValueError("max-symbols must be between 1 and 100")
    if args.max_filings < 1 or args.max_filings > 100:
        raise ValueError("max-filings must be between 1 and 100")
    if args.request_delay_seconds < 0 or args.request_delay_seconds > 10:
        raise ValueError("request-delay-seconds must be between 0 and 10")
    raw_symbols = (
        [args.symbol]
        if args.symbol is not None
        else args.symbols_file.read_text(encoding="utf-8-sig").splitlines()
    )
    symbols: list[str] = []
    seen: set[str] = set()
    for raw in raw_symbols:
        symbol = raw.strip().upper()
        if not symbol:
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    if not symbols:
        raise ValueError("at least one NSE symbol is required")
    if len(symbols) > args.max_symbols:
        raise ValueError("symbols input exceeds max-symbols")
    return tuple(symbols)


def _financial_stage_summary(
    *,
    symbol: str,
    filing: NSEFinancialFiling | None,
    status: str,
    error: str | None = None,
    provider: NSEIntegratedFinancialsProvider | None = None,
    result: IngestionResult | None = None,
) -> dict[str, object]:
    return {
        "stage": "financials",
        "symbol": symbol,
        "filing_sequence_id": filing.sequence_id if filing is not None else None,
        "scope": filing.scope if filing is not None else None,
        "period_end": filing.quarter_end.isoformat() if filing is not None else None,
        "status": status,
        "source_uri": filing.xbrl_uri if filing is not None else None,
        "retrieved_at": (
            provider.retrieved_at.isoformat()
            if provider is not None and provider.retrieved_at is not None
            else None
        ),
        "run_id": str(result.run_id) if result is not None else None,
        "records_received": result.records_received if result is not None else 0,
        "records_accepted": result.records_accepted if result is not None else 0,
        "records_quarantined": result.records_quarantined if result is not None else 0,
        "records_duplicated": result.records_duplicated if result is not None else 0,
        "recognized_source_facts": (
            provider.recognized_source_facts if provider is not None else 0
        ),
        "mapping_version": NSE_INTEGRATED_FINANCIAL_MAPPING_VERSION,
        "mapped_metric_codes": list(provider.mapped_metric_codes) if provider else [],
        "intentionally_unmapped_target_metrics": list(NSE_INTENTIONALLY_UNMAPPED_TARGET_METRICS),
        "unsupported_reason": provider.unsupported_reason if provider else None,
        "error": error,
    }


def _execute_financials(
    args: argparse.Namespace, session: Session
) -> tuple[list[dict[str, object]], bool]:
    production_preflight(session, args.license_class)
    symbols = _financial_symbols(args)
    service = IngestionService(session, LocalRawObjectStore(args.raw_root))
    client = NSEHttpClient(maximum_response_bytes=10_000_000)
    universe_provider = NSEUniverseProvider(
        HttpNSEArtifactSource(client, NSE_EQUITY_UNIVERSE_URL),
        _metadata(NSE_UNIVERSE_DATASET_CODE, args.license_class),
    )
    universe_batch = universe_provider.fetch_universe()
    symbol_to_isin = {
        envelope.record.symbol: envelope.record.isin
        for envelope in universe_batch.records
        if not envelope.record.parse_errors and envelope.record.symbol and envelope.record.isin
    }
    discovery = NSEIntegratedFinancialDiscovery(client)
    summaries: list[dict[str, object]] = []
    succeeded = True
    remaining = args.max_filings
    for symbol_index, symbol in enumerate(symbols):
        if remaining == 0:
            summaries.append(
                _financial_stage_summary(
                    symbol=symbol,
                    filing=None,
                    status="max_filings_reached",
                )
            )
            continue
        isin = symbol_to_isin.get(symbol)
        if isin is None:
            summaries.append(
                _financial_stage_summary(
                    symbol=symbol,
                    filing=None,
                    status="unsupported_symbol",
                    error="symbol is not an EQ row in the official NSE equity master",
                )
            )
            continue
        try:
            filings = discovery.discover(
                symbol=symbol,
                max_filings=remaining,
                from_date=args.from_date,
                to_date=args.to_date,
            )
        except (NSEAcquisitionError, OSError, ValueError, RuntimeError) as error:
            summaries.append(
                _financial_stage_summary(
                    symbol=symbol, filing=None, status="failed", error=str(error)
                )
            )
            succeeded = False
            continue
        if not filings:
            summaries.append(
                _financial_stage_summary(symbol=symbol, filing=None, status="no_filings")
            )
            continue
        for filing in filings:
            if remaining == 0:
                break
            provider = NSEIntegratedFinancialsProvider(
                HttpNSEArtifactSource(client, filing.xbrl_uri),
                _metadata(NSE_FINANCIAL_DATASET_CODE, args.license_class),
                filing,
                expected_isin=isin,
            )
            try:
                result = service.ingest_financials(provider)
                status = "unsupported" if provider.unsupported_reason else result.status
                summaries.append(
                    _financial_stage_summary(
                        symbol=symbol,
                        filing=filing,
                        status=status,
                        provider=provider,
                        result=result,
                    )
                )
            except (NSEAcquisitionError, OSError, ValueError, RuntimeError) as error:
                summaries.append(
                    _financial_stage_summary(
                        symbol=symbol,
                        filing=filing,
                        status=(
                            "source_not_available"
                            if isinstance(error, NSESourceNotAvailableError)
                            else "failed"
                        ),
                        error=str(error),
                        provider=provider,
                    )
                )
                succeeded = False
            remaining -= 1
            if args.request_delay_seconds and (remaining > 0 or symbol_index < len(symbols) - 1):
                time.sleep(args.request_delay_seconds)
    return summaries, succeeded


def _corporate_filing_provider(
    args: argparse.Namespace,
    *,
    client: NSEHttpClient,
) -> tuple[NSECorporateActionProvider | NSEAnnouncementProvider, LocalRawObjectStore]:
    store = LocalRawObjectStore(args.raw_root)
    universe = NSEUniverseProvider(
        HttpNSEArtifactSource(client, NSE_EQUITY_UNIVERSE_URL),
        _metadata(NSE_UNIVERSE_DATASET_CODE, args.license_class),
    ).fetch_universe()
    identities = nse_equity_identities(universe.records)
    source_uri = (
        nse_corporate_actions_url(args.from_date, args.to_date, symbol=args.symbol)
        if args.command == "ingest-corporate-actions"
        else nse_announcements_url(args.from_date, args.to_date, symbol=args.symbol)
    )
    source = _source(
        local_file=args.file,
        source_uri=args.source_uri,
        live_uri=source_uri,
        client=client,
    )
    if args.command == "ingest-corporate-actions":
        return (
            NSECorporateActionProvider(
                source,
                _metadata(NSE_CORPORATE_ACTION_DATASET_CODE, args.license_class),
                identities,
            ),
            store,
        )
    return (
        NSEAnnouncementProvider(
            source,
            _metadata(NSE_ANNOUNCEMENT_DATASET_CODE, args.license_class),
            identities,
            max_announcements=args.max_announcements,
        ),
        store,
    )


def _execute_corporate_filings(
    args: argparse.Namespace, session: Session
) -> tuple[list[dict[str, object]], bool]:
    production_preflight(session, args.license_class)
    client = NSEHttpClient()
    provider, store = _corporate_filing_provider(args, client=client)
    service = IngestionService(session, store)
    if isinstance(provider, NSECorporateActionProvider):
        summary, succeeded = _run_stage(
            stage="corporate_actions",
            provider=provider,
            ingest=service.ingest_corporate_actions,
        )
        summary["supported_action_counts"] = provider.supported_action_counts
        summary["purpose_rules_version"] = "nse_corporate_action_purpose_rules_v1"
        summary["unsupported_purpose_examples"] = list(provider.unsupported_purposes[:10])
        return [summary], succeeded

    summary, succeeded = _run_stage(
        stage="announcements",
        provider=provider,
        ingest=service.ingest_announcements,
    )
    summary["documents_discovered"] = provider.documents_discovered
    if not succeeded or args.command == "ingest-announcements":
        return [summary], succeeded
    try:
        catalyst = _process_catalyst_evidence(
            args=args,
            session=session,
            store=store,
            client=client,
            as_of=provider.retrieved_at,
            external_record_ids=provider.external_record_ids,
        )
        return [summary, catalyst], True
    except (OSError, ValueError, RuntimeError) as error:
        session.rollback()
        return [
            summary,
            {"stage": "catalyst_evidence", "status": "failed", "error": str(error)},
        ], False


def _process_catalyst_evidence(
    *,
    args: argparse.Namespace,
    session: Session,
    store: LocalRawObjectStore,
    client: NSEHttpClient,
    as_of: datetime | None,
    external_record_ids: tuple[str, ...],
) -> dict[str, object]:
    if as_of is None:
        raise ValueError("announcement retrieval time is unavailable")
    if args.max_documents < 0 or args.max_documents > 500:
        raise ValueError("max-documents must be between 0 and 500")
    dataset = session.scalar(
        select(ProviderDataset)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(
            DataProvider.code == NSE_PROVIDER_CODE,
            ProviderDataset.code == NSE_ANNOUNCEMENT_DATASET_CODE,
        )
    )
    if dataset is None:
        raise ValueError("NSE announcement dataset was not persisted")
    reader = PointInTimeAnnouncementReader(session)
    announcements = []
    if args.symbol:
        security = session.scalar(
            select(Security)
            .join(ExchangeListing, ExchangeListing.security_id == Security.id)
            .where(
                ExchangeListing.exchange == "NSE",
                ExchangeListing.symbol == args.symbol.strip().upper(),
            )
        )
        if security is None:
            raise ValueError("requested NSE symbol is not in the canonical universe")
        announcements.extend(
            reader.security_announcements_as_of(
                provider_dataset_id=dataset.id,
                security_id=security.id,
                as_of=as_of,
                start_date=args.from_date,
                end_date=args.to_date,
            )
        )
    else:
        company_ids = list(
            session.scalars(
                select(Announcement.company_id)
                .where(Announcement.provider_dataset_id == dataset.id)
                .distinct()
            )
        )
        for company_id in company_ids:
            announcements.extend(
                reader.company_announcements_as_of(
                    provider_dataset_id=dataset.id,
                    company_id=company_id,
                    as_of=as_of,
                    start_date=args.from_date,
                    end_date=args.to_date,
                )
            )
    allowed_external_ids = frozenset(external_record_ids)
    selected = {
        item.id: item for item in announcements if item.external_record_id in allowed_external_ids
    }
    ordered = sorted(selected.values(), key=lambda item: (item.available_at, str(item.id)))
    acquisition = DocumentAcquisitionService(session, store)
    extraction = DocumentTextExtractionService(session, store)
    extractor = PyPdfTextExtractor()
    fetcher = NSEOfficialDocumentFetcher(client)
    text_reader = DocumentTextReader(session, store)
    extraction_identity = DocumentExtractionIdentity(
        extractor.extractor_code,
        extractor.extractor_semantic_version,
        extractor.extractor_runtime_version,
    )
    detector = BusinessEventDetectionService(BusinessEventRepository(session))
    event_reader = PointInTimeBusinessEventReader(session)
    quant = BusinessEventQuantitativeDerivationService(
        BusinessEventQuantitativeRepository(session), store
    )
    documents_acquired = 0
    acquisition_failures = 0
    successful_extractions = 0
    unavailable_extractions = 0
    events: dict[str, int] = {}
    derivations = 0
    facts: dict[str, int] = {}
    derived_at = datetime.now(UTC)
    for announcement in ordered:
        texts = []
        for document in announcement.documents:
            if documents_acquired + acquisition_failures >= args.max_documents:
                break
            try:
                asset = acquisition.acquire(document_id=document.id, fetcher=fetcher)
                documents_acquired += 1
            except (OSError, ValueError, RuntimeError):
                acquisition_failures += 1
                continue
            try:
                extracted = extraction.extract(
                    document_asset_id=asset.asset_id,
                    extractor=extractor,
                )
                if extracted.status == "success":
                    successful_extractions += 1
                else:
                    unavailable_extractions += 1
                text = text_reader.text_for_document(
                    document=document,
                    extraction_identity=extraction_identity,
                )
                if text is not None:
                    texts.append(text)
            except (OSError, ValueError, RuntimeError):
                unavailable_extractions += 1
        detected = detector.detect(
            announcement=announcement,
            document_texts=tuple(texts),
            derived_at=derived_at,
        )
        for result in detected:
            events[result.event_type] = events.get(result.event_type, 0) + 1
        for event in event_reader.events_for_announcement(
            announcement=announcement,
            ruleset_code=BUSINESS_EVENT_RULESET_CODE,
            ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
        ):
            result = quant.derive(event=event, derived_at=derived_at)
            derivations += 1
            for fact in result.facts:
                facts[fact.fact_code] = facts.get(fact.fact_code, 0) + 1
    session.commit()
    return {
        "stage": "catalyst_evidence",
        "status": "completed",
        "announcements_processed": len(ordered),
        "documents_discovered": sum(len(item.documents) for item in ordered),
        "documents_acquired": documents_acquired,
        "document_acquisition_failures": acquisition_failures,
        "successful_text_extractions": successful_extractions,
        "no_text_or_failed_extractions": unavailable_extractions,
        "business_events_by_type": events,
        "quantitative_derivations": derivations,
        "quantitative_facts_by_code": facts,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine: Engine | None = None
    try:
        engine = create_engine(args.database_url, pool_pre_ping=True)
        factory = sessionmaker(bind=engine, autoflush=False)
        with factory() as session:
            stages, succeeded = (
                _execute_market_range(args, session)
                if args.command == "ingest-market-range"
                else _execute_delivery_range(args, session)
                if args.command == "ingest-delivery-range"
                else (
                    _execute_financials(args, session)
                    if args.command == "ingest-financials"
                    else (
                        _execute_corporate_filings(args, session)
                        if args.command
                        in {
                            "ingest-corporate-actions",
                            "ingest-announcements",
                            "ingest-catalyst-evidence",
                        }
                        else _execute(args, session)
                    )
                )
            )
        print(json.dumps({"status": "completed" if succeeded else "failed", "stages": stages}))
        return 0 if succeeded else 1
    except (ProductionPreflightError, ValueError, OSError) as error:
        print(json.dumps({"status": "failed", "stages": [], "error": str(error)}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
