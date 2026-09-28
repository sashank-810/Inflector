"""Deterministic readers and citable slices over Phase 6B text evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from re import fullmatch
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.announcement_pit import PointInTimeDocument
from inflector_data.archive import RawObjectStore
from inflector_data.document_services import (
    ASSET_ACCEPTED_STATUSES,
    PAGE_SEPARATOR,
    DocumentEvidenceIntegrityError,
    read_integrity_checked,
)
from inflector_database.models import DocumentAsset, DocumentTextExtraction


@dataclass(frozen=True, slots=True)
class DocumentExtractionIdentity:
    extractor_code: str
    extractor_semantic_version: str
    extractor_runtime_version: str


@dataclass(frozen=True, slots=True)
class DocumentAssetView:
    id: UUID
    document_id: UUID
    content_sha256: str
    object_key: str
    size_bytes: int
    requested_uri: str
    resolved_uri: str | None
    declared_media_type: str | None
    detected_media_type: str | None
    retrieved_at: datetime
    status: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DocumentTextExtractionView:
    id: UUID
    document_asset_id: UUID
    extractor_code: str
    extractor_semantic_version: str
    extractor_runtime_version: str
    text_object_key: str | None
    text_sha256: str | None
    character_count: int | None
    page_count: int | None
    status: str
    warnings: tuple[str, ...]
    extracted_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentPageView:
    page_number: int
    start_offset: int
    end_offset: int
    page_text_sha256: str
    text: str | None


@dataclass(frozen=True, slots=True)
class PointInTimeDocumentText:
    document: PointInTimeDocument
    asset: DocumentAssetView
    extraction: DocumentTextExtractionView
    source_available_at: datetime
    text: str | None
    pages: tuple[DocumentPageView, ...]


@dataclass(frozen=True, slots=True)
class DocumentEvidenceSlice:
    document: PointInTimeDocument
    asset: DocumentAssetView
    extraction: DocumentTextExtractionView
    source_available_at: datetime
    start_offset: int
    end_offset: int
    excerpt: str
    page_numbers: tuple[int, ...]
    page_text_sha256s: tuple[str, ...]


class DocumentTextReader:
    """Read explicit extractor identity for an already PIT-selected document."""

    def __init__(self, session: Session, object_store: RawObjectStore) -> None:
        self._session = session
        self._object_store = object_store

    def text_for_document(
        self,
        *,
        document: PointInTimeDocument,
        extraction_identity: DocumentExtractionIdentity,
    ) -> PointInTimeDocumentText | None:
        rows = list(
            self._session.execute(
                select(DocumentAsset, DocumentTextExtraction)
                .join(
                    DocumentTextExtraction,
                    DocumentTextExtraction.document_asset_id == DocumentAsset.id,
                )
                .where(
                    DocumentAsset.document_id == document.id,
                    DocumentAsset.status.in_(ASSET_ACCEPTED_STATUSES),
                    DocumentTextExtraction.extractor_code
                    == extraction_identity.extractor_code,
                    DocumentTextExtraction.extractor_semantic_version
                    == extraction_identity.extractor_semantic_version,
                    DocumentTextExtraction.extractor_runtime_version
                    == extraction_identity.extractor_runtime_version,
                )
            )
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise DocumentEvidenceIntegrityError(
                "multiple accepted assets match one document/extractor identity"
            )
        asset, extraction = rows[0][0], rows[0][1]
        text = self._read_text(extraction)
        pages = self._page_views(extraction, text)
        return PointInTimeDocumentText(
            document=document,
            asset=self._asset_view(asset),
            extraction=self._extraction_view(extraction),
            source_available_at=_as_utc(document.available_at),
            text=text,
            pages=pages,
        )

    def page_evidence(
        self,
        *,
        document: PointInTimeDocument,
        extraction_identity: DocumentExtractionIdentity,
        page_number: int,
    ) -> DocumentEvidenceSlice:
        value = self._required_success(document, extraction_identity)
        page = next((item for item in value.pages if item.page_number == page_number), None)
        if page is None or page.text is None:
            raise ValueError("page_number is outside the extracted document")
        return DocumentEvidenceSlice(
            document=value.document,
            asset=value.asset,
            extraction=value.extraction,
            source_available_at=value.source_available_at,
            start_offset=page.start_offset,
            end_offset=page.end_offset,
            excerpt=page.text,
            page_numbers=(page.page_number,),
            page_text_sha256s=(page.page_text_sha256,),
        )

    def excerpt_evidence(
        self,
        *,
        document: PointInTimeDocument,
        extraction_identity: DocumentExtractionIdentity,
        start_offset: int,
        end_offset: int,
    ) -> DocumentEvidenceSlice:
        value = self._required_success(document, extraction_identity)
        assert value.text is not None
        if not 0 <= start_offset < end_offset <= len(value.text):
            raise ValueError("excerpt offsets must satisfy 0 <= start < end <= character_count")
        pages = tuple(
            page
            for page in value.pages
            if start_offset < page.end_offset and end_offset > page.start_offset
        )
        return DocumentEvidenceSlice(
            document=value.document,
            asset=value.asset,
            extraction=value.extraction,
            source_available_at=value.source_available_at,
            start_offset=start_offset,
            end_offset=end_offset,
            excerpt=value.text[start_offset:end_offset],
            page_numbers=tuple(page.page_number for page in pages),
            page_text_sha256s=tuple(page.page_text_sha256 for page in pages),
        )

    def _required_success(
        self,
        document: PointInTimeDocument,
        extraction_identity: DocumentExtractionIdentity,
    ) -> PointInTimeDocumentText:
        value = self.text_for_document(
            document=document,
            extraction_identity=extraction_identity,
        )
        if value is None:
            raise ValueError("explicit document extraction was not found")
        if value.extraction.status != "success" or value.text is None:
            raise ValueError("document extraction has no successful text")
        return value

    def _read_text(self, extraction: DocumentTextExtraction) -> str | None:
        if extraction.status != "success":
            if extraction.text_object_key is not None or extraction.text_sha256 is not None:
                raise DocumentEvidenceIntegrityError(
                    "non-success extraction must not reference a text object"
                )
            return None
        if extraction.text_object_key is None or extraction.text_sha256 is None:
            raise DocumentEvidenceIntegrityError("successful extraction is missing text lineage")
        content = read_integrity_checked(
            self._object_store,
            object_key=extraction.text_object_key,
            expected_sha256=extraction.text_sha256,
        )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DocumentEvidenceIntegrityError("stored text object is not UTF-8") from error
        if extraction.character_count != len(text):
            raise DocumentEvidenceIntegrityError("stored text character count mismatch")
        return text

    @staticmethod
    def _page_views(
        extraction: DocumentTextExtraction,
        text: str | None,
    ) -> tuple[DocumentPageView, ...]:
        pages: list[DocumentPageView] = []
        expected_start = 0
        empty_hash = sha256(b"").hexdigest()
        for expected_page, raw in enumerate(extraction.page_map_json, start=1):
            page_number = raw.get("page_number")
            start_offset = raw.get("start_offset")
            end_offset = raw.get("end_offset")
            page_hash = raw.get("page_text_sha256")
            if (
                not isinstance(page_number, int)
                or isinstance(page_number, bool)
                or not isinstance(start_offset, int)
                or isinstance(start_offset, bool)
                or not isinstance(end_offset, int)
                or isinstance(end_offset, bool)
                or not isinstance(page_hash, str)
                or fullmatch(r"[0-9a-f]{64}", page_hash) is None
            ):
                raise DocumentEvidenceIntegrityError("invalid persisted page map")
            if (
                page_number != expected_page
                or start_offset != expected_start
                or end_offset < start_offset
            ):
                raise DocumentEvidenceIntegrityError("invalid persisted page offsets")
            page_text = None if text is None else text[start_offset:end_offset]
            if page_text is not None and sha256(page_text.encode("utf-8")).hexdigest() != page_hash:
                raise DocumentEvidenceIntegrityError("persisted page text hash mismatch")
            if (
                text is None
                and extraction.status == "no_extractable_text"
                and (end_offset != start_offset or page_hash != empty_hash)
            ):
                raise DocumentEvidenceIntegrityError("invalid no-text page evidence")
            pages.append(
                DocumentPageView(
                    page_number=page_number,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    page_text_sha256=page_hash,
                    text=page_text,
                )
            )
            expected_start = end_offset + len(PAGE_SEPARATOR)
        if extraction.page_count is not None and extraction.page_count != len(pages):
            raise DocumentEvidenceIntegrityError("persisted page count mismatch")
        if text is not None and pages and pages[-1].end_offset != len(text):
            raise DocumentEvidenceIntegrityError("final page offset does not match text length")
        return tuple(pages)

    @staticmethod
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

    @staticmethod
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
