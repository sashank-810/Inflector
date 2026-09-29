"""Persistence boundary for deterministic business events and source evidence."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_database.models import (
    Announcement,
    AnnouncementDocument,
    BusinessEvent,
    BusinessEventEvidence,
    Document,
    DocumentAsset,
    DocumentTextExtraction,
    SourceRecord,
)


class BusinessEventRepository:
    """Own database operations for the Phase 6C-A event spine."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def event(
        self,
        *,
        announcement_id: UUID,
        event_type: str,
        ruleset_code: str,
        ruleset_semantic_version: str,
    ) -> BusinessEvent | None:
        return self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.announcement_id == announcement_id,
                BusinessEvent.event_type == event_type,
                BusinessEvent.ruleset_code == ruleset_code,
                BusinessEvent.ruleset_semantic_version == ruleset_semantic_version,
            )
        )

    def announcement_lineage(
        self, announcement_id: UUID
    ) -> tuple[Announcement, SourceRecord] | None:
        row = self.session.execute(
            select(Announcement, SourceRecord)
            .join(SourceRecord, Announcement.source_record_id == SourceRecord.id)
            .where(Announcement.id == announcement_id)
        ).first()
        return None if row is None else (row[0], row[1])

    def announcement_documents(
        self, announcement_id: UUID
    ) -> list[tuple[AnnouncementDocument, Document, SourceRecord]]:
        return [
            (row[0], row[1], row[2])
            for row in self.session.execute(
                select(AnnouncementDocument, Document, SourceRecord)
                .join(Document, AnnouncementDocument.document_id == Document.id)
                .join(SourceRecord, Document.source_record_id == SourceRecord.id)
                .where(
                    AnnouncementDocument.announcement_id == announcement_id,
                    SourceRecord.validation_status == "accepted",
                )
            )
        ]

    def evidence_for_event(self, business_event_id: UUID) -> list[BusinessEventEvidence]:
        return list(
            self.session.scalars(
                select(BusinessEventEvidence)
                .where(BusinessEventEvidence.business_event_id == business_event_id)
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

    def extraction_lineage(
        self,
        *,
        document_asset_id: UUID,
        text_extraction_id: UUID,
    ) -> tuple[DocumentAsset, DocumentTextExtraction] | None:
        row = self.session.execute(
            select(DocumentAsset, DocumentTextExtraction)
            .join(
                DocumentTextExtraction,
                DocumentTextExtraction.document_asset_id == DocumentAsset.id,
            )
            .where(
                DocumentAsset.id == document_asset_id,
                DocumentTextExtraction.id == text_extraction_id,
            )
        ).first()
        return None if row is None else (row[0], row[1])

    def add_event(
        self,
        *,
        company_id: UUID,
        security_id: UUID | None,
        announcement_id: UUID,
        provider_dataset_id: UUID,
        event_type: str,
        source_event_date: date | None,
        source_available_at: datetime,
        ruleset_code: str,
        ruleset_semantic_version: str,
        matched_rule_codes: tuple[str, ...],
        detection_fingerprint_sha256: str,
        derived_at: datetime,
    ) -> BusinessEvent:
        event = BusinessEvent(
            company_id=company_id,
            security_id=security_id,
            announcement_id=announcement_id,
            provider_dataset_id=provider_dataset_id,
            event_type=event_type,
            source_event_date=source_event_date,
            source_available_at=source_available_at,
            ruleset_code=ruleset_code,
            ruleset_semantic_version=ruleset_semantic_version,
            matched_rule_codes_json=list(matched_rule_codes),
            status="detected",
            warnings_json=[],
            detection_fingerprint_sha256=detection_fingerprint_sha256,
            derived_at=derived_at,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def add_evidence(
        self,
        *,
        business_event_id: UUID,
        announcement_id: UUID,
        evidence_kind: str,
        document_id: UUID | None,
        document_asset_id: UUID | None,
        text_extraction_id: UUID | None,
        rule_code: str,
        rule_semantic_version: str,
        start_offset: int,
        end_offset: int,
        page_numbers: tuple[int, ...],
        page_text_sha256s: tuple[str, ...],
        excerpt_text: str,
        excerpt_sha256: str,
        source_available_at: datetime,
        evidence_fingerprint_sha256: str,
    ) -> BusinessEventEvidence:
        evidence = BusinessEventEvidence(
            business_event_id=business_event_id,
            announcement_id=announcement_id,
            evidence_kind=evidence_kind,
            document_id=document_id,
            document_asset_id=document_asset_id,
            text_extraction_id=text_extraction_id,
            rule_code=rule_code,
            rule_semantic_version=rule_semantic_version,
            start_offset=start_offset,
            end_offset=end_offset,
            page_numbers_json=list(page_numbers),
            page_text_sha256s_json=list(page_text_sha256s),
            excerpt_text=excerpt_text,
            excerpt_sha256=excerpt_sha256,
            source_available_at=source_available_at,
            evidence_fingerprint_sha256=evidence_fingerprint_sha256,
        )
        self.session.add(evidence)
        self.session.flush()
        return evidence
