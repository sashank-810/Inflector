"""CSV and in-memory adapters that implement the provider ports."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
from pathlib import Path

from inflector_core.providers import (
    CorporateActionRecord,
    FinancialRecord,
    IngestionEnvelope,
    MarketBarRecord,
    ProviderBatch,
    ProviderMetadata,
    UniverseRecord,
)


def _row_hash(row: dict[str, str]) -> str:
    return sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _optional_datetime(value: str, errors: list[str], field_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError:
        errors.append(f"invalid_{field_name}")
        return None


def _optional_date(value: str, errors: list[str], field_name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"invalid_{field_name}")
        return None


def _optional_decimal(value: str, errors: list[str], field_name: str) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        errors.append(f"invalid_{field_name}")
        return None


def _optional_int(value: str, errors: list[str], field_name: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        errors.append(f"invalid_{field_name}")
        return None


@dataclass(frozen=True, slots=True)
class CSVUniverseProvider:
    """Development-only universe adapter with no database dependency."""

    path: Path
    metadata: ProviderMetadata
    retrieved_at: datetime

    def fetch_universe(self) -> ProviderBatch[UniverseRecord]:
        raw_payload = self.path.read_bytes()
        records: list[IngestionEnvelope[UniverseRecord]] = []
        for index, row in enumerate(
            csv.DictReader(StringIO(raw_payload.decode("utf-8-sig"))), start=1
        ):
            errors: list[str] = []
            valid_from = _optional_date(row.get("valid_from", ""), errors, "valid_from")
            valid_to = _optional_date(row.get("valid_to", ""), errors, "valid_to")
            record = UniverseRecord(
                legal_name=row.get("legal_name", ""),
                display_name=row.get("display_name", ""),
                sector=row.get("sector", ""),
                industry=row.get("industry", ""),
                isin=row.get("isin", ""),
                security_type=row.get("security_type", "equity"),
                security_status=row.get("security_status", "active"),
                exchange=row.get("exchange", ""),
                symbol=row.get("symbol", ""),
                listing_status=row.get("listing_status", "active"),
                valid_from=valid_from,
                valid_to=valid_to,
                parse_errors=tuple(errors),
            )
            records.append(
                IngestionEnvelope(
                    provider=self.metadata,
                    external_record_id=row.get("external_id") or f"row-{index}",
                    source_uri=f"file://{self.path.name}",
                    raw_payload_reference=f"row-{index}",
                    content_sha256=_row_hash(row),
                    retrieved_at=self.retrieved_at,
                    record=record,
                )
            )
        return ProviderBatch(
            self.metadata,
            f"file://{self.path.name}",
            raw_payload,
            self.retrieved_at,
            tuple(records),
        )


@dataclass(frozen=True, slots=True)
class CSVMarketDataProvider:
    """Development-only OHLCV adapter retaining parse errors for quarantine."""

    path: Path
    metadata: ProviderMetadata
    retrieved_at: datetime

    def fetch_market_data(self) -> ProviderBatch[MarketBarRecord]:
        raw_payload = self.path.read_bytes()
        records: list[IngestionEnvelope[MarketBarRecord]] = []
        for index, row in enumerate(
            csv.DictReader(StringIO(raw_payload.decode("utf-8-sig"))), start=1
        ):
            errors: list[str] = []
            trading_date = _optional_date(row.get("trading_date", ""), errors, "date")
            available_at = _optional_datetime(row.get("available_at", ""), errors, "available_at")
            revision_at = _optional_datetime(row.get("revision_at", ""), errors, "revision_at")
            record = MarketBarRecord(
                security_isin=row.get("security_isin") or None,
                trading_date=trading_date,
                interval=row.get("interval") or None,
                open_price=_optional_decimal(row.get("open", ""), errors, "open"),
                high_price=_optional_decimal(row.get("high", ""), errors, "high"),
                low_price=_optional_decimal(row.get("low", ""), errors, "low"),
                close_price=_optional_decimal(row.get("close", ""), errors, "close"),
                volume=_optional_int(row.get("volume", ""), errors, "volume"),
                parse_errors=tuple(errors),
            )
            records.append(
                IngestionEnvelope(
                    provider=self.metadata,
                    external_record_id=row.get("external_id") or f"row-{index}",
                    source_uri=f"file://{self.path.name}",
                    raw_payload_reference=f"row-{index}",
                    content_sha256=_row_hash(row),
                    retrieved_at=self.retrieved_at,
                    record=record,
                    available_at=available_at,
                    revision_at=revision_at,
                )
            )
        return ProviderBatch(
            self.metadata,
            f"file://{self.path.name}",
            raw_payload,
            self.retrieved_at,
            tuple(records),
        )


@dataclass(frozen=True, slots=True)
class CSVFinancialsProvider:
    """Development-only financial-fact adapter retaining parsing failures."""

    path: Path
    metadata: ProviderMetadata
    retrieved_at: datetime

    def fetch_financials(self) -> ProviderBatch[FinancialRecord]:
        raw_payload = self.path.read_bytes()
        records: list[IngestionEnvelope[FinancialRecord]] = []
        for index, row in enumerate(
            csv.DictReader(StringIO(raw_payload.decode("utf-8-sig"))), start=1
        ):
            errors: list[str] = []
            published_at = _optional_datetime(row.get("published_at", ""), errors, "published_at")
            available_at = _optional_datetime(row.get("available_at", ""), errors, "available_at")
            revision_at = _optional_datetime(row.get("revision_at", ""), errors, "revision_at")
            record = FinancialRecord(
                company_legal_name=row.get("company_legal_name") or None,
                filing_external_id=row.get("filing_external_id") or None,
                filing_type=row.get("filing_type") or None,
                filing_scope=row.get("filing_scope") or None,
                is_restatement=(row.get("is_restatement", "").lower() == "true"),
                period_kind=row.get("period_kind") or None,
                period_start=_optional_date(row.get("period_start", ""), errors, "period_start"),
                period_end=_optional_date(row.get("period_end", ""), errors, "period_end"),
                fiscal_year=_optional_int(row.get("fiscal_year", ""), errors, "fiscal_year"),
                fiscal_quarter=_optional_int(
                    row.get("fiscal_quarter", ""), errors, "fiscal_quarter"
                ),
                is_ytd=(row.get("is_ytd", "").lower() == "true"),
                metric_code=row.get("metric_code") or None,
                reported_value=_optional_decimal(
                    row.get("reported_value", ""), errors, "reported_value"
                ),
                reported_unit=row.get("reported_unit") or None,
                reported_scale=row.get("reported_scale") or None,
                reported_currency=row.get("reported_currency") or None,
                parse_errors=tuple(errors),
            )
            records.append(
                IngestionEnvelope(
                    provider=self.metadata,
                    external_record_id=row.get("external_id") or f"row-{index}",
                    source_uri=f"file://{self.path.name}",
                    raw_payload_reference=f"row-{index}",
                    content_sha256=_row_hash(row),
                    retrieved_at=self.retrieved_at,
                    record=record,
                    published_at=published_at,
                    available_at=available_at,
                    revision_at=revision_at,
                )
            )
        return ProviderBatch(
            self.metadata,
            f"file://{self.path.name}",
            raw_payload,
            self.retrieved_at,
            tuple(records),
        )


@dataclass(frozen=True, slots=True)
class CSVCorporateActionProvider:
    """Development-only corporate-action adapter with explicit typed terms."""

    path: Path
    metadata: ProviderMetadata
    retrieved_at: datetime

    def fetch_corporate_actions(self) -> ProviderBatch[CorporateActionRecord]:
        raw_payload = self.path.read_bytes()
        records: list[IngestionEnvelope[CorporateActionRecord]] = []
        for index, row in enumerate(
            csv.DictReader(StringIO(raw_payload.decode("utf-8-sig"))), start=1
        ):
            errors: list[str] = []
            available_at = _optional_datetime(row.get("available_at", ""), errors, "available_at")
            revision_at = _optional_datetime(row.get("revision_at", ""), errors, "revision_at")
            record = CorporateActionRecord(
                security_isin=row.get("security_isin") or None,
                action_type=row.get("action_type") or None,
                announcement_date=_optional_date(
                    row.get("announcement_date", ""), errors, "announcement_date"
                ),
                ex_date=_optional_date(row.get("ex_date", ""), errors, "ex_date"),
                record_date=_optional_date(row.get("record_date", ""), errors, "record_date"),
                effective_date=_optional_date(
                    row.get("effective_date", ""), errors, "effective_date"
                ),
                ratio_numerator=_optional_int(
                    row.get("ratio_numerator", ""), errors, "ratio_numerator"
                ),
                ratio_denominator=_optional_int(
                    row.get("ratio_denominator", ""), errors, "ratio_denominator"
                ),
                cash_amount=_optional_decimal(row.get("cash_amount", ""), errors, "cash_amount"),
                cash_currency=row.get("cash_currency") or None,
                cash_unit=row.get("cash_unit") or None,
                subscription_price=_optional_decimal(
                    row.get("subscription_price", ""), errors, "subscription_price"
                ),
                subscription_currency=row.get("subscription_currency") or None,
                exchange=row.get("exchange") or None,
                old_symbol=row.get("old_symbol") or None,
                new_symbol=row.get("new_symbol") or None,
                successor_isin=row.get("successor_isin") or None,
                parse_errors=tuple(errors),
            )
            records.append(
                IngestionEnvelope(
                    provider=self.metadata,
                    external_record_id=row.get("external_id") or f"row-{index}",
                    source_uri=f"file://{self.path.name}",
                    raw_payload_reference=f"row-{index}",
                    content_sha256=_row_hash(row),
                    retrieved_at=self.retrieved_at,
                    record=record,
                    available_at=available_at,
                    revision_at=revision_at,
                )
            )
        return ProviderBatch(
            self.metadata,
            f"file://{self.path.name}",
            raw_payload,
            self.retrieved_at,
            tuple(records),
        )


@dataclass(frozen=True, slots=True)
class MockUniverseProvider:
    """In-memory universe provider for orchestration tests."""

    batch: ProviderBatch[UniverseRecord]

    def fetch_universe(self) -> ProviderBatch[UniverseRecord]:
        return self.batch


@dataclass(frozen=True, slots=True)
class MockMarketDataProvider:
    """In-memory market provider for orchestration tests."""

    batch: ProviderBatch[MarketBarRecord]

    def fetch_market_data(self) -> ProviderBatch[MarketBarRecord]:
        return self.batch


@dataclass(frozen=True, slots=True)
class MockFinancialsProvider:
    """In-memory financial provider for orchestration tests."""

    batch: ProviderBatch[FinancialRecord]

    def fetch_financials(self) -> ProviderBatch[FinancialRecord]:
        return self.batch


@dataclass(frozen=True, slots=True)
class MockCorporateActionProvider:
    """In-memory action provider for orchestration tests."""

    batch: ProviderBatch[CorporateActionRecord]

    def fetch_corporate_actions(self) -> ProviderBatch[CorporateActionRecord]:
        return self.batch
