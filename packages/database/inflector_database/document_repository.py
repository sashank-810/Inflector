"""Persistence boundary for immutable document assets and text extractions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_database.models import (
    Document,
    DocumentAsset,
    DocumentTextExtraction,
    SourceRecord,
)


class DocumentEvidenceRepository:
    """Own database operations for the Phase 6B evidence spine."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def document_by_id(self, document_id: UUID) -> tuple[Document, SourceRecord] | None:
        row = self.session.execute(
            select(Document, SourceRecord)
            .join(SourceRecord, Document.source_record_id == SourceRecord.id)
            .where(Document.id == document_id)
        ).first()
        return None if row is None else (row[0], row[1])

    def assets_for_document(self, document_id: UUID) -> list[DocumentAsset]:
        return list(
            self.session.scalars(
                select(DocumentAsset)
                .where(DocumentAsset.document_id == document_id)
                .order_by(DocumentAsset.created_at, DocumentAsset.id)
            )
        )

    def asset_by_document_and_hash(
        self, *, document_id: UUID, content_sha256: str
    ) -> DocumentAsset | None:
        return self.session.scalar(
            select(DocumentAsset).where(
                DocumentAsset.document_id == document_id,
                DocumentAsset.content_sha256 == content_sha256,
            )
        )

    def asset_by_id(self, document_asset_id: UUID) -> DocumentAsset | None:
        return self.session.get(DocumentAsset, document_asset_id)

    def add_document_asset(
        self,
        *,
        document_id: UUID,
        content_sha256: str,
        object_key: str,
        size_bytes: int,
        requested_uri: str,
        resolved_uri: str | None,
        declared_media_type: str | None,
        detected_media_type: str | None,
        retrieved_at: datetime,
        status: str,
        warnings: tuple[str, ...],
    ) -> DocumentAsset:
        asset = DocumentAsset(
            document_id=document_id,
            content_sha256=content_sha256,
            object_key=object_key,
            size_bytes=size_bytes,
            requested_uri=requested_uri,
            resolved_uri=resolved_uri,
            declared_media_type=declared_media_type,
            detected_media_type=detected_media_type,
            retrieved_at=retrieved_at,
            status=status,
            warnings_json=list(warnings),
        )
        self.session.add(asset)
        self.session.flush()
        return asset

    def text_extraction(
        self,
        *,
        document_asset_id: UUID,
        extractor_code: str,
        extractor_semantic_version: str,
        extractor_runtime_version: str,
    ) -> DocumentTextExtraction | None:
        return self.session.scalar(
            select(DocumentTextExtraction).where(
                DocumentTextExtraction.document_asset_id == document_asset_id,
                DocumentTextExtraction.extractor_code == extractor_code,
                DocumentTextExtraction.extractor_semantic_version
                == extractor_semantic_version,
                DocumentTextExtraction.extractor_runtime_version == extractor_runtime_version,
            )
        )

    def add_text_extraction(
        self,
        *,
        document_asset_id: UUID,
        extractor_code: str,
        extractor_semantic_version: str,
        extractor_runtime_version: str,
        text_object_key: str | None,
        text_sha256: str | None,
        character_count: int | None,
        page_count: int | None,
        page_map: list[dict[str, object]],
        status: str,
        warnings: tuple[str, ...],
        extracted_at: datetime,
    ) -> DocumentTextExtraction:
        extraction = DocumentTextExtraction(
            document_asset_id=document_asset_id,
            extractor_code=extractor_code,
            extractor_semantic_version=extractor_semantic_version,
            extractor_runtime_version=extractor_runtime_version,
            text_object_key=text_object_key,
            text_sha256=text_sha256,
            character_count=character_count,
            page_count=page_count,
            page_map_json=page_map,
            status=status,
            warnings_json=list(warnings),
            extracted_at=extracted_at,
        )
        self.session.add(extraction)
        self.session.flush()
        return extraction
