"""Phase 2A provenance, validation, idempotency, and revision tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import CSVMarketDataProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import DataQualityIssue, PriceBar, SourceRecord

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 1, 7, tzinfo=UTC)
METADATA = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
MARKET_METADATA = ProviderMetadata(
    "synthetic_csv", "csv", "market_daily", "synthetic-development-only"
)


def test_local_archive_is_content_addressed(tmp_path: Path) -> None:
    archive = LocalRawObjectStore(tmp_path)
    first = archive.put(b"immutable source")
    second = archive.put(b"immutable source")

    assert first == second
    assert (tmp_path / first.object_key).read_bytes() == b"immutable source"


def test_csv_ingestion_quarantines_invalid_rows_and_is_idempotent(session, tmp_path: Path) -> None:
    archive = LocalRawObjectStore(tmp_path / "raw")
    service = IngestionService(session, archive)
    universe = CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", METADATA, RETRIEVED_AT)
    assert service.ingest_universe(universe).records_accepted == 3

    market = CSVMarketDataProvider(
        FIXTURES / "market_valid_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
    )
    assert service.ingest_market_data(market).records_accepted == 1
    duplicate = service.ingest_market_data(market)
    assert duplicate.records_duplicated == 1

    invalid = CSVMarketDataProvider(
        FIXTURES / "market_invalid_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
    )
    result = service.ingest_market_data(invalid)
    assert (result.records_accepted, result.records_quarantined) == (0, 2)
    assert session.scalar(select(func.count()).select_from(PriceBar)) == 1
    assert session.scalar(select(func.count()).select_from(DataQualityIssue)) >= 3
    assert session.scalar(select(func.count()).select_from(SourceRecord)) == 6


def test_corrected_price_is_append_only(session, tmp_path: Path) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", METADATA, RETRIEVED_AT)
    )
    service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_valid_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
        )
    )
    result = service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_correction_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
        )
    )

    prices = list(session.scalars(select(PriceBar).order_by(PriceBar.available_at)))
    assert result.records_accepted == 1
    assert [price.close_price for price in prices] == [Decimal("100"), Decimal("101")]
    assert prices[1].revision_at is not None
