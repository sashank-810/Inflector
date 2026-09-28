"""Deterministic point-in-time reads over accepted announcement evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_data.pit import SourceRecordView
from inflector_database.models import (
    Announcement,
    AnnouncementDocument,
    Document,
    SourceRecord,
)


class AnnouncementDataIntegrityError(ValueError):
    """Raised when persisted announcement provenance violates provider identity."""


@dataclass(frozen=True, slots=True)
class PointInTimeDocument:
    id: UUID
    company_id: UUID
    security_id: UUID | None
    provider_dataset_id: UUID
    document_type: str
    title: str
    language: str | None
    media_type: str | None
    document_uri: str
    document_content_sha256: str | None
    role: str
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    source_record: SourceRecordView


@dataclass(frozen=True, slots=True)
class PointInTimeAnnouncement:
    id: UUID
    company_id: UUID
    security_id: UUID | None
    provider_dataset_id: UUID
    external_record_id: str
    provider_category: str | None
    headline: str
    announcement_date: date | None
    exchange: str | None
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    source_record: SourceRecordView
    documents: tuple[PointInTimeDocument, ...]


_AnnouncementRow = tuple[Announcement, SourceRecord]
_DocumentRow = tuple[AnnouncementDocument, Document, SourceRecord]


class PointInTimeAnnouncementReader:
    """Read one accepted revision per provider announcement identity."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def announcement_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        external_record_id: str,
        as_of: datetime,
    ) -> PointInTimeAnnouncement | None:
        cutoff = self._knowledge_cutoff(as_of)
        if not external_record_id.strip():
            raise ValueError("external_record_id must be non-empty")
        row = self._session.execute(
            self._base_statement(provider_dataset_id, cutoff).where(
                SourceRecord.external_record_id == external_record_id
            )
        ).first()
        return None if row is None else self._view((row[0], row[1]))

    def company_announcements_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[PointInTimeAnnouncement]:
        return self._announcements_as_of(
            provider_dataset_id=provider_dataset_id,
            as_of=as_of,
            company_id=company_id,
            security_id=None,
            start_date=start_date,
            end_date=end_date,
        )

    def security_announcements_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[PointInTimeAnnouncement]:
        return self._announcements_as_of(
            provider_dataset_id=provider_dataset_id,
            as_of=as_of,
            company_id=None,
            security_id=security_id,
            start_date=start_date,
            end_date=end_date,
        )

    def _announcements_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        as_of: datetime,
        company_id: UUID | None,
        security_id: UUID | None,
        start_date: date | None,
        end_date: date | None,
    ) -> list[PointInTimeAnnouncement]:
        cutoff = self._knowledge_cutoff(as_of)
        self._validate_range(start_date, end_date)
        selected: dict[str, _AnnouncementRow] = {}
        for row in self._session.execute(self._base_statement(provider_dataset_id, cutoff)):
            announcement, source = row[0], row[1]
            selected.setdefault(source.external_record_id, (announcement, source))
        filtered = (
            row
            for row in selected.values()
            if self._matches_filters(
                row[0],
                company_id=company_id,
                security_id=security_id,
                start_date=start_date,
                end_date=end_date,
            )
        )
        return [self._view(row) for row in filtered]

    @staticmethod
    def _matches_filters(
        announcement: Announcement,
        *,
        company_id: UUID | None,
        security_id: UUID | None,
        start_date: date | None,
        end_date: date | None,
    ) -> bool:
        if company_id is not None and announcement.company_id != company_id:
            return False
        if security_id is not None and announcement.security_id != security_id:
            return False
        if start_date is None and end_date is None:
            return True
        if announcement.announcement_date is None:
            return False
        if start_date is not None and announcement.announcement_date < start_date:
            return False
        return end_date is None or announcement.announcement_date <= end_date

    @staticmethod
    def _base_statement(provider_dataset_id: UUID, cutoff: datetime):
        return (
            select(Announcement, SourceRecord)
            .join(SourceRecord, Announcement.source_record_id == SourceRecord.id)
            .where(
                Announcement.provider_dataset_id == provider_dataset_id,
                Announcement.available_at <= cutoff,
                SourceRecord.validation_status == "accepted",
            )
            .order_by(
                Announcement.available_at.desc(),
                func.coalesce(
                    Announcement.revision_at,
                    Announcement.available_at,
                ).desc(),
                Announcement.ingested_at.desc(),
                Announcement.id.desc(),
            )
        )

    def _view(self, row: _AnnouncementRow) -> PointInTimeAnnouncement:
        announcement, source = row
        if source.provider_dataset_id != announcement.provider_dataset_id:
            raise AnnouncementDataIntegrityError(
                "announcement source provider does not match announcement provider"
            )
        return PointInTimeAnnouncement(
            id=announcement.id,
            company_id=announcement.company_id,
            security_id=announcement.security_id,
            provider_dataset_id=announcement.provider_dataset_id,
            external_record_id=source.external_record_id,
            provider_category=announcement.provider_category,
            headline=announcement.headline,
            announcement_date=announcement.announcement_date,
            exchange=announcement.exchange,
            available_at=self._as_utc(announcement.available_at),
            revision_at=self._as_utc_or_none(announcement.revision_at),
            ingested_at=self._as_utc(announcement.ingested_at),
            source_record=self._source_view(source),
            documents=self._documents(announcement),
        )

    def _documents(self, announcement: Announcement) -> tuple[PointInTimeDocument, ...]:
        rows: list[_DocumentRow] = []
        for row in self._session.execute(
            select(AnnouncementDocument, Document, SourceRecord)
            .join(Document, AnnouncementDocument.document_id == Document.id)
            .join(SourceRecord, Document.source_record_id == SourceRecord.id)
            .where(
                AnnouncementDocument.announcement_id == announcement.id,
                SourceRecord.validation_status == "accepted",
            )
        ):
            rows.append((row[0], row[1], row[2]))
        role_order = {"primary": 0, "attachment": 1, "supporting": 2}
        rows.sort(key=lambda row: (role_order.get(row[0].role, 99), str(row[1].id)))
        documents: list[PointInTimeDocument] = []
        for relation, document, source in rows:
            if (
                document.provider_dataset_id != announcement.provider_dataset_id
                or source.provider_dataset_id != document.provider_dataset_id
                or document.company_id != announcement.company_id
                or document.security_id != announcement.security_id
            ):
                raise AnnouncementDataIntegrityError(
                    "document lineage does not match its announcement context"
                )
            documents.append(
                PointInTimeDocument(
                    id=document.id,
                    company_id=document.company_id,
                    security_id=document.security_id,
                    provider_dataset_id=document.provider_dataset_id,
                    document_type=document.document_type,
                    title=document.title,
                    language=document.language,
                    media_type=document.media_type,
                    document_uri=document.document_uri,
                    document_content_sha256=document.document_content_sha256,
                    role=relation.role,
                    available_at=self._as_utc(document.available_at),
                    revision_at=self._as_utc_or_none(document.revision_at),
                    ingested_at=self._as_utc(document.ingested_at),
                    source_record=self._source_view(source),
                )
            )
        return tuple(documents)

    @staticmethod
    def _source_view(source: SourceRecord) -> SourceRecordView:
        return SourceRecordView(
            id=source.id,
            external_record_id=source.external_record_id,
            source_uri=source.source_uri,
            raw_object_key=source.raw_object_key,
            raw_payload_reference=source.raw_payload_reference,
            content_sha256=source.content_sha256,
            validation_status=source.validation_status,
        )

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _as_utc_or_none(cls, value: datetime | None) -> datetime | None:
        return None if value is None else cls._as_utc(value)

    @staticmethod
    def _validate_range(start_date: date | None, end_date: date | None) -> None:
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
