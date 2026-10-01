"""Production NSE acquisition, parsing, PIT, safety, and CLI regressions."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.nse_cli import (
    ProductionPreflightError,
    _failure_summary,
    main,
    production_preflight,
)
from inflector_data.nse_http import (
    AcquiredNSEArtifact,
    NSEAcquisitionError,
    NSEArtifactTooLargeError,
    NSEHostNotAllowedError,
    NSEHttpClient,
    NSESourceNotAvailableError,
    _AllowlistedRedirectHandler,
    validate_nse_url,
)
from inflector_data.nse_providers import (
    NSE_BENCHMARK_DATASET_CODE,
    NSE_MARKET_DATASET_CODE,
    NSE_UNIVERSE_DATASET_CODE,
    LocalNSEArtifactSource,
    NSEBenchmarkDataProvider,
    NSEFormatError,
    NSEMarketDataProvider,
    NSEUniverseProvider,
    nse_market_filename,
)
from inflector_data.service import IngestionService
from inflector_database.base import Base
from inflector_database.models import (
    BenchmarkBar,
    BenchmarkSeries,
    Company,
    DataProvider,
    DataQualityIssue,
    PriceBar,
    ProviderDataset,
    SourceRecord,
)
from inflector_database.seed import seed_database

FIXTURES = Path(__file__).parent / "fixtures"
TRADE_DATE = date(2026, 9, 30)
RETRIEVED_AT = datetime(2026, 10, 1, 8, 15, 30, tzinfo=UTC)
LATER_RETRIEVED_AT = datetime(2026, 10, 1, 9, 45, tzinfo=UTC)
UNIVERSE_URI = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
MARKET_URI = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_20260930_F_0000.csv.zip"
)
BENCHMARK_URI = (
    "https://nsearchives.nseindia.com/content/indices/ind_close_all_30092026.csv"
)


def _metadata(dataset_code: str) -> ProviderMetadata:
    return ProviderMetadata(
        "nse_official",
        "https",
        dataset_code,
        "official-source-terms-reviewed-locally",
        redistributable=False,
    )


class _StaticSource:
    def __init__(self, uri: str, payload: bytes, retrieved_at: datetime = RETRIEVED_AT) -> None:
        self.source_uri = uri
        self.artifact = AcquiredNSEArtifact(uri, payload, retrieved_at)

    def acquire(self) -> AcquiredNSEArtifact:
        return self.artifact


def _zip_market(payload: bytes, *, member: str | None = None) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member or nse_market_filename(TRADE_DATE), payload)
    return stream.getvalue()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class _FakeResponse:
    def __init__(self, payload: bytes, url: str, content_length: int | None = None) -> None:
        self.payload = payload
        self.url = url
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.payload if amount < 0 else self.payload[:amount]

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float) -> _FakeResponse:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return cast(_FakeResponse, outcome)


def test_http_allowlist_https_redirect_and_source_not_available() -> None:
    validate_nse_url(UNIVERSE_URI)
    with pytest.raises(NSEHostNotAllowedError, match="HTTPS"):
        validate_nse_url("http://nsearchives.nseindia.com/content/equities/EQUITY_L.csv")
    with pytest.raises(NSEHostNotAllowedError, match="allowlisted"):
        validate_nse_url("https://example.com/EQUITY_L.csv")
    with pytest.raises(NSEHostNotAllowedError, match="allowlisted"):
        _AllowlistedRedirectHandler().redirect_request(
            Request(UNIVERSE_URI),
            None,
            302,
            "Found",
            {},
            "https://example.com/redirected.csv",
        )

    missing = HTTPError(UNIVERSE_URI, 404, "not found", cast(Any, {}), None)
    client = NSEHttpClient(opener=cast(Any, _FakeOpener([missing])), maximum_attempts=1)
    with pytest.raises(NSESourceNotAvailableError):
        client.acquire(UNIVERSE_URI, warm_up=False)


def test_http_warmup_retry_byte_bound_exact_payload_and_observed_time() -> None:
    payload = b"exact official bytes\x00\xff"
    homepage = _FakeResponse(b"home", "https://www.nseindia.com/")
    api_uri = "https://www.nseindia.com/api/official-artifact"
    transient = HTTPError(api_uri, 503, "temporary", cast(Any, {}), None)
    artifact_response = _FakeResponse(payload, api_uri)
    opener = _FakeOpener([homepage, transient, artifact_response])
    sleeps: list[float] = []
    client = NSEHttpClient(
        opener=cast(Any, opener),
        maximum_attempts=2,
        clock=lambda: RETRIEVED_AT,
        sleeper=sleeps.append,
    )
    artifact = client.acquire(api_uri)

    assert artifact.payload == payload
    assert artifact.retrieved_at == RETRIEVED_AT
    assert artifact.retrieved_at.tzinfo is UTC
    assert len(opener.requests) == 3
    assert sleeps == [1.0]
    assert opener.requests[0].full_url == "https://www.nseindia.com/"

    archive_response = _FakeResponse(payload, UNIVERSE_URI)
    archive_opener = _FakeOpener([archive_response])
    archive_client = NSEHttpClient(opener=cast(Any, archive_opener), clock=lambda: RETRIEVED_AT)
    assert archive_client.acquire(UNIVERSE_URI).payload == payload
    assert len(archive_opener.requests) == 1

    oversized = _FakeResponse(b"12345", UNIVERSE_URI, content_length=5)
    bounded = NSEHttpClient(
        opener=cast(Any, _FakeOpener([oversized])),
        maximum_response_bytes=4,
        maximum_attempts=1,
    )
    with pytest.raises(NSEArtifactTooLargeError):
        bounded.acquire(UNIVERSE_URI, warm_up=False)

    network = NSEHttpClient(
        opener=cast(Any, _FakeOpener([URLError("offline"), URLError("offline")])),
        maximum_attempts=2,
        sleeper=lambda _: None,
    )
    with pytest.raises(NSEAcquisitionError, match="after 2 attempts"):
        network.acquire(UNIVERSE_URI, warm_up=False)

    timeout_client = NSEHttpClient(
        opener=cast(Any, _FakeOpener([TimeoutError("timed out")])),
        maximum_attempts=1,
    )
    with pytest.raises(NSEAcquisitionError, match="after 1 attempts"):
        timeout_client.acquire(UNIVERSE_URI, warm_up=False)


def test_structural_headers_fail_closed_and_missing_source_is_not_empty() -> None:
    with pytest.raises(NSEFormatError, match="missing required headers"):
        NSEUniverseProvider(
            _StaticSource(UNIVERSE_URI, b"SYMBOL,SERIES\nX,EQ\n"),
            _metadata(NSE_UNIVERSE_DATASET_CODE),
        ).fetch_universe()
    with pytest.raises(NSEFormatError, match="missing required headers"):
        NSEBenchmarkDataProvider(
            _StaticSource(BENCHMARK_URI, b"Index Name,Index Date\nX,30-09-2026\n"),
            _metadata(NSE_BENCHMARK_DATASET_CODE),
            TRADE_DATE,
        ).fetch_benchmark_data()
    summary = _failure_summary(
        "market", NSESourceNotAvailableError("requested artifact absent")
    )
    assert summary["status"] == "source_not_available"
    assert summary["records_received"] == 0


def test_universe_filter_mapping_quarantine_idempotency_and_metadata(
    session, tmp_path: Path
) -> None:
    payload = (FIXTURES / "nse_equity_master_synthetic.csv").read_bytes()
    provider = NSEUniverseProvider(
        _StaticSource(UNIVERSE_URI, payload),
        _metadata(NSE_UNIVERSE_DATASET_CODE),
    )
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    first = service.ingest_universe(provider)
    second = service.ingest_universe(
        NSEUniverseProvider(
            _StaticSource(UNIVERSE_URI, payload),
            _metadata(NSE_UNIVERSE_DATASET_CODE),
        )
    )

    company = session.scalar(select(Company).where(Company.legal_name.like("Kalpana%")))
    provider_row = session.scalar(select(DataProvider).where(DataProvider.code == "nse_official"))
    dataset = session.scalar(
        select(ProviderDataset).where(ProviderDataset.code == NSE_UNIVERSE_DATASET_CODE)
    )
    assert first.records_received == 2
    assert (first.records_accepted, first.records_quarantined) == (1, 1)
    assert provider.skipped_rows == 1
    assert second.records_duplicated == 2
    assert company is not None
    assert (company.display_name, company.sector, company.industry) == (
        "Kalpana Precision Limited",
        "",
        "",
    )
    security = company.securities[0]
    assert security.isin == "INE0KLP01019"
    assert security.listings[0].symbol == "KALPANA"
    assert security.listings[0].valid_from == date(2020, 1, 15)
    assert provider_row is not None and provider_row.code != "synthetic_csv"
    assert dataset is not None
    assert dataset.licence_class == "official-source-terms-reviewed-locally"
    assert session.scalar(select(func.count()).select_from(DataQualityIssue)) >= 1


def test_market_zip_raw_archive_decimal_boundaries_pit_and_revision(
    session, tmp_path: Path
) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    universe_payload = (FIXTURES / "nse_equity_master_synthetic.csv").read_bytes()
    service.ingest_universe(
        NSEUniverseProvider(
            _StaticSource(UNIVERSE_URI, universe_payload),
            _metadata(NSE_UNIVERSE_DATASET_CODE),
        )
    )
    csv_payload = (FIXTURES / "nse_udiff_synthetic.csv").read_bytes()
    zip_payload = _zip_market(csv_payload)
    provider = NSEMarketDataProvider(
        _StaticSource(MARKET_URI, zip_payload),
        _metadata(NSE_MARKET_DATASET_CODE),
        TRADE_DATE,
    )
    first = service.ingest_market_data(provider)
    duplicate = service.ingest_market_data(
        NSEMarketDataProvider(
            _StaticSource(MARKET_URI, zip_payload),
            _metadata(NSE_MARKET_DATASET_CODE),
            TRADE_DATE,
        )
    )

    bar = session.scalar(select(PriceBar))
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id.like("nse-cm:%"))
    )
    assert first.records_accepted == 1 and provider.skipped_rows == 2
    assert duplicate.records_duplicated == 1
    assert bar is not None and source is not None
    assert (bar.open_price, bar.high_price, bar.low_price, bar.close_price) == (
        Decimal("101.125"),
        Decimal("105.75"),
        Decimal("99.50"),
        Decimal("104.625"),
    )
    assert bar.volume == 123456
    assert bar.market_cap is None
    assert bar.delivery_quantity is None and bar.delivery_percentage is None
    assert bar.trading_date == TRADE_DATE
    assert _as_utc(bar.available_at) == RETRIEVED_AT
    assert _as_utc(bar.available_at) != datetime(2026, 9, 30, tzinfo=UTC)
    assert source.raw_payload_reference == f"{nse_market_filename(TRADE_DATE)}#row-2"
    assert (tmp_path / "raw" / source.raw_object_key).read_bytes() == zip_payload

    corrected_csv = csv_payload.replace(b"104.625,104.625", b"103.625,103.625")
    corrected = service.ingest_market_data(
        NSEMarketDataProvider(
            _StaticSource(MARKET_URI, _zip_market(corrected_csv), LATER_RETRIEVED_AT),
            _metadata(NSE_MARKET_DATASET_CODE),
            TRADE_DATE,
        )
    )
    bars = tuple(session.scalars(select(PriceBar).order_by(PriceBar.available_at)))
    assert corrected.records_accepted == 1
    assert [item.close_price for item in bars] == [Decimal("104.625"), Decimal("103.625")]
    assert bars[1].revision_at is None


def test_udiff_zip_safety_and_strict_headers() -> None:
    metadata = _metadata(NSE_MARKET_DATASET_CODE)
    malformed = (FIXTURES / "nse_udiff_malformed_synthetic.zip").read_bytes()
    with pytest.raises(NSEFormatError, match="valid ZIP"):
        NSEMarketDataProvider(
            _StaticSource(MARKET_URI, malformed), metadata, TRADE_DATE
        ).fetch_market_data()

    csv_payload = (FIXTURES / "nse_udiff_synthetic.csv").read_bytes()
    traversal = _zip_market(csv_payload, member="../" + nse_market_filename(TRADE_DATE))
    with pytest.raises(NSEFormatError, match="unsafe"):
        NSEMarketDataProvider(
            _StaticSource(MARKET_URI, traversal), metadata, TRADE_DATE
        ).fetch_market_data()

    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(nse_market_filename(TRADE_DATE), csv_payload)
        archive.writestr("nested/" + nse_market_filename(TRADE_DATE), csv_payload)
    with pytest.raises(NSEFormatError, match="exactly one"):
        NSEMarketDataProvider(
            _StaticSource(MARKET_URI, stream.getvalue()), metadata, TRADE_DATE
        ).fetch_market_data()

    bad_headers = _zip_market(b"TradDt,ISIN\n2026-09-30,INE0KLP01019\n")
    with pytest.raises(NSEFormatError, match="missing required headers"):
        NSEMarketDataProvider(
            _StaticSource(MARKET_URI, bad_headers), metadata, TRADE_DATE
        ).fetch_market_data()


def test_benchmarks_ingest_all_rows_decimal_observed_time_and_idempotency(
    session, tmp_path: Path
) -> None:
    payload = (FIXTURES / "nse_indices_synthetic.csv").read_bytes()
    provider = NSEBenchmarkDataProvider(
        _StaticSource(BENCHMARK_URI, payload),
        _metadata(NSE_BENCHMARK_DATASET_CODE),
        TRADE_DATE,
    )
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    first = service.ingest_benchmark_data(provider)
    duplicate = service.ingest_benchmark_data(
        NSEBenchmarkDataProvider(
            _StaticSource(BENCHMARK_URI, payload),
            _metadata(NSE_BENCHMARK_DATASET_CODE),
            TRADE_DATE,
        )
    )

    series = tuple(session.scalars(select(BenchmarkSeries).order_by(BenchmarkSeries.code)))
    bars = tuple(session.scalars(select(BenchmarkBar).order_by(BenchmarkBar.open_value)))
    assert (first.records_accepted, first.records_quarantined) == (2, 1)
    assert duplicate.records_duplicated == 3
    assert {item.code for item in series} == {
        "Fictional Broad 50",
        "Fictional Industrial 20",
    }
    assert len(bars) == 2
    assert bars[1].open_value == Decimal("25000.125")
    assert all(_as_utc(item.available_at) == RETRIEVED_AT for item in bars)
    assert all(item.available_at.date() != item.trading_date for item in bars)


def test_local_file_requires_source_uri_and_preserves_observation_time(tmp_path: Path) -> None:
    path = tmp_path / "official.csv"
    path.write_bytes(b"official")
    with pytest.raises(ValueError, match="source_uri"):
        LocalNSEArtifactSource(path, " ")
    source = LocalNSEArtifactSource(path, UNIVERSE_URI, lambda: RETRIEVED_AT)
    artifact = source.acquire()
    assert artifact.payload == b"official"
    assert artifact.source_uri == UNIVERSE_URI
    assert artifact.retrieved_at == RETRIEVED_AT


def test_production_preflight_rejects_synthetic_without_mutation(session) -> None:
    production_preflight(session, "reviewed-source-terms")
    assert seed_database(session) == 3
    before = session.scalar(select(func.count()).select_from(Company))
    with pytest.raises(ProductionPreflightError, match="synthetic-development"):
        production_preflight(session, "reviewed-source-terms")
    after = session.scalar(select(func.count()).select_from(Company))
    assert before == after == 3
    assert session.scalar(select(func.count()).select_from(DataProvider)) == 0


def test_production_preflight_rejects_provider_and_licence_markers(session) -> None:
    synthetic = DataProvider(
        code="synthetic_csv",
        provider_type="csv",
        licence_name="synthetic-development-only",
    )
    session.add(synthetic)
    session.commit()
    before = session.scalar(select(func.count()).select_from(DataProvider))
    with pytest.raises(ProductionPreflightError, match="synthetic-development"):
        production_preflight(session, "reviewed-source-terms")
    assert session.scalar(select(func.count()).select_from(DataProvider)) == before == 1


def test_daily_cli_local_official_pipeline_json_and_clean_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "production.db"
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    market_zip = tmp_path / "market.zip"
    market_zip.write_bytes(_zip_market((FIXTURES / "nse_udiff_synthetic.csv").read_bytes()))

    exit_code = main(
        [
            "ingest-daily",
            "--date",
            TRADE_DATE.isoformat(),
            "--database-url",
            f"sqlite+pysqlite:///{database_path}",
            "--raw-root",
            str(tmp_path / "production-raw"),
            "--license-class",
            "reviewed-source-terms",
            "--universe-file",
            str(FIXTURES / "nse_equity_master_synthetic.csv"),
            "--universe-source-uri",
            UNIVERSE_URI,
            "--market-file",
            str(market_zip),
            "--market-source-uri",
            MARKET_URI,
            "--benchmark-file",
            str(FIXTURES / "nse_indices_synthetic.csv"),
            "--benchmark-source-uri",
            BENCHMARK_URI,
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    assert exit_code == 0 and summary["status"] == "completed"
    assert [stage["stage"] for stage in summary["stages"]] == [
        "universe",
        "market",
        "benchmarks",
    ]
    assert all(stage["run_id"] for stage in summary["stages"])
    assert summary["stages"][0]["provider_skipped_rows"] == 1
    assert summary["stages"][1]["provider_skipped_rows"] == 2

    verification_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with sessionmaker(bind=verification_engine)() as verification:
        assert verification.scalar(select(func.count()).select_from(PriceBar)) == 1
        assert verification.scalar(select(func.count()).select_from(BenchmarkBar)) == 2
        datasets = set(verification.scalars(select(ProviderDataset.code)))
        assert datasets == {
            NSE_UNIVERSE_DATASET_CODE,
            NSE_MARKET_DATASET_CODE,
            NSE_BENCHMARK_DATASET_CODE,
        }
    verification_engine.dispose()


def test_provider_modules_keep_database_boundary() -> None:
    source_path = Path(__file__).parents[1] / "packages/data/inflector_data/nse_providers.py"
    source = source_path.read_text()
    forbidden = ("sqlalchemy", "inflector_database", "ScoreSnapshot", "ComponentScorer")
    assert all(name not in source for name in forbidden)
