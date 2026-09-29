"""Persistence boundary for quantitative business-event derivations."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_database.models import (
    Announcement,
    BusinessEvent,
    BusinessEventEvidence,
    BusinessEventQuantitativeDerivation,
    BusinessEventQuantitativeFact,
    DocumentAsset,
    DocumentTextExtraction,
)


class BusinessEventQuantitativeRepository:
    """Own database access for Phase 6C-B immutable outcomes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def event(self, event_id: UUID) -> BusinessEvent | None:
        return self.session.get(BusinessEvent, event_id)

    def announcement(self, announcement_id: UUID) -> Announcement | None:
        return self.session.get(Announcement, announcement_id)

    def evidence_for_event(self, event_id: UUID) -> list[BusinessEventEvidence]:
        return list(
            self.session.scalars(
                select(BusinessEventEvidence)
                .where(BusinessEventEvidence.business_event_id == event_id)
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
        self, *, asset_id: UUID, extraction_id: UUID
    ) -> tuple[DocumentAsset, DocumentTextExtraction] | None:
        row = self.session.execute(
            select(DocumentAsset, DocumentTextExtraction)
            .join(
                DocumentTextExtraction,
                DocumentTextExtraction.document_asset_id == DocumentAsset.id,
            )
            .where(
                DocumentAsset.id == asset_id,
                DocumentTextExtraction.id == extraction_id,
            )
        ).first()
        return None if row is None else (row[0], row[1])

    def derivation(
        self,
        *,
        business_event_id: UUID,
        ruleset_code: str,
        ruleset_semantic_version: str,
    ) -> BusinessEventQuantitativeDerivation | None:
        return self.session.scalar(
            select(BusinessEventQuantitativeDerivation).where(
                BusinessEventQuantitativeDerivation.business_event_id == business_event_id,
                BusinessEventQuantitativeDerivation.ruleset_code == ruleset_code,
                BusinessEventQuantitativeDerivation.ruleset_semantic_version
                == ruleset_semantic_version,
            )
        )

    def facts(self, derivation_id: UUID) -> list[BusinessEventQuantitativeFact]:
        return list(
            self.session.scalars(
                select(BusinessEventQuantitativeFact)
                .where(BusinessEventQuantitativeFact.derivation_id == derivation_id)
                .order_by(
                    BusinessEventQuantitativeFact.fact_code,
                    BusinessEventQuantitativeFact.business_event_evidence_id,
                    BusinessEventQuantitativeFact.start_offset,
                    BusinessEventQuantitativeFact.end_offset,
                    BusinessEventQuantitativeFact.rule_code,
                    BusinessEventQuantitativeFact.fact_fingerprint_sha256,
                )
            )
        )

    def add_derivation(
        self,
        *,
        business_event_id: UUID,
        ruleset_code: str,
        ruleset_semantic_version: str,
        source_available_at: datetime,
        available_fact_codes: tuple[str, ...],
        warnings: tuple[str, ...],
        derivation_fingerprint_sha256: str,
        derived_at: datetime,
    ) -> BusinessEventQuantitativeDerivation:
        value = BusinessEventQuantitativeDerivation(
            business_event_id=business_event_id,
            ruleset_code=ruleset_code,
            ruleset_semantic_version=ruleset_semantic_version,
            source_available_at=source_available_at,
            available_fact_codes_json=list(available_fact_codes),
            warnings_json=list(warnings),
            derivation_fingerprint_sha256=derivation_fingerprint_sha256,
            derived_at=derived_at,
        )
        self.session.add(value)
        self.session.flush()
        return value

    def add_fact(
        self,
        *,
        derivation_id: UUID,
        business_event_evidence_id: UUID,
        fact_code: str,
        fact_kind: str,
        rule_code: str,
        rule_semantic_version: str,
        start_offset: int,
        end_offset: int,
        raw_text: str,
        raw_text_sha256: str,
        reported_value: Decimal | None,
        reported_scale: str | None,
        reported_unit: str | None,
        reported_currency: str | None,
        normalized_value: Decimal | None,
        normalized_unit: str | None,
        date_value: date | None,
        source_available_at: datetime,
        warnings: tuple[str, ...],
        fact_fingerprint_sha256: str,
    ) -> BusinessEventQuantitativeFact:
        value = BusinessEventQuantitativeFact(
            derivation_id=derivation_id,
            business_event_evidence_id=business_event_evidence_id,
            fact_code=fact_code,
            fact_kind=fact_kind,
            rule_code=rule_code,
            rule_semantic_version=rule_semantic_version,
            start_offset=start_offset,
            end_offset=end_offset,
            raw_text=raw_text,
            raw_text_sha256=raw_text_sha256,
            reported_value=reported_value,
            reported_scale=reported_scale,
            reported_unit=reported_unit,
            reported_currency=reported_currency,
            normalized_value=normalized_value,
            normalized_unit=normalized_unit,
            date_value=date_value,
            source_available_at=source_available_at,
            warnings_json=list(warnings),
            fact_fingerprint_sha256=fact_fingerprint_sha256,
        )
        self.session.add(value)
        self.session.flush()
        return value
