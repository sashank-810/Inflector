"""Phase 2A provenance, validation, idempotency, and revision tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from inflector_core.providers import IngestionEnvelope, ProviderMetadata, UniverseRecord
from inflector_data.archive import ArchivedRawObject, LocalRawObjectStore
from inflector_data.providers import CSVMarketDataProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import DataQualityIssue, IngestionRun, PriceBar, SourceRecord

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


def test_raw_payload_reference_is_persisted(session, tmp_path: Path) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", METADATA, RETRIEVED_AT)
    )

    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "SYN-UNI-001")
    )
    assert source is not None
    assert source.raw_payload_reference == "row-1"


def test_equivalent_market_observation_with_different_id_is_not_a_new_price(
    session, tmp_path: Path
) -> None:
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
            FIXTURES / "market_equivalent_other_id_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
        )
    )

    duplicate = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "SYN-MKT-OTHER-ID")
    )
    assert result.records_duplicated == 1
    assert session.scalar(select(func.count()).select_from(PriceBar)) == 1
    assert duplicate is not None and duplicate.validation_status == "duplicate_economic"


def test_ambiguous_correction_is_quarantined(session, tmp_path: Path) -> None:
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
            FIXTURES / "market_ambiguous_correction_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
        )
    )

    issue = session.scalar(
        select(DataQualityIssue).where(DataQualityIssue.rule_code == "ambiguous_price_revision")
    )
    assert result.records_quarantined == 1
    assert session.scalar(select(func.count()).select_from(PriceBar)) == 1
    assert issue is not None


def test_invalid_universe_dates_are_quarantined(session, tmp_path: Path) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_invalid_dates_synthetic.csv", METADATA, RETRIEVED_AT
        )
    )

    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert result.records_quarantined == 2
    assert {"invalid_valid_to", "valid_to_before_valid_from"}.issubset(rules)


def test_malformed_sha256_is_rejected() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        IngestionEnvelope(
            provider=METADATA,
            external_record_id="invalid",
            source_uri="fixture://invalid",
            raw_payload_reference="row-1",
            content_sha256="g" * 64,
            retrieved_at=RETRIEVED_AT,
            record=UniverseRecord(
                "Invalid",
                "Invalid",
                "Sector",
                "Industry",
                "INF0BAD01010",
                "equity",
                "active",
                "NSE",
                "INVALID",
                "active",
                None,
                None,
            ),
        )


class _FailingRawStore:
    def put(self, content: bytes) -> ArchivedRawObject:
        raise OSError("archive unavailable")


def test_archive_failure_creates_no_zombie_run(session) -> None:
    service = IngestionService(session, _FailingRawStore())
    with pytest.raises(OSError, match="archive unavailable"):
        service.ingest_universe(
            CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", METADATA, RETRIEVED_AT)
        )

    assert session.scalar(select(func.count()).select_from(IngestionRun)) == 0


def test_processing_failure_finalizes_run(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", METADATA, RETRIEVED_AT)
    )

    def fail_add_price(**_: object) -> None:
        raise RuntimeError("persistence unavailable")

    monkeypatch.setattr(service._repository, "add_price", fail_add_price)
    with pytest.raises(RuntimeError, match="persistence unavailable"):
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "market_valid_synthetic.csv", MARKET_METADATA, RETRIEVED_AT
            )
        )

    run = session.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()))
    assert run is not None
    assert run.status == "failed" and run.finished_at is not None
    assert run.error_message is not None and "persistence unavailable" in run.error_message
    assert session.scalar(select(func.count()).select_from(PriceBar)) == 0
