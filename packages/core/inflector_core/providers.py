"""Provider-neutral contracts and typed ingestion envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from re import fullmatch
from typing import Protocol


def _require_utc(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """Stable provider and dataset identity without operational secrets."""

    provider_code: str
    provider_type: str
    dataset_code: str
    licence_class: str
    licence_reference: str | None = None
    redistributable: bool = False
    retention_days: int | None = None


@dataclass(frozen=True, slots=True)
class UniverseRecord:
    """Provider-neutral company/security/listing input for normalization."""

    legal_name: str
    display_name: str
    sector: str
    industry: str
    isin: str
    security_type: str
    security_status: str
    exchange: str
    symbol: str
    listing_status: str
    valid_from: date | None
    valid_to: date | None
    parse_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MarketBarRecord:
    """Daily market-bar input that preserves parse failures for validation."""

    security_isin: str | None
    trading_date: date | None
    interval: str | None
    open_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None
    close_price: Decimal | None
    volume: int | None
    parse_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IngestionEnvelope[TRecord]:
    """Record-level provider observation before storage or normalization."""

    provider: ProviderMetadata
    external_record_id: str
    source_uri: str
    raw_payload_reference: str
    content_sha256: str
    retrieved_at: datetime
    record: TRecord
    reported_at: datetime | None = None
    published_at: datetime | None = None
    available_at: datetime | None = None
    revision_at: datetime | None = None
    cursor: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "retrieved_at",
            "reported_at",
            "published_at",
            "available_at",
            "revision_at",
        ):
            _require_utc(getattr(self, field_name), field_name)
        if fullmatch(r"[0-9a-fA-F]{64}", self.content_sha256) is None:
            raise ValueError("content_sha256 must be a SHA-256 hexadecimal digest")


@dataclass(frozen=True, slots=True)
class ProviderBatch[TRecord]:
    """One immutable raw provider payload and its parsed record envelopes."""

    provider: ProviderMetadata
    source_uri: str
    raw_payload: bytes
    retrieved_at: datetime
    records: tuple[IngestionEnvelope[TRecord], ...]
    cursor: str | None = None

    def __post_init__(self) -> None:
        _require_utc(self.retrieved_at, "retrieved_at")


class UniverseProvider(Protocol):
    """Provider port for canonical company/security/listing observations."""

    def fetch_universe(self) -> ProviderBatch[UniverseRecord]:
        """Return one provider-neutral universe batch."""

        ...


class MarketDataProvider(Protocol):
    """Provider port for daily market-bar observations."""

    def fetch_market_data(self) -> ProviderBatch[MarketBarRecord]:
        """Return one provider-neutral market-data batch."""

        ...
