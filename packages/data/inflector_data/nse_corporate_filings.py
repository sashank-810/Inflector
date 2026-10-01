"""Official NSE corporate-action and announcement production adapters."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
from typing import Final
from urllib.parse import urlencode, urlparse

from inflector_core.providers import (
    AnnouncementDocumentRecord,
    AnnouncementRecord,
    CorporateActionRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
    UniverseRecord,
)
from inflector_data.nse_http import AcquiredNSEArtifact
from inflector_data.nse_providers import NSEArtifactSource

NSE_CORPORATE_ACTION_DATASET_CODE = "nse_corporate_actions"
NSE_ANNOUNCEMENT_DATASET_CODE = "nse_corporate_announcements"
NSE_CORPORATE_ACTION_PURPOSE_RULES_VERSION = "nse_corporate_action_purpose_rules_v1"
NSE_CORPORATE_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
MAXIMUM_CORPORATE_FILING_DATE_RANGE_DAYS = 366

_DATE_FORMAT: Final = "%d-%b-%Y"
_DATETIME_FORMAT: Final = "%d-%b-%Y %H:%M:%S"
_DIVIDEND = re.compile(
    r"^(?:(?:Interim|Final|Special) )?Dividend - (?:Rs|Re) "
    r"(?P<amount>[0-9]+(?:\.[0-9]+)?) Per Share$"
)
_BONUS = re.compile(r"^Bonus (?P<numerator>[1-9][0-9]*):(?P<denominator>[1-9][0-9]*)$")
_SPLIT = re.compile(
    r"^Face Value Split \(Sub-Division\) - From (?:Rs|Re) "
    r"(?P<old>[0-9]+(?:\.[0-9]+)?)/- Per Share To (?:Rs|Re) "
    r"(?P<new>[0-9]+(?:\.[0-9]+)?)/- Per Share$"
)
_RIGHTS_PREMIUM = re.compile(
    r"^Rights (?P<numerator>[1-9][0-9]*):(?P<denominator>[1-9][0-9]*) "
    r"@ Premium Rs (?P<premium>[0-9]+(?:\.[0-9]+)?)/-$"
)
_RIGHTS_TOTAL = re.compile(
    r"^Rights (?P<numerator>[1-9][0-9]*):(?P<denominator>[1-9][0-9]*) "
    r"@ Rs (?P<price>[0-9]+(?:\.[0-9]+)?)/-$"
)


class NSECorporateFilingFormatError(ValueError):
    """An official filing response has an unsafe structural shape."""


@dataclass(frozen=True, slots=True)
class NSEEquityIdentity:
    symbol: str
    isin: str
    company_name: str


def nse_equity_identities(
    records: tuple[IngestionEnvelope[UniverseRecord], ...],
) -> dict[str, NSEEquityIdentity]:
    """Build an exact current EQ symbol map from the official universe batch."""

    identities: dict[str, NSEEquityIdentity] = {}
    for envelope in records:
        record = envelope.record
        if record.parse_errors or not record.symbol or not record.isin:
            continue
        symbol = record.symbol.strip().upper()
        candidate = NSEEquityIdentity(symbol, record.isin.strip(), record.legal_name.strip())
        existing = identities.get(symbol)
        if existing is not None and existing != candidate:
            raise NSECorporateFilingFormatError(
                "official NSE universe contains a conflicting EQ symbol identity"
            )
        identities[symbol] = candidate
    return identities


def nse_corporate_actions_url(from_date: date, to_date: date, *, symbol: str | None = None) -> str:
    query = _filing_query(from_date, to_date, symbol=symbol)
    return f"{NSE_CORPORATE_ACTIONS_URL}?{urlencode(query)}"


def nse_announcements_url(from_date: date, to_date: date, *, symbol: str | None = None) -> str:
    query = _filing_query(from_date, to_date, symbol=symbol)
    return f"{NSE_ANNOUNCEMENTS_URL}?{urlencode(query)}"


def _filing_query(from_date: date, to_date: date, *, symbol: str | None) -> dict[str, str]:
    if to_date < from_date:
        raise ValueError("to_date must be on or after from_date")
    if (to_date - from_date).days > MAXIMUM_CORPORATE_FILING_DATE_RANGE_DAYS:
        raise ValueError("corporate filing date range exceeds 366 days")
    query = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
    }
    if symbol is not None:
        normalized = symbol.strip().upper()
        if not normalized or re.fullmatch(r"[A-Z0-9&-]{1,64}", normalized) is None:
            raise ValueError("symbol must be a controlled NSE symbol")
        query["symbol"] = normalized
    return query


class _ProviderStats:
    def __init__(self, source: NSEArtifactSource) -> None:
        self._source = source
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

    def _observe(self, artifact: AcquiredNSEArtifact) -> None:
        self._source_uri = artifact.source_uri
        self._retrieved_at = artifact.retrieved_at


class NSECorporateActionProvider(_ProviderStats):
    """Strict current EQ action adapter with versioned anchored Purpose rules."""

    def __init__(
        self,
        source: NSEArtifactSource,
        metadata: ProviderMetadata,
        identities: dict[str, NSEEquityIdentity],
    ) -> None:
        super().__init__(source)
        self._metadata = metadata
        self._identities = identities
        self._supported_action_counts: dict[str, int] = {}
        self._unsupported_purposes: list[str] = []

    @property
    def supported_action_counts(self) -> dict[str, int]:
        return dict(self._supported_action_counts)

    @property
    def unsupported_purposes(self) -> tuple[str, ...]:
        return tuple(self._unsupported_purposes)

    def fetch_corporate_actions(self) -> ProviderBatch[CorporateActionRecord]:
        artifact = self._source.acquire()
        self._observe(artifact)
        rows = _json_rows(artifact.payload)
        records: list[IngestionEnvelope[CorporateActionRecord]] = []
        skipped = 0
        counts: dict[str, int] = {}
        unsupported: list[str] = []
        for index, row in enumerate(rows):
            symbol = _text(row.get("symbol")).upper()
            if _text(row.get("series")) != "EQ" or symbol not in self._identities:
                skipped += 1
                continue
            identity = self._identities[symbol]
            purpose = _text(row.get("subject"))
            parsed = _parse_action_purpose(purpose, face_value=_text(row.get("faceVal")))
            if parsed is None:
                skipped += 1
                if purpose and purpose not in unsupported:
                    unsupported.append(purpose)
                continue
            action_type, numerator, denominator, cash_amount, subscription_price = parsed
            errors: list[str] = []
            source_isin = _text(row.get("isin"))
            if not source_isin:
                errors.append("missing_source_isin")
            elif source_isin != identity.isin:
                errors.append("nse_security_identity_mismatch")
            ex_date = _optional_date(row.get("exDate"), errors, "invalid_ex_date")
            record_date = _optional_date(row.get("recDate"), errors, "invalid_record_date")
            announcement_date = _optional_datetime_date(
                row.get("caBroadcastDate"), errors, "invalid_announcement_date"
            )
            effective_date = ex_date if action_type in {"split", "bonus", "rights"} else None
            record = CorporateActionRecord(
                security_isin=identity.isin,
                action_type=action_type,
                announcement_date=announcement_date,
                ex_date=ex_date,
                record_date=record_date,
                effective_date=effective_date,
                ratio_numerator=numerator,
                ratio_denominator=denominator,
                cash_amount=cash_amount,
                cash_currency="INR" if cash_amount is not None else None,
                cash_unit="INR/share" if cash_amount is not None else None,
                subscription_price=subscription_price,
                subscription_currency="INR" if subscription_price is not None else None,
                exchange="NSE",
                old_symbol=None,
                new_symbol=None,
                successor_isin=None,
                parse_errors=tuple(errors),
            )
            identity_text = "|".join(
                (
                    symbol,
                    purpose,
                    _text(row.get("exDate")),
                    _text(row.get("recDate")),
                )
            )
            external_id = f"nse-action:{sha256(identity_text.encode('utf-8')).hexdigest()}"
            records.append(
                _envelope(
                    artifact,
                    self._metadata,
                    external_id=external_id,
                    locator=f"json:[{index}]:{symbol}",
                    row=row,
                    record=record,
                )
            )
            counts[action_type] = counts.get(action_type, 0) + 1
        self._skipped_rows = skipped
        self._supported_action_counts = counts
        self._unsupported_purposes = unsupported
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            tuple(records),
        )


class NSEAnnouncementProvider(_ProviderStats):
    """Official structured NSE announcement metadata and attachment adapter."""

    def __init__(
        self,
        source: NSEArtifactSource,
        metadata: ProviderMetadata,
        identities: dict[str, NSEEquityIdentity],
        *,
        max_announcements: int,
    ) -> None:
        super().__init__(source)
        if max_announcements < 1 or max_announcements > 1_000:
            raise ValueError("max_announcements must be between 1 and 1000")
        self._metadata = metadata
        self._identities = identities
        self._max_announcements = max_announcements
        self._documents_discovered = 0
        self._external_record_ids: tuple[str, ...] = ()

    @property
    def documents_discovered(self) -> int:
        return self._documents_discovered

    @property
    def external_record_ids(self) -> tuple[str, ...]:
        return self._external_record_ids

    def fetch_announcements(self) -> ProviderBatch[AnnouncementRecord]:
        artifact = self._source.acquire()
        self._observe(artifact)
        rows = _json_rows(artifact.payload)
        records: list[IngestionEnvelope[AnnouncementRecord]] = []
        skipped = 0
        documents = 0
        for index, row in enumerate(rows):
            if len(records) >= self._max_announcements:
                skipped += len(rows) - index
                break
            symbol = _text(row.get("symbol")).upper()
            identity = self._identities.get(symbol)
            if identity is None:
                skipped += 1
                continue
            errors: list[str] = []
            source_isin = _text(row.get("sm_isin"))
            if not source_isin:
                errors.append("missing_source_isin")
            elif source_isin != identity.isin:
                errors.append("nse_security_identity_mismatch")
            source_name = _text(row.get("sm_name"))
            if source_name and source_name != identity.company_name:
                errors.append("nse_company_security_mismatch")
            sequence_id = _text(row.get("seq_id"))
            if not sequence_id:
                errors.append("missing_announcement_sequence")
                sequence_id = f"row-{index}"
            event_at = _required_datetime(row.get("an_dt"), errors)
            headline = _text(row.get("attchmntText")) or _text(row.get("desc"))
            documents_for_row: list[AnnouncementDocumentRecord] = []
            document_uri = _text(row.get("attchmntFile"))
            if document_uri:
                document_errors: list[str] = []
                if not _official_attachment_uri(document_uri):
                    document_errors.append("invalid_official_document_uri")
                documents_for_row.append(
                    AnnouncementDocumentRecord(
                        document_type="announcement_attachment",
                        title=_text(row.get("desc")) or "NSE announcement attachment",
                        language="en",
                        media_type=None,
                        document_uri=document_uri,
                        document_content_sha256=None,
                        role="primary",
                        parse_errors=tuple(document_errors),
                    )
                )
                documents += 1
            record = AnnouncementRecord(
                company_legal_name=source_name or identity.company_name,
                security_isin=identity.isin,
                provider_category=_text(row.get("desc")) or None,
                headline=headline or None,
                announcement_date=event_at.date() if event_at is not None else None,
                exchange="NSE",
                documents=tuple(documents_for_row),
                parse_errors=tuple(errors),
            )
            records.append(
                _envelope(
                    artifact,
                    self._metadata,
                    external_id=f"nse-announcement:{sequence_id}",
                    locator=f"json:[{index}]:seq_id:{sequence_id}",
                    row=row,
                    record=record,
                )
            )
        self._skipped_rows = skipped
        self._documents_discovered = documents
        self._external_record_ids = tuple(envelope.external_record_id for envelope in records)
        return ProviderBatch(
            self._metadata,
            artifact.source_uri,
            artifact.payload,
            artifact.retrieved_at,
            tuple(records),
        )


def _parse_action_purpose(
    purpose: str, *, face_value: str
) -> tuple[str, int | None, int | None, Decimal | None, Decimal | None] | None:
    match = _DIVIDEND.fullmatch(purpose)
    if match is not None:
        amount = _positive_decimal(match.group("amount"))
        return None if amount is None else ("cash_dividend", None, None, amount, None)
    match = _BONUS.fullmatch(purpose)
    if match is not None:
        return (
            "bonus",
            int(match.group("numerator")),
            int(match.group("denominator")),
            None,
            None,
        )
    match = _SPLIT.fullmatch(purpose)
    if match is not None:
        old_value = _positive_decimal(match.group("old"))
        new_value = _positive_decimal(match.group("new"))
        if old_value is None or new_value is None:
            return None
        ratio = Fraction(old_value) / Fraction(new_value)
        return ("split", ratio.numerator, ratio.denominator, None, None)
    match = _RIGHTS_TOTAL.fullmatch(purpose)
    if match is not None:
        total = _positive_decimal(match.group("price"))
        if total is None:
            return None
        return (
            "rights",
            int(match.group("numerator")),
            int(match.group("denominator")),
            None,
            total,
        )
    match = _RIGHTS_PREMIUM.fullmatch(purpose)
    if match is not None:
        premium = _positive_decimal(match.group("premium"))
        face = _positive_decimal(face_value)
        if premium is None or face is None:
            return None
        return (
            "rights",
            int(match.group("numerator")),
            int(match.group("denominator")),
            None,
            premium + face,
        )
    return None


def _json_rows(payload: bytes) -> list[dict[str, object]]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NSECorporateFilingFormatError("official NSE response is not JSON") from error
    if not isinstance(parsed, list):
        raise NSECorporateFilingFormatError("official NSE response must be a JSON list")
    if not all(isinstance(row, dict) for row in parsed):
        raise NSECorporateFilingFormatError("official NSE response contains a non-object row")
    return parsed


def _envelope[TRecord](
    artifact: AcquiredNSEArtifact,
    metadata: ProviderMetadata,
    *,
    external_id: str,
    locator: str,
    row: dict[str, object],
    record: TRecord,
) -> IngestionEnvelope[TRecord]:
    digest = sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return IngestionEnvelope(
        provider=metadata,
        external_record_id=external_id,
        source_uri=artifact.source_uri,
        raw_payload_reference=locator,
        content_sha256=digest,
        retrieved_at=artifact.retrieved_at,
        available_at=artifact.retrieved_at,
        revision_at=None,
        record=record,
    )


def _optional_date(raw: object, errors: list[str], code: str) -> date | None:
    text = _text(raw)
    if not text or text == "-":
        return None
    try:
        return datetime.strptime(text, _DATE_FORMAT).date()
    except ValueError:
        errors.append(code)
        return None


def _optional_datetime_date(raw: object, errors: list[str], code: str) -> date | None:
    text = _text(raw)
    if not text or text == "-":
        return None
    for pattern in (_DATETIME_FORMAT, _DATE_FORMAT):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    errors.append(code)
    return None


def _required_datetime(raw: object, errors: list[str]) -> datetime | None:
    text = _text(raw)
    try:
        return datetime.strptime(text, _DATETIME_FORMAT)
    except ValueError:
        errors.append("invalid_announcement_event_time")
        return None


def _positive_decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _official_attachment_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "nsearchives.nseindia.com"
        and parsed.username is None
        and parsed.password is None
    )
