"""Point-in-time reads for neutral deterministic business events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.business_event_rules import BUSINESS_EVENT_TYPE_ORDER
from inflector_data.announcement_pit import (
    PointInTimeAnnouncement,
    PointInTimeAnnouncementReader,
    PointInTimeDocument,
)
from inflector_data.document_text import DocumentAssetView, DocumentTextExtractionView
from inflector_database.models import (
    BusinessEvent,
    BusinessEventEvidence,
    DocumentAsset,
    DocumentTextExtraction,
)


class BusinessEventReadIntegrityError(ValueError):
    """Raised when persisted event evidence no longer matches its source lineage."""


@dataclass(frozen=True, slots=True)
class BusinessEventEvidenceView:
    id: UUID
    evidence_kind: str
    document: PointInTimeDocument | None
    document_asset: DocumentAssetView | None
    text_extraction: DocumentTextExtractionView | None
    rule_code: str
    rule_semantic_version: str
    start_offset: int
    end_offset: int
    page_numbers: tuple[int, ...]
    page_text_sha256s: tuple[str, ...]
    excerpt_text: str
    excerpt_sha256: str
    source_available_at: datetime
    evidence_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class PointInTimeBusinessEvent:
    id: UUID
    announcement: PointInTimeAnnouncement
    company_id: UUID
    security_id: UUID | None
    provider_dataset_id: UUID
    event_type: str
    source_event_date: date | None
    source_available_at: datetime
    ruleset_code: str
    ruleset_semantic_version: str
    matched_rule_codes: tuple[str, ...]
    status: str
    warnings: tuple[str, ...]
    detection_fingerprint_sha256: str
    derived_at: datetime
    evidence: tuple[BusinessEventEvidenceView, ...]


class PointInTimeBusinessEventReader:
    """Expose events only through the exact PIT-selected announcement revision."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._announcements = PointInTimeAnnouncementReader(session)

    def events_for_announcement(
        self,
        *,
        announcement: PointInTimeAnnouncement,
        ruleset_code: str,
        ruleset_semantic_version: str,
    ) -> tuple[PointInTimeBusinessEvent, ...]:
        if not ruleset_code or not ruleset_semantic_version:
            raise ValueError("ruleset identity must be non-empty")
        events = list(
            self._session.scalars(
                select(BusinessEvent).where(
                    BusinessEvent.announcement_id == announcement.id,
                    BusinessEvent.ruleset_code == ruleset_code,
                    BusinessEvent.ruleset_semantic_version == ruleset_semantic_version,
                )
            )
        )
        rank = {event_type: index for index, event_type in enumerate(BUSINESS_EVENT_TYPE_ORDER)}
        events.sort(key=lambda event: (rank.get(event.event_type, 99), str(event.id)))
        return tuple(self._event_view(event, announcement) for event in events)

    def company_events_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        as_of: datetime,
        ruleset_code: str,
        ruleset_semantic_version: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[PointInTimeBusinessEvent, ...]:
        announcements = self._announcements.company_announcements_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
        )
        return self._events_for_announcements(
            announcements,
            ruleset_code=ruleset_code,
            ruleset_semantic_version=ruleset_semantic_version,
        )

    def security_events_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        security_id: UUID,
        as_of: datetime,
        ruleset_code: str,
        ruleset_semantic_version: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[PointInTimeBusinessEvent, ...]:
        announcements = self._announcements.security_announcements_as_of(
            provider_dataset_id=provider_dataset_id,
            security_id=security_id,
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
        )
        return self._events_for_announcements(
            announcements,
            ruleset_code=ruleset_code,
            ruleset_semantic_version=ruleset_semantic_version,
        )

    def _events_for_announcements(
        self,
        announcements: list[PointInTimeAnnouncement],
        *,
        ruleset_code: str,
        ruleset_semantic_version: str,
    ) -> tuple[PointInTimeBusinessEvent, ...]:
        values = [
            event
            for announcement in announcements
            for event in self.events_for_announcement(
                announcement=announcement,
                ruleset_code=ruleset_code,
                ruleset_semantic_version=ruleset_semantic_version,
            )
        ]
        values.sort(
            key=lambda event: (
                event.source_available_at,
                str(event.announcement.id),
                BUSINESS_EVENT_TYPE_ORDER.index(event.event_type),
            )
        )
        return tuple(values)

    def _event_view(
        self,
        event: BusinessEvent,
        announcement: PointInTimeAnnouncement,
    ) -> PointInTimeBusinessEvent:
        source_available_at = _as_utc(event.source_available_at)
        if (
            event.company_id != announcement.company_id
            or event.security_id != announcement.security_id
            or event.provider_dataset_id != announcement.provider_dataset_id
            or event.source_event_date != announcement.announcement_date
            or source_available_at != _as_utc(announcement.available_at)
            or event.status != "detected"
        ):
            raise BusinessEventReadIntegrityError(
                "business event context does not match its announcement revision"
            )
        if event.event_type not in BUSINESS_EVENT_TYPE_ORDER:
            raise BusinessEventReadIntegrityError("unknown persisted business event type")
        evidence_rows = list(
            self._session.scalars(
                select(BusinessEventEvidence)
                .where(BusinessEventEvidence.business_event_id == event.id)
                .order_by(
                    BusinessEventEvidence.evidence_kind,
                    BusinessEventEvidence.document_id,
                    BusinessEventEvidence.text_extraction_id,
                    BusinessEventEvidence.start_offset,
                    BusinessEventEvidence.end_offset,
                    BusinessEventEvidence.rule_code,
                    BusinessEventEvidence.evidence_fingerprint_sha256,
                )
            )
        )
        if not evidence_rows:
            raise BusinessEventReadIntegrityError("detected business event has no evidence")
        evidence = tuple(
            self._evidence_view(row, announcement, source_available_at)
            for row in evidence_rows
        )
        return PointInTimeBusinessEvent(
            id=event.id,
            announcement=announcement,
            company_id=event.company_id,
            security_id=event.security_id,
            provider_dataset_id=event.provider_dataset_id,
            event_type=event.event_type,
            source_event_date=event.source_event_date,
            source_available_at=source_available_at,
            ruleset_code=event.ruleset_code,
            ruleset_semantic_version=event.ruleset_semantic_version,
            matched_rule_codes=tuple(str(value) for value in event.matched_rule_codes_json),
            status=event.status,
            warnings=tuple(str(value) for value in event.warnings_json),
            detection_fingerprint_sha256=event.detection_fingerprint_sha256,
            derived_at=_as_utc(event.derived_at),
            evidence=evidence,
        )

    def _evidence_view(
        self,
        row: BusinessEventEvidence,
        announcement: PointInTimeAnnouncement,
        source_available_at: datetime,
    ) -> BusinessEventEvidenceView:
        if (
            row.announcement_id != announcement.id
            or _as_utc(row.source_available_at) != source_available_at
            or sha256(row.excerpt_text.encode("utf-8")).hexdigest() != row.excerpt_sha256
            or row.start_offset < 0
            or row.end_offset <= row.start_offset
        ):
            raise BusinessEventReadIntegrityError("invalid persisted event evidence")
        documents = {document.id: document for document in announcement.documents}
        document: PointInTimeDocument | None = None
        asset_view: DocumentAssetView | None = None
        extraction_view: DocumentTextExtractionView | None = None
        page_numbers = tuple(int(value) for value in row.page_numbers_json)
        page_hashes = tuple(str(value) for value in row.page_text_sha256s_json)
        if row.evidence_kind == "announcement_headline":
            if (
                row.document_id is not None
                or row.document_asset_id is not None
                or row.text_extraction_id is not None
                or page_numbers
                or page_hashes
                or row.end_offset > len(announcement.headline)
                or announcement.headline[row.start_offset : row.end_offset] != row.excerpt_text
            ):
                raise BusinessEventReadIntegrityError("invalid headline event evidence")
        elif row.evidence_kind == "document_text":
            if (
                row.document_id is None
                or row.document_asset_id is None
                or row.text_extraction_id is None
                or len(page_numbers) != 1
                or len(page_numbers) != len(page_hashes)
            ):
                raise BusinessEventReadIntegrityError("incomplete document event evidence")
            document = documents.get(row.document_id)
            if document is None:
                raise BusinessEventReadIntegrityError(
                    "event evidence document is not in its announcement revision"
                )
            asset = self._session.get(DocumentAsset, row.document_asset_id)
            extraction = self._session.get(DocumentTextExtraction, row.text_extraction_id)
            if (
                asset is None
                or extraction is None
                or asset.document_id != document.id
                or extraction.document_asset_id != asset.id
                or asset.status not in {"verified", "accepted_unverified"}
                or extraction.status != "success"
            ):
                raise BusinessEventReadIntegrityError("invalid document extraction lineage")
            page_number = page_numbers[0]
            page_map = next(
                (
                    value
                    for value in extraction.page_map_json
                    if value.get("page_number") == page_number
                ),
                None,
            )
            page_start = None if page_map is None else page_map.get("start_offset")
            page_end = None if page_map is None else page_map.get("end_offset")
            if (
                page_map is None
                or page_map.get("page_text_sha256") != page_hashes[0]
                or not isinstance(page_start, int)
                or isinstance(page_start, bool)
                or not isinstance(page_end, int)
                or isinstance(page_end, bool)
                or row.start_offset < page_start
                or row.end_offset > page_end
            ):
                raise BusinessEventReadIntegrityError(
                    "event evidence does not match its extraction page map"
                )
            asset_view = _asset_view(asset)
            extraction_view = _extraction_view(extraction)
        else:
            raise BusinessEventReadIntegrityError("unknown event evidence kind")
        return BusinessEventEvidenceView(
            id=row.id,
            evidence_kind=row.evidence_kind,
            document=document,
            document_asset=asset_view,
            text_extraction=extraction_view,
            rule_code=row.rule_code,
            rule_semantic_version=row.rule_semantic_version,
            start_offset=row.start_offset,
            end_offset=row.end_offset,
            page_numbers=page_numbers,
            page_text_sha256s=page_hashes,
            excerpt_text=row.excerpt_text,
            excerpt_sha256=row.excerpt_sha256,
            source_available_at=_as_utc(row.source_available_at),
            evidence_fingerprint_sha256=row.evidence_fingerprint_sha256,
        )


def _asset_view(asset: DocumentAsset) -> DocumentAssetView:
    return DocumentAssetView(
        id=asset.id,
        document_id=asset.document_id,
        content_sha256=asset.content_sha256,
        object_key=asset.object_key,
        size_bytes=asset.size_bytes,
        requested_uri=asset.requested_uri,
        resolved_uri=asset.resolved_uri,
        declared_media_type=asset.declared_media_type,
        detected_media_type=asset.detected_media_type,
        retrieved_at=_as_utc(asset.retrieved_at),
        status=asset.status,
        warnings=tuple(str(value) for value in asset.warnings_json),
    )


def _extraction_view(extraction: DocumentTextExtraction) -> DocumentTextExtractionView:
    return DocumentTextExtractionView(
        id=extraction.id,
        document_asset_id=extraction.document_asset_id,
        extractor_code=extraction.extractor_code,
        extractor_semantic_version=extraction.extractor_semantic_version,
        extractor_runtime_version=extraction.extractor_runtime_version,
        text_object_key=extraction.text_object_key,
        text_sha256=extraction.text_sha256,
        character_count=extraction.character_count,
        page_count=extraction.page_count,
        status=extraction.status,
        warnings=tuple(str(value) for value in extraction.warnings_json),
        extracted_at=_as_utc(extraction.extracted_at),
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
