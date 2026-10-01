"""Database-free adapters for official NSE universe, UDiFF, and index artifacts."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from typing import Protocol
from zipfile import BadZipFile, ZipFile

from inflector_core.providers import (
    BenchmarkBarRecord,
    IngestionEnvelope,
    MarketBarRecord,
    ProviderBatch,
    ProviderMetadata,
    UniverseRecord,
)
from inflector_data.nse_http import AcquiredNSEArtifact, NSEHttpClient, validate_nse_url

NSE_EQUITY_UNIVERSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_UNIVERSE_DATASET_CODE = "nse_equity_universe"
NSE_MARKET_DATASET_CODE = "nse_cash_market_udiff_daily"
NSE_BENCHMARK_DATASET_CODE = "nse_indices_daily"
MAXIMUM_UDIFF_DECOMPRESSED_BYTES = 75_000_000

_UNIVERSE_HEADERS = frozenset(
    {"SYMBOL", "NAME OF COMPANY", "SERIES", "DATE OF LISTING", "ISIN NUMBER"}
)
_MARKET_HEADERS = frozenset(
    {
        "TradDt",
        "Sgmt",
        "FinInstrmTp",
        "ISIN",
        "TckrSymb",
        "SctySrs",
        "FinInstrmNm",
        "OpnPric",
        "HghPric",
        "LwPric",
        "ClsPric",
        "TtlTradgVol",
    }
)
_BENCHMARK_HEADERS = frozenset(
    {
        "Index Name",
        "Index Date",
        "Open Index Value",
        "High Index Value",
        "Low Index Value",
        "Closing Index Value",
    }
)
_NSE_ENGLISH_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_NSE_LISTING_DATE_PATTERN = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


class NSEFormatError(ValueError):
    """An official artifact is structurally unsafe or incompatible."""


class NSEArtifactSource(Protocol):
    """Acquisition boundary shared by live HTTPS and explicit local recovery."""

    @property
    def source_uri(self) -> str: ...

    def acquire(self) -> AcquiredNSEArtifact: ...


@dataclass(frozen=True, slots=True)
class HttpNSEArtifactSource:
    client: NSEHttpClient
    source_uri: str

    def acquire(self) -> AcquiredNSEArtifact:
        return self.client.acquire(self.source_uri)


@dataclass(frozen=True, slots=True)
class LocalNSEArtifactSource:
    """Read explicitly downloaded official bytes without relabelling them synthetic."""

    path: Path
    source_uri: str
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if not self.source_uri.strip():
            raise ValueError("source_uri is required for a local official artifact")
        validate_nse_url(self.source_uri)

    def acquire(self) -> AcquiredNSEArtifact:
        payload = self.path.read_bytes()
        retrieved_at = self.clock()
        if retrieved_at.tzinfo is None:
            raise ValueError("local artifact retrieval clock must be timezone-aware")
        return AcquiredNSEArtifact(
            self.source_uri,
            payload,
            retrieved_at.astimezone(UTC),
        )


def nse_market_url(trading_date: date) -> str:
    filename = nse_market_filename(trading_date)
    return f"https://nsearchives.nseindia.com/content/cm/{filename}.zip"


def nse_market_filename(trading_date: date) -> str:
    return f"BhavCopy_NSE_CM_0_0_0_{trading_date:%Y%m%d}_F_0000.csv"


def nse_benchmark_url(trading_date: date) -> str:
    return (
        "https://nsearchives.nseindia.com/content/indices/"
        f"ind_close_all_{trading_date:%d%m%Y}.csv"
    )


class _ProviderStats:
    def __init__(self, source_uri: str) -> None:
        self._skipped_rows = 0
        self._source_uri: str | None = source_uri
        self._retrieved_at: datetime | None = None

    @property
    def skipped_rows(self) -> int:
        return self._skipped_rows

    @property
    def source_uri(self) -> str | None:
        return self._source_uri

    @property
    def retrieved_at(self) -> datetime | None:
        return self._retrieved_at

    def _observe(self, artifact: AcquiredNSEArtifact) -> None:
        self._source_uri = artifact.source_uri
        self._retrieved_at = artifact.retrieved_at


class NSEUniverseProvider(_ProviderStats):
    """Current NSE EQ-series master mapped only from source-supported fields."""

    def __init__(self, source: NSEArtifactSource, metadata: ProviderMetadata) -> None:
        super().__init__(source.source_uri)
        self._source = source
        self._metadata = metadata

    def fetch_universe(self) -> ProviderBatch[UniverseRecord]:
        artifact = self._source.acquire()
        self._observe(artifact)
        rows = _csv_rows(artifact.payload, _UNIVERSE_HEADERS)
        records: list[IngestionEnvelope[UniverseRecord]] = []
        skipped = 0
        for row_number, row in rows:
            if row.get("SERIES", "").strip() != "EQ":
                skipped += 1
                continue
            errors = _row_shape_errors(row)
            listing_date = _nse_listing_date(
                row.get("DATE OF LISTING", ""), errors, "invalid_listing_date"
            )
            symbol = row.get("SYMBOL", "").strip()
            isin = row.get("ISIN NUMBER", "").strip()
            company_name = row.get("NAME OF COMPANY", "").strip()
            record = UniverseRecord(
                legal_name=company_name,
                display_name=company_name,
                sector="",
                industry="",
                isin=isin,
                security_type="equity",
                security_status="active",
                exchange="NSE",
                symbol=symbol,
                listing_status="active",
                valid_from=listing_date,
                valid_to=None,
                parse_errors=tuple(errors),
            )
            records.append(
                _envelope(
                    artifact,
                    self._metadata,
                    external_record_id=f"nse-universe:{isin or symbol or row_number}:EQ",
                    raw_reference=f"row-{row_number}",
                    row=row,
                    record=record,
                    available_at=artifact.retrieved_at,
                )
            )
        self._skipped_rows = skipped
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            tuple(records),
        )


class NSEMarketDataProvider(_ProviderStats):
    """CM UDiFF Final daily ZIP adapter for regular EQ-series stock rows only."""

    def __init__(
        self,
        source: NSEArtifactSource,
        metadata: ProviderMetadata,
        trading_date: date,
        *,
        maximum_decompressed_bytes: int = MAXIMUM_UDIFF_DECOMPRESSED_BYTES,
    ) -> None:
        super().__init__(source.source_uri)
        self._source = source
        self._metadata = metadata
        self._trading_date = trading_date
        self._maximum_decompressed_bytes = maximum_decompressed_bytes

    def fetch_market_data(self) -> ProviderBatch[MarketBarRecord]:
        artifact = self._source.acquire()
        self._observe(artifact)
        member_name, csv_payload = _extract_udiff_member(
            artifact.payload,
            nse_market_filename(self._trading_date),
            self._maximum_decompressed_bytes,
        )
        rows = _csv_rows(csv_payload, _MARKET_HEADERS)
        records: list[IngestionEnvelope[MarketBarRecord]] = []
        skipped = 0
        for row_number, row in rows:
            if not _supported_market_row(row):
                skipped += 1
                continue
            errors = _row_shape_errors(row)
            observed_date = _date_value(
                row.get("TradDt", ""), errors, "invalid_date", "%Y-%m-%d"
            )
            if observed_date is not None and observed_date != self._trading_date:
                errors.append("unexpected_trading_date")
            volume = _integer_value(row.get("TtlTradgVol", ""), errors, "invalid_volume")
            record = MarketBarRecord(
                security_isin=row.get("ISIN", "").strip() or None,
                trading_date=observed_date,
                interval="1d",
                open_price=_decimal_value(row.get("OpnPric", ""), errors, "invalid_open"),
                high_price=_decimal_value(row.get("HghPric", ""), errors, "invalid_high"),
                low_price=_decimal_value(row.get("LwPric", ""), errors, "invalid_low"),
                close_price=_decimal_value(row.get("ClsPric", ""), errors, "invalid_close"),
                volume=volume,
                parse_errors=tuple(errors),
                market_cap=None,
                delivery_quantity=None,
                delivery_percentage=None,
            )
            isin = row.get("ISIN", "").strip()
            records.append(
                _envelope(
                    artifact,
                    self._metadata,
                    external_record_id=f"nse-cm:{self._trading_date.isoformat()}:{isin}:EQ",
                    raw_reference=f"{member_name}#row-{row_number}",
                    row=row,
                    record=record,
                    available_at=artifact.retrieved_at,
                )
            )
        self._skipped_rows = skipped
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            tuple(records),
        )


class NSEBenchmarkDataProvider(_ProviderStats):
    """All structurally complete rows from the official NSE daily index snapshot."""

    def __init__(
        self, source: NSEArtifactSource, metadata: ProviderMetadata, trading_date: date
    ) -> None:
        super().__init__(source.source_uri)
        self._source = source
        self._metadata = metadata
        self._trading_date = trading_date

    def fetch_benchmark_data(self) -> ProviderBatch[BenchmarkBarRecord]:
        artifact = self._source.acquire()
        self._observe(artifact)
        rows = _csv_rows(artifact.payload, _BENCHMARK_HEADERS)
        records: list[IngestionEnvelope[BenchmarkBarRecord]] = []
        for row_number, row in rows:
            errors = _row_shape_errors(row)
            benchmark_name = row.get("Index Name", "").strip()
            observed_date = _date_value(
                row.get("Index Date", ""), errors, "invalid_date", "%d-%m-%Y"
            )
            if observed_date is not None and observed_date != self._trading_date:
                errors.append("unexpected_trading_date")
            record = BenchmarkBarRecord(
                benchmark_code=benchmark_name or None,
                benchmark_display_name=benchmark_name or None,
                currency="INR",
                trading_date=observed_date,
                interval="1d",
                open_value=_decimal_value(
                    row.get("Open Index Value", ""), errors, "invalid_open"
                ),
                high_value=_decimal_value(
                    row.get("High Index Value", ""), errors, "invalid_high"
                ),
                low_value=_decimal_value(
                    row.get("Low Index Value", ""), errors, "invalid_low"
                ),
                close_value=_decimal_value(
                    row.get("Closing Index Value", ""), errors, "invalid_close"
                ),
                parse_errors=tuple(errors),
            )
            records.append(
                _envelope(
                    artifact,
                    self._metadata,
                    external_record_id=(
                        f"nse-index:{self._trading_date.isoformat()}:{benchmark_name or row_number}"
                    ),
                    raw_reference=f"row-{row_number}",
                    row=row,
                    record=record,
                    available_at=artifact.retrieved_at,
                )
            )
        self._skipped_rows = 0
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            tuple(records),
        )


def _csv_rows(payload: bytes, required_headers: frozenset[str]) -> list[tuple[int, dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NSEFormatError("official NSE CSV is not UTF-8 compatible") from error
    reader = csv.reader(StringIO(text, newline=""))
    try:
        raw_headers = next(reader)
    except StopIteration as error:
        raise NSEFormatError("official NSE CSV is empty") from error
    headers = [header.strip() for header in raw_headers]
    if len(headers) != len(set(headers)):
        raise NSEFormatError("official NSE CSV has duplicate normalized headers")
    missing = required_headers.difference(headers)
    if missing:
        raise NSEFormatError(f"official NSE CSV is missing required headers: {sorted(missing)}")
    rows: list[tuple[int, dict[str, str]]] = []
    for row_number, values in enumerate(reader, start=2):
        if not values or not any(value.strip() for value in values):
            continue
        if len(values) != len(headers):
            padded = values[: len(headers)] + [""] * max(0, len(headers) - len(values))
            row = dict(zip(headers, padded, strict=True))
            row["__row_shape_error__"] = "invalid_csv_row_shape"
        else:
            row = dict(zip(headers, values, strict=True))
        rows.append((row_number, row))
    return rows


def _supported_market_row(row: dict[str, str]) -> bool:
    return (
        row.get("Sgmt", "").strip() == "CM"
        and row.get("FinInstrmTp", "").strip() == "STK"
        and row.get("SctySrs", "").strip() == "EQ"
        and bool(row.get("ISIN", "").strip())
    )


def _extract_udiff_member(
    payload: bytes, expected_member: str, maximum_decompressed_bytes: int
) -> tuple[str, bytes]:
    try:
        with ZipFile(BytesIO(payload)) as archive:
            candidates = []
            total_size = 0
            for info in archive.infolist():
                path = PurePosixPath(info.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise NSEFormatError("UDiFF archive contains an unsafe member path")
                if info.is_dir():
                    continue
                total_size += info.file_size
                if total_size > maximum_decompressed_bytes:
                    raise NSEFormatError("UDiFF archive exceeds decompressed byte limit")
                if path.name == expected_member:
                    candidates.append(info)
            if len(candidates) != 1:
                raise NSEFormatError("UDiFF archive does not contain exactly one expected CSV")
            info = candidates[0]
            extracted = archive.read(info)
            if len(extracted) != info.file_size or len(extracted) > maximum_decompressed_bytes:
                raise NSEFormatError("UDiFF member size is inconsistent or exceeds the limit")
            return info.filename, extracted
    except BadZipFile as error:
        raise NSEFormatError("UDiFF artifact is not a valid ZIP archive") from error


def _date_value(raw: str, errors: list[str], code: str, format_code: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), format_code).date()
    except ValueError:
        errors.append(code)
        return None


def _nse_listing_date(raw: str, errors: list[str], code: str) -> date | None:
    match = _NSE_LISTING_DATE_PATTERN.fullmatch(raw.strip())
    if match is None:
        errors.append(code)
        return None
    day_text, month_text, year_text = match.groups()
    month = _NSE_ENGLISH_MONTHS.get(month_text.upper())
    if month is None:
        errors.append(code)
        return None
    try:
        return date(int(year_text), month, int(day_text))
    except ValueError:
        errors.append(code)
        return None


def _decimal_value(raw: str, errors: list[str], code: str) -> Decimal | None:
    value = raw.strip()
    if not value:
        errors.append(code)
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        errors.append(code)
        return None


def _integer_value(raw: str, errors: list[str], code: str) -> int | None:
    try:
        value = Decimal(raw.strip())
        if value != value.to_integral_value():
            raise ValueError
        return int(value)
    except (InvalidOperation, ValueError):
        errors.append(code)
        return None


def _envelope[TRecord](
    artifact: AcquiredNSEArtifact,
    metadata: ProviderMetadata,
    *,
    external_record_id: str,
    raw_reference: str,
    row: dict[str, str],
    record: TRecord,
    available_at: datetime,
) -> IngestionEnvelope[TRecord]:
    semantic_row = {key: value for key, value in row.items() if not key.startswith("__")}
    content_hash = sha256(
        json.dumps(semantic_row, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return IngestionEnvelope(
        provider=metadata,
        external_record_id=external_record_id,
        source_uri=artifact.source_uri,
        raw_payload_reference=raw_reference,
        content_sha256=content_hash,
        retrieved_at=artifact.retrieved_at,
        available_at=available_at,
        revision_at=None,
        record=record,
    )


def _row_shape_errors(row: dict[str, str]) -> list[str]:
    value = row.get("__row_shape_error__")
    return [value] if value is not None else []
