"""Deterministic PIT reads over immutable corporate-action observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_core.providers import corporate_action_event_anchor
from inflector_data.pit import SourceRecordView
from inflector_database.models import CorporateAction, SourceRecord


class CorporateActionIntegrityError(ValueError):
    """Raised when persisted action provenance violates provider identity."""


@dataclass(frozen=True, slots=True)
class PointInTimeCorporateAction:
    """Complete immutable action terms selected at one knowledge cutoff."""

    id: UUID
    provider_dataset_id: UUID
    security_id: UUID
    action_type: str
    announcement_date: date | None
    ex_date: date | None
    record_date: date | None
    effective_date: date | None
    ratio_numerator: int | None
    ratio_denominator: int | None
    cash_amount: Decimal | None
    cash_currency: str | None
    cash_unit: str | None
    subscription_price: Decimal | None
    subscription_currency: str | None
    exchange: str | None
    old_symbol: str | None
    new_symbol: str | None
    successor_isin: str | None
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    source_record: SourceRecordView

    @property
    def event_anchor(self) -> date | None:
        """Return the canonical Phase 2C economic event anchor."""

        return corporate_action_event_anchor(self.action_type, self.ex_date, self.effective_date)


_ActionRow = tuple[CorporateAction, SourceRecord]


class PointInTimeCorporateActionReader:
    """Select accepted provider-local action revisions without current-state gates."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def corporate_actions_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        as_of: datetime,
        action_types: tuple[str, ...] | None = None,
    ) -> list[PointInTimeCorporateAction]:
        """Return newest action revisions after external-ID and event collapse."""

        cutoff = self._knowledge_cutoff(as_of)
        if action_types is not None:
            if any(not action_type.strip() for action_type in action_types):
                raise ValueError("action_types must contain only non-empty values")
            if not action_types:
                return []
        requested_types = None if action_types is None else frozenset(action_types)
        statement = (
            select(CorporateAction, SourceRecord)
            .join(SourceRecord, CorporateAction.source_record_id == SourceRecord.id)
            .where(
                CorporateAction.provider_dataset_id == provider_dataset_id,
                CorporateAction.security_id == security_id,
                CorporateAction.available_at <= cutoff,
                SourceRecord.validation_status == "accepted",
            )
            .order_by(
                CorporateAction.available_at.desc(),
                func.coalesce(CorporateAction.revision_at, CorporateAction.available_at).desc(),
                CorporateAction.ingested_at.desc(),
                CorporateAction.id.desc(),
            )
        )
        rows = list(self._session.execute(statement).tuples())
        for action, source in rows:
            if source.provider_dataset_id != action.provider_dataset_id:
                raise CorporateActionIntegrityError(
                    "corporate-action source dataset does not match action dataset"
                )

        newest_by_external_id: dict[str, _ActionRow] = {}
        for row in rows:
            newest_by_external_id.setdefault(row[1].external_record_id, row)

        newest_by_event: dict[tuple[str, date | None], _ActionRow] = {}
        for row in newest_by_external_id.values():
            action = row[0]
            if requested_types is not None and action.action_type not in requested_types:
                continue
            event_key = (
                action.action_type,
                corporate_action_event_anchor(
                    action.action_type, action.ex_date, action.effective_date
                ),
            )
            newest_by_event.setdefault(event_key, row)

        selected = [self._view(row) for row in newest_by_event.values()]
        return sorted(
            selected,
            key=lambda action: (
                action.event_anchor or date.min,
                action.action_type,
                action.available_at,
                str(action.id),
            ),
        )

    @staticmethod
    def _view(row: _ActionRow) -> PointInTimeCorporateAction:
        action, source = row
        return PointInTimeCorporateAction(
            id=action.id,
            provider_dataset_id=action.provider_dataset_id,
            security_id=action.security_id,
            action_type=action.action_type,
            announcement_date=action.announcement_date,
            ex_date=action.ex_date,
            record_date=action.record_date,
            effective_date=action.effective_date,
            ratio_numerator=action.ratio_numerator,
            ratio_denominator=action.ratio_denominator,
            cash_amount=action.cash_amount,
            cash_currency=action.cash_currency,
            cash_unit=action.cash_unit,
            subscription_price=action.subscription_price,
            subscription_currency=action.subscription_currency,
            exchange=action.exchange,
            old_symbol=action.old_symbol,
            new_symbol=action.new_symbol,
            successor_isin=action.successor_isin,
            available_at=PointInTimeCorporateActionReader._as_utc(action.available_at),
            revision_at=PointInTimeCorporateActionReader._as_utc_or_none(action.revision_at),
            ingested_at=PointInTimeCorporateActionReader._as_utc(action.ingested_at),
            source_record=SourceRecordView(
                id=source.id,
                external_record_id=source.external_record_id,
                source_uri=source.source_uri,
                raw_object_key=source.raw_object_key,
                raw_payload_reference=source.raw_payload_reference,
                content_sha256=source.content_sha256,
                validation_status=source.validation_status,
            ),
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _as_utc_or_none(value: datetime | None) -> datetime | None:
        return None if value is None else PointInTimeCorporateActionReader._as_utc(value)
