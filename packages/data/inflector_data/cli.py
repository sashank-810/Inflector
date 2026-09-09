"""Small development CLI for synthetic CSV ingestion."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from inflector_core.providers import ProviderMetadata
from inflector_core.settings import get_settings
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import (
    CSVFinancialsProvider,
    CSVMarketDataProvider,
    CSVUniverseProvider,
)
from inflector_data.service import IngestionService


def _metadata(dataset_code: str) -> ProviderMetadata:
    return ProviderMetadata(
        "synthetic_csv",
        "csv",
        dataset_code,
        "synthetic-development-only",
        "Committed test fixture; never real market data.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest an Inflector development CSV fixture")
    parser.add_argument("kind", choices=("ingest-universe", "ingest-market", "ingest-financials"))
    parser.add_argument("csv", type=Path)
    parser.add_argument("--database-url", default=get_settings().database_url)
    parser.add_argument("--raw-root", type=Path, default=Path(get_settings().raw_archive_root))
    args = parser.parse_args()
    engine = create_engine(args.database_url)
    session = sessionmaker(bind=engine)()
    try:
        retrieved_at = datetime.now(UTC)
        if args.kind == "ingest-universe":
            provider = CSVUniverseProvider(args.csv, _metadata("universe"), retrieved_at)
            result = IngestionService(session, LocalRawObjectStore(args.raw_root)).ingest_universe(
                provider
            )
        elif args.kind == "ingest-market":
            provider = CSVMarketDataProvider(args.csv, _metadata("market_daily"), retrieved_at)
            result = IngestionService(
                session, LocalRawObjectStore(args.raw_root)
            ).ingest_market_data(provider)
        else:
            provider = CSVFinancialsProvider(args.csv, _metadata("financials"), retrieved_at)
            result = IngestionService(
                session, LocalRawObjectStore(args.raw_root)
            ).ingest_financials(provider)
        print(
            json.dumps(
                {
                    "run_id": str(result.run_id),
                    "status": result.status,
                    "records_received": result.records_received,
                    "records_accepted": result.records_accepted,
                    "records_quarantined": result.records_quarantined,
                    "records_duplicated": result.records_duplicated,
                }
            )
        )
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    main()
