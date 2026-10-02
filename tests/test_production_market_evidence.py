"""Production F official delivery and valuation-qualification regressions."""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.market_adjustments import MarketAdjustmentPrimitives
from inflector_data.market_pit import PointInTimeMarketBar, PointInTimeMarketReader
from inflector_data.market_structure_features import MarketStructureFeaturePrimitives
from inflector_data.nse_cli import _execute_delivery_range
from inflector_data.nse_delivery import (
    NSE_DELIVERY_DATASET_CODE,
    NSE_DELIVERY_REPORT_FAMILY,
    NSEDeliveryFormatError,
    NSEMarketDeliveryProvider,
    nse_delivery_url,
)
from inflector_data.nse_http import AcquiredNSEArtifact
from inflector_data.operations_profile import load_operations_profile
from inflector_data.pit import SourceRecordView
from inflector_data.production_operations import operational_stage_definitions
from inflector_data.research_profile import load_research_profile
from inflector_data.service import IngestionService
from inflector_database.models import (
    Company,
    DataProvider,
    ExchangeListing,
    MarketDeliveryObservation,
    ProviderDataset,
    Security,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/nse_delivery_fictional.csv"
TRADE_DATE = date(2026, 9, 30)
RETRIEVED = datetime(2026, 10, 2, 6, 28, 48, tzinfo=UTC)
URI = nse_delivery_url(TRADE_DATE)
LICENCE = "official-source-terms-reviewed-locally"


class _Source:
    def __init__(self, payload: bytes, at: datetime = RETRIEVED, uri: str = URI) -> None:
        self.source_uri = uri
        self._artifact = AcquiredNSEArtifact(uri, payload, at, "text/csv")

    def acquire(self) -> AcquiredNSEArtifact:
        return self._artifact


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        "nse_official",
        "https",
        NSE_DELIVERY_DATASET_CODE,
        LICENCE,
        redistributable=False,
    )


def _identity(
    session: Session, *, symbol: str = "FICALPHA", isin: str = "INE0FIC01019"
) -> Security:
    company = Company(
        legal_name=f"{symbol} Limited",
        display_name=f"{symbol} Limited",
        sector="",
        industry="",
    )
    security = Security(company=company, isin=isin, security_type="equity", status="active")
    ExchangeListing(
        security=security,
        exchange="NSE",
        symbol=symbol,
        valid_from=date(2020, 1, 1),
        valid_to=None,
        status="active",
    )
    session.add(company)
    session.commit()
    return security


def test_official_delivery_parser_exact_values_missing_and_series_filter() -> None:
    provider = NSEMarketDeliveryProvider(
        _Source(FIXTURE.read_bytes()),
        _metadata(),
        TRADE_DATE,
        {"FICALPHA": "INE0FIC01019", "FICMISS": "INE0MIS01017"},
    )
    batch = provider.fetch_market_delivery()
    assert batch.raw_payload == FIXTURE.read_bytes()
    assert batch.source_uri == URI
    assert provider.skipped_rows == 1
    assert NSE_DELIVERY_REPORT_FAMILY == "full_bhavcopy_security_deliverable_v1"
    alpha, missing = (item.record for item in batch.records)
    assert (alpha.symbol, alpha.series, alpha.trading_date) == ("FICALPHA", "EQ", TRADE_DATE)
    assert alpha.total_traded_quantity == 1000
    assert alpha.delivery_quantity == 452
    assert alpha.reported_delivery_percentage == Decimal("45.20")
    assert alpha.delivery_percentage == Decimal("0.452")
    assert missing.delivery_quantity is None
    assert missing.reported_delivery_percentage is None
    assert missing.delivery_percentage is None


def test_delivery_parser_headers_and_malformed_values_fail_closed() -> None:
    with pytest.raises(NSEDeliveryFormatError, match="missing headers"):
        NSEMarketDeliveryProvider(
            _Source(b"SYMBOL,SERIES\nFICALPHA,EQ\n"),
            _metadata(),
            TRADE_DATE,
            {"FICALPHA": "INE0FIC01019"},
        ).fetch_market_delivery()
    payload = FIXTURE.read_bytes().replace(b" 1000, 1.01", b" invalid, 1.01", 1)
    record = (
        NSEMarketDeliveryProvider(
            _Source(payload), _metadata(), TRADE_DATE, {"FICALPHA": "INE0FIC01019"}
        )
        .fetch_market_delivery()
        .records[0]
        .record
    )
    assert "invalid_traded_quantity" in record.parse_errors


def test_delivery_archive_ingestion_idempotency_conflict_and_pit(
    session: Session, tmp_path: Path
) -> None:
    security = _identity(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    provider = NSEMarketDeliveryProvider(
        _Source(FIXTURE.read_bytes()), _metadata(), TRADE_DATE, {"FICALPHA": security.isin}
    )
    first = service.ingest_market_delivery(provider)
    assert (first.records_accepted, first.records_quarantined) == (1, 0)
    duplicate = service.ingest_market_delivery(provider)
    assert duplicate.records_duplicated == 1
    observation = session.scalar(select(MarketDeliveryObservation))
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.validation_status == "accepted")
    )
    dataset = session.scalar(
        select(ProviderDataset)
        .join(DataProvider)
        .where(
            DataProvider.code == "nse_official", ProviderDataset.code == NSE_DELIVERY_DATASET_CODE
        )
    )
    assert observation is not None and source is not None and dataset is not None
    assert (
        source.raw_content_sha256
        == LocalRawObjectStore(tmp_path / "raw").put(FIXTURE.read_bytes()).content_sha256
    )
    assert observation.available_at.replace(tzinfo=UTC) == RETRIEVED
    reader = PointInTimeMarketReader(session)
    assert (
        reader.market_delivery_series_as_of(
            provider_dataset_id=dataset.id,
            security_id=security.id,
            series="EQ",
            as_of=RETRIEVED - timedelta(seconds=1),
        )
        == []
    )
    visible = reader.market_delivery_series_as_of(
        provider_dataset_id=dataset.id,
        security_id=security.id,
        series="EQ",
        as_of=RETRIEVED,
    )
    assert len(visible) == 1 and visible[0].delivery_percentage == Decimal("0.452")

    changed = FIXTURE.read_bytes().replace(b" 452, 45.20", b" 500, 50.00", 1)
    later = NSEMarketDeliveryProvider(
        _Source(changed, RETRIEVED), _metadata(), TRADE_DATE, {"FICALPHA": security.isin}
    )
    conflict = service.ingest_market_delivery(later)
    assert conflict.records_quarantined == 1
    revised = FIXTURE.read_bytes().replace(b" 452, 45.20", b" 600, 60.00", 1)
    revision = service.ingest_market_delivery(
        NSEMarketDeliveryProvider(
            _Source(revised, RETRIEVED + timedelta(hours=1)),
            _metadata(),
            TRADE_DATE,
            {"FICALPHA": security.isin},
        )
    )
    assert revision.records_accepted == 1
    selected = reader.market_delivery_series_as_of(
        provider_dataset_id=dataset.id,
        security_id=security.id,
        series="EQ",
        as_of=RETRIEVED + timedelta(hours=1),
    )
    assert selected[0].delivery_percentage == Decimal("0.6")


def test_existing_delivery_feature_uses_only_complete_pit_visible_window(
    session: Session, tmp_path: Path
) -> None:
    security = _identity(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    start = date(2026, 9, 1)
    for index in range(20):
        day = start + timedelta(days=index)
        payload = (
            "SYMBOL,SERIES,DATE1,TTL_TRD_QNTY,DELIV_QTY,DELIV_PER\n"
            f"FICALPHA,EQ,{day:%d-%b-%Y},1000,500,50.00\n"
        ).encode()
        service.ingest_market_delivery(
            NSEMarketDeliveryProvider(
                _Source(payload, RETRIEVED, nse_delivery_url(day)),
                _metadata(),
                day,
                {"FICALPHA": security.isin},
            )
        )
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(
            DataProvider.code == "nse_official", ProviderDataset.code == NSE_DELIVERY_DATASET_CODE
        )
    )
    assert dataset_id is not None
    source_view = SourceRecordView(
        id=security.id,
        external_record_id="price",
        source_uri="fixture://price",
        raw_object_key="sha256/fixture",
        raw_payload_reference="row",
        content_sha256="0" * 64,
        validation_status="accepted",
    )
    bars = tuple(
        PointInTimeMarketBar(
            id=security.id,
            provider_dataset_id=dataset_id,
            security_id=security.id,
            trading_date=start + timedelta(days=index),
            interval="1d",
            open_price=Decimal("100"),
            high_price=Decimal("100"),
            low_price=Decimal("100"),
            close_price=Decimal("100"),
            volume=1000,
            market_cap=None,
            delivery_quantity=None,
            delivery_percentage=None,
            available_at=RETRIEVED,
            revision_at=None,
            ingested_at=RETRIEVED,
            source_record=source_view,
        )
        for index in range(20)
    )
    primitives = MarketStructureFeaturePrimitives(
        PointInTimeMarketReader(session), cast(MarketAdjustmentPrimitives, None)
    )
    complete = primitives._independent_delivery(  # noqa: SLF001
        bars,
        bars[-1].trading_date,
        RETRIEVED,
        delivery_provider_dataset_id=dataset_id,
        security_id=security.id,
        series="EQ",
        observation_window=20,
    )
    assert complete.value == Decimal("0.5")
    unavailable = primitives._independent_delivery(  # noqa: SLF001
        bars,
        bars[-1].trading_date,
        RETRIEVED - timedelta(seconds=1),
        delivery_provider_dataset_id=dataset_id,
        security_id=security.id,
        series="EQ",
        observation_window=20,
    )
    assert unavailable.value is None
    assert unavailable.warnings == ("incomplete_delivery_window",)


def test_delivery_range_and_profile_versioning(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identity(session)
    requested: list[date] = []

    def fake_run_stage(**kwargs: object) -> tuple[dict[str, object], bool]:
        provider = cast(NSEMarketDeliveryProvider, kwargs["provider"])
        requested.append(provider._trading_date)  # noqa: SLF001
        status = "source_not_available" if len(requested) == 2 else "completed"
        return {
            "stage": "delivery",
            "status": status,
            "source_uri": provider.source_uri,
            "retrieved_at": None,
            "run_id": None,
            "records_received": 0,
            "records_accepted": 0,
            "records_quarantined": 0,
            "records_duplicated": 0,
            "provider_skipped_rows": 0,
        }, status == "completed"

    monkeypatch.setattr("inflector_data.nse_cli._run_stage", fake_run_stage)
    args = Namespace(
        raw_root=tmp_path / "raw",
        license_class=LICENCE,
        from_date=date(2026, 9, 27),
        to_date=date(2026, 9, 29),
        request_delay_seconds=0,
    )
    summaries, succeeded = _execute_delivery_range(args, session)
    assert succeeded
    assert requested == [date(2026, 9, 27), date(2026, 9, 28), date(2026, 9, 29)]
    assert summaries[0]["source_not_available"] == 1
    args.to_date = date(2027, 2, 25)
    with pytest.raises(ValueError, match="150 calendar days"):
        _execute_delivery_range(args, session)

    research_v1 = load_research_profile(ROOT / "config/research/production_research_v1.json")
    research_v2 = load_research_profile(ROOT / "config/research/production_research_v2.json")
    operations_v1 = load_operations_profile(
        ROOT / "config/operations/production_operations_v1.json"
    )
    operations_v2 = load_operations_profile(
        ROOT / "config/operations/production_operations_v2.json"
    )
    assert (
        research_v1.checksum_sha256
        == "182d139eaf6a6603b3fa817cdc590676454d2e4051e49a495a32591747f51f80"
    )
    assert (
        operations_v1.checksum_sha256
        == "ffefef5ba249ef93eb8ac7db26297a579ed6d59ad02204ad263a7bc354d39d4c"
    )
    assert research_v2.provider_datasets["delivery"] is not None
    assert research_v2.delivery_observation_window == 20
    assert operations_v2.delivery_history_calendar_lookback_days == 120
    assert "delivery_history" not in dict(operational_stage_definitions(operations_v1))
    assert "delivery_history" in dict(operational_stage_definitions(operations_v2))


def test_market_delivery_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    command.upgrade(config, "20261002_0017")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    inspector = inspect(engine)
    assert "market_delivery_observations" in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("market_delivery_observations")}
    assert {
        "security_id",
        "provider_dataset_id",
        "source_record_id",
        "trading_date",
        "reported_delivery_percentage",
        "delivery_percentage",
        "available_at",
    } <= columns
    assert {
        item["name"] for item in inspector.get_unique_constraints("market_delivery_observations")
    } == {None}
    engine.dispose()
    command.downgrade(config, "20261002_0016")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    assert "market_delivery_observations" not in inspect(engine).get_table_names()
    engine.dispose()
