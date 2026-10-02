"""Official NSE full-bhavcopy delivery adapter with exact observed availability."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO

from inflector_core.providers import (
    IngestionEnvelope,
    MarketDeliveryRecord,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.nse_providers import NSEArtifactSource

NSE_DELIVERY_DATASET_CODE = "nse_cash_market_delivery_daily"
NSE_DELIVERY_REPORT_FAMILY = "full_bhavcopy_security_deliverable_v1"
NSE_DELIVERY_HEADERS = frozenset(
    {"SYMBOL", "SERIES", "DATE1", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"}
)
_MISSING = frozenset({"", "-", "NA", "N.A.", "N/A"})


class NSEDeliveryFormatError(ValueError):
    """The official delivery artifact is structurally unsafe or incompatible."""


def nse_delivery_url(trading_date: date) -> str:
    return (
        "https://nsearchives.nseindia.com/products/content/"
        f"sec_bhavdata_full_{trading_date:%d%m%Y}.csv"
    )


class NSEMarketDeliveryProvider:
    """Current official Full Bhavcopy delivery rows for known NSE EQ identities."""

    def __init__(
        self,
        source: NSEArtifactSource,
        metadata: ProviderMetadata,
        trading_date: date,
        identities: Mapping[str, str],
    ) -> None:
        self._source = source
        self._metadata = metadata
        self._trading_date = trading_date
        self._identities = {
            symbol.strip().upper(): isin.strip() for symbol, isin in identities.items()
        }
        self._source_uri: str | None = source.source_uri
        self._retrieved_at: datetime | None = None
        self._skipped_rows = 0

    @property
    def source_uri(self) -> str | None:
        return self._source_uri

    @property
    def retrieved_at(self) -> datetime | None:
        return self._retrieved_at

    @property
    def skipped_rows(self) -> int:
        return self._skipped_rows

    def fetch_market_delivery(self) -> ProviderBatch[MarketDeliveryRecord]:
        artifact = self._source.acquire()
        self._source_uri = artifact.source_uri
        self._retrieved_at = artifact.retrieved_at
        rows = _rows(artifact.payload)
        records: list[IngestionEnvelope[MarketDeliveryRecord]] = []
        skipped = 0
        for row_number, row in rows:
            symbol = row.get("SYMBOL", "").strip().upper()
            series = row.get("SERIES", "").strip().upper()
            isin = self._identities.get(symbol)
            if series != "EQ" or isin is None:
                skipped += 1
                continue
            errors = [row["__row_shape_error__"]] if "__row_shape_error__" in row else []
            observed_date = _date(row.get("DATE1", ""), errors)
            if observed_date is not None and observed_date != self._trading_date:
                errors.append("unexpected_trading_date")
            traded = _integer(row.get("TTL_TRD_QNTY", ""), errors, "invalid_traded_quantity")
            delivered = _integer(row.get("DELIV_QTY", ""), errors, "invalid_delivery_quantity")
            reported = _decimal(row.get("DELIV_PER", ""), errors, "invalid_delivery_percentage")
            ratio = reported / Decimal("100") if reported is not None else None
            if (
                traded is not None
                and traded != 0
                and delivered is not None
                and reported is not None
            ):
                calculated = Decimal(delivered) * Decimal("100") / Decimal(traded)
                if abs(calculated - reported) > Decimal("0.01"):
                    errors.append("incoherent_delivery_percentage")
            record = MarketDeliveryRecord(
                security_isin=isin,
                symbol=symbol,
                series=series,
                trading_date=observed_date,
                total_traded_quantity=traded,
                delivery_quantity=delivered,
                reported_delivery_percentage=reported,
                delivery_percentage=ratio,
                parse_errors=tuple(errors),
            )
            semantic_row = {key: value for key, value in row.items() if not key.startswith("__")}
            records.append(
                IngestionEnvelope(
                    provider=self._metadata,
                    external_record_id=(
                        f"nse-delivery:{self._trading_date.isoformat()}:{symbol}:{series}"
                    ),
                    source_uri=artifact.source_uri,
                    raw_payload_reference=f"row-{row_number}:{symbol}:{series}",
                    content_sha256=sha256(
                        json.dumps(semantic_row, sort_keys=True, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    retrieved_at=artifact.retrieved_at,
                    available_at=artifact.retrieved_at,
                    revision_at=None,
                    record=record,
                )
            )
        self._skipped_rows = skipped
        return ProviderBatch(
            provider=self._metadata,
            source_uri=artifact.source_uri,
            raw_payload=artifact.payload,
            retrieved_at=artifact.retrieved_at,
            records=tuple(records),
        )


def _rows(payload: bytes) -> list[tuple[int, dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NSEDeliveryFormatError("official NSE delivery CSV is not UTF-8 compatible") from error
    reader = csv.reader(StringIO(text, newline=""))
    try:
        raw_headers = next(reader)
    except StopIteration as error:
        raise NSEDeliveryFormatError("official NSE delivery CSV is empty") from error
    headers = [value.strip() for value in raw_headers]
    if len(headers) != len(set(headers)):
        raise NSEDeliveryFormatError("official NSE delivery CSV has duplicate headers")
    missing = NSE_DELIVERY_HEADERS.difference(headers)
    if missing:
        raise NSEDeliveryFormatError(
            f"official NSE delivery CSV is missing headers: {sorted(missing)}"
        )
    result: list[tuple[int, dict[str, str]]] = []
    for row_number, values in enumerate(reader, start=2):
        if not values or not any(value.strip() for value in values):
            continue
        padded = values[: len(headers)] + [""] * max(0, len(headers) - len(values))
        row = dict(zip(headers, padded, strict=True))
        if len(values) != len(headers):
            row["__row_shape_error__"] = "invalid_csv_row_shape"
        result.append((row_number, row))
    return result


def _date(raw: str, errors: list[str]) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").date()
    except ValueError:
        errors.append("invalid_trading_date")
        return None


def _integer(raw: str, errors: list[str], code: str) -> int | None:
    value = raw.strip().upper()
    if value in _MISSING:
        return None
    try:
        parsed = Decimal(value)
        if parsed != parsed.to_integral_value():
            raise ValueError
        return int(parsed)
    except (InvalidOperation, ValueError):
        errors.append(code)
        return None


def _decimal(raw: str, errors: list[str], code: str) -> Decimal | None:
    value = raw.strip().upper()
    if value in _MISSING:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        errors.append(code)
        return None
