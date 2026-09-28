"""Phase 6B acquisition and deterministic text-extraction orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_core.document_processing import (
    DEFAULT_MAX_DOCUMENT_BYTES,
    DocumentExtractionError,
    DocumentFetcher,
    DocumentTextExtractor,
    ExtractedPage,
)
from inflector_data.archive import ObjectStoreIntegrityError, RawObjectStore
from inflector_database.document_repository import DocumentEvidenceRepository
from inflector_database.models import DocumentAsset, DocumentTextExtraction

ASSET_ACCEPTED_STATUSES = frozenset({"verified", "accepted_unverified"})
PAGE_SEPARATOR = "\n\f\n"


class DocumentEvidenceIntegrityError(ValueError):
    """Raised when persisted document evidence and immutable bytes diverge."""


@dataclass(frozen=True, slots=True)
class DocumentAssetResult:
    asset_id: UUID
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
    created: bool


@dataclass(frozen=True, slots=True)
class DocumentTextExtractionResult:
    extraction_id: UUID
    document_asset_id: UUID
    extractor_code: str
    extractor_semantic_version: str
    extractor_runtime_version: str
    text_object_key: str | None
    text_sha256: str | None
    character_count: int | None
    page_count: int | None
    page_map: tuple[dict[str, object], ...]
    status: str
    warnings: tuple[str, ...]
    extracted_at: datetime
    created: bool


def read_integrity_checked(
    store: RawObjectStore,
    *,
    object_key: str,
    expected_sha256: str,
) -> bytes:
    """Read immutable bytes and fail closed on metadata/content divergence."""

    content = store.get(object_key)
    if sha256(content).hexdigest() != expected_sha256:
        raise ObjectStoreIntegrityError("stored object SHA-256 does not match persisted metadata")
    return content


def detect_media_type(content: bytes, declared_media_type: str | None) -> str:
    """Detect only the narrow Phase 6B PDF/plain-text media set."""

    if content.startswith(b"%PDF-"):
        return "application/pdf"
    declared = _base_media_type(declared_media_type)
    if declared == "text/plain":
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            return "application/octet-stream"
        if "\x00" not in decoded:
            return "text/plain"
    return "application/octet-stream"


class DocumentAcquisitionService:
    """Acquire exact bytes without changing Phase 6A source knowledge time."""

    def __init__(self, session: Session, object_store: RawObjectStore) -> None:
        self._repository = DocumentEvidenceRepository(session)
        self._object_store = object_store

    def acquire(
        self,
        *,
        document_id: UUID,
        fetcher: DocumentFetcher,
        max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
    ) -> DocumentAssetResult:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        row = self._repository.document_by_id(document_id)
        if row is None:
            raise ValueError("unknown document_id")
        document, source = row
        if source.validation_status != "accepted":
            raise ValueError("document SourceRecord must be accepted")
        if source.provider_dataset_id != document.provider_dataset_id:
            raise DocumentEvidenceIntegrityError("document source provider mismatch")

        fetched = fetcher.fetch(uri=document.document_uri, max_bytes=max_bytes)
        if fetched.requested_uri != document.document_uri:
            raise DocumentEvidenceIntegrityError("fetcher requested URI does not match document")
        if len(fetched.content) > max_bytes:
            raise ValueError("fetcher returned content above max_bytes")
        archived = self._object_store.put(fetched.content)
        actual_hash = sha256(fetched.content).hexdigest()
        if archived.content_sha256 != actual_hash or archived.size_bytes != len(fetched.content):
            raise DocumentEvidenceIntegrityError("object store returned inconsistent metadata")

        existing = self._repository.asset_by_document_and_hash(
            document_id=document.id,
            content_sha256=actual_hash,
        )
        if existing is not None:
            return self._asset_result(existing, created=False)

        declared_media_type = document.media_type or fetched.media_type
        detected_media_type = detect_media_type(fetched.content, declared_media_type)
        warnings = list(_media_warnings(declared_media_type, detected_media_type))
        if not fetched.content:
            status = "invalid_empty_content"
            warnings.append("empty_document_content")
        elif document.document_content_sha256 is not None:
            if actual_hash == document.document_content_sha256.lower():
                status = "verified"
            else:
                status = "rejected_hash_mismatch"
                warnings.append("document_hash_mismatch")
        elif any(
            asset.status == "accepted_unverified"
            for asset in self._repository.assets_for_document(document.id)
        ):
            status = "rejected_content_conflict"
            warnings.append("document_content_changed_without_metadata_revision")
        else:
            status = "accepted_unverified"

        asset = self._repository.add_document_asset(
            document_id=document.id,
            content_sha256=actual_hash,
            object_key=archived.object_key,
            size_bytes=archived.size_bytes,
            requested_uri=fetched.requested_uri,
            resolved_uri=fetched.resolved_uri,
            declared_media_type=declared_media_type,
            detected_media_type=detected_media_type,
            retrieved_at=fetched.retrieved_at.astimezone(UTC),
            status=status,
            warnings=_deduplicate(warnings),
        )
        return self._asset_result(asset, created=True)

    @staticmethod
    def _asset_result(asset: DocumentAsset, *, created: bool) -> DocumentAssetResult:
        return DocumentAssetResult(
            asset_id=asset.id,
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
            created=created,
        )


class DocumentTextExtractionService:
    """Persist deterministic text tied to exact asset and extractor identities."""

    def __init__(
        self,
        session: Session,
        object_store: RawObjectStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = DocumentEvidenceRepository(session)
        self._object_store = object_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def extract(
        self,
        *,
        document_asset_id: UUID,
        extractor: DocumentTextExtractor,
    ) -> DocumentTextExtractionResult:
        asset = self._repository.asset_by_id(document_asset_id)
        if asset is None:
            raise ValueError("unknown document_asset_id")
        if asset.status not in ASSET_ACCEPTED_STATUSES:
            raise ValueError("document asset status is not extractable")
        existing = self._repository.text_extraction(
            document_asset_id=asset.id,
            extractor_code=extractor.extractor_code,
            extractor_semantic_version=extractor.extractor_semantic_version,
            extractor_runtime_version=extractor.extractor_runtime_version,
        )
        if existing is not None:
            return self._extraction_result(existing, created=False)

        content = read_integrity_checked(
            self._object_store,
            object_key=asset.object_key,
            expected_sha256=asset.content_sha256,
        )
        extracted_at = self._clock()
        if extracted_at.tzinfo is None or extracted_at.utcoffset() is None:
            raise ValueError("extraction clock must return a timezone-aware timestamp")
        detected_media_type = asset.detected_media_type or "application/octet-stream"
        if detected_media_type not in extractor.supported_media_types:
            extraction = self._persist_unavailable(
                asset=asset,
                extractor=extractor,
                status="unsupported_media_type",
                warnings=("unsupported_media_type",),
                extracted_at=extracted_at,
            )
            return self._extraction_result(extraction, created=True)

        try:
            result = extractor.extract(
                content=content,
                detected_media_type=detected_media_type,
            )
            self._validate_pages(result.pages)
        except DocumentExtractionError as error:
            extraction = self._persist_unavailable(
                asset=asset,
                extractor=extractor,
                status="extraction_failed",
                warnings=(error.warning_code,),
                extracted_at=extracted_at,
            )
            return self._extraction_result(extraction, created=True)
        except Exception:
            extraction = self._persist_unavailable(
                asset=asset,
                extractor=extractor,
                status="extraction_failed",
                warnings=("extraction_failed",),
                extracted_at=extracted_at,
            )
            return self._extraction_result(extraction, created=True)

        page_map, full_text = _compose_pages(result.pages)
        if not any(page.text for page in result.pages):
            warnings = _deduplicate([*result.warnings, "no_extractable_text"])
            extraction = self._repository.add_text_extraction(
                document_asset_id=asset.id,
                extractor_code=extractor.extractor_code,
                extractor_semantic_version=extractor.extractor_semantic_version,
                extractor_runtime_version=extractor.extractor_runtime_version,
                text_object_key=None,
                text_sha256=None,
                character_count=len(full_text),
                page_count=len(result.pages),
                page_map=page_map,
                status="no_extractable_text",
                warnings=warnings,
                extracted_at=extracted_at.astimezone(UTC),
            )
            return self._extraction_result(extraction, created=True)

        encoded = full_text.encode("utf-8")
        text_object = self._object_store.put(encoded)
        text_hash = sha256(encoded).hexdigest()
        if text_object.content_sha256 != text_hash:
            raise DocumentEvidenceIntegrityError("text object store returned inconsistent hash")
        extraction = self._repository.add_text_extraction(
            document_asset_id=asset.id,
            extractor_code=extractor.extractor_code,
            extractor_semantic_version=extractor.extractor_semantic_version,
            extractor_runtime_version=extractor.extractor_runtime_version,
            text_object_key=text_object.object_key,
            text_sha256=text_hash,
            character_count=len(full_text),
            page_count=len(result.pages),
            page_map=page_map,
            status="success",
            warnings=_deduplicate(list(result.warnings)),
            extracted_at=extracted_at.astimezone(UTC),
        )
        return self._extraction_result(extraction, created=True)

    def _persist_unavailable(
        self,
        *,
        asset: DocumentAsset,
        extractor: DocumentTextExtractor,
        status: str,
        warnings: tuple[str, ...],
        extracted_at: datetime,
    ) -> DocumentTextExtraction:
        return self._repository.add_text_extraction(
            document_asset_id=asset.id,
            extractor_code=extractor.extractor_code,
            extractor_semantic_version=extractor.extractor_semantic_version,
            extractor_runtime_version=extractor.extractor_runtime_version,
            text_object_key=None,
            text_sha256=None,
            character_count=None,
            page_count=None,
            page_map=[],
            status=status,
            warnings=warnings,
            extracted_at=extracted_at.astimezone(UTC),
        )

    @staticmethod
    def _validate_pages(pages: tuple[ExtractedPage, ...]) -> None:
        for expected, page in enumerate(pages, start=1):
            if page.page_number != expected:
                raise DocumentEvidenceIntegrityError("extractor page numbers must be contiguous")

    @staticmethod
    def _extraction_result(
        extraction: DocumentTextExtraction,
        *,
        created: bool,
    ) -> DocumentTextExtractionResult:
        return DocumentTextExtractionResult(
            extraction_id=extraction.id,
            document_asset_id=extraction.document_asset_id,
            extractor_code=extraction.extractor_code,
            extractor_semantic_version=extraction.extractor_semantic_version,
            extractor_runtime_version=extraction.extractor_runtime_version,
            text_object_key=extraction.text_object_key,
            text_sha256=extraction.text_sha256,
            character_count=extraction.character_count,
            page_count=extraction.page_count,
            page_map=tuple(dict(value) for value in extraction.page_map_json),
            status=extraction.status,
            warnings=tuple(str(value) for value in extraction.warnings_json),
            extracted_at=_as_utc(extraction.extracted_at),
            created=created,
        )


def _compose_pages(pages: tuple[ExtractedPage, ...]) -> tuple[list[dict[str, object]], str]:
    page_map: list[dict[str, object]] = []
    page_texts: list[str] = []
    offset = 0
    for index, page in enumerate(pages):
        text = page.text
        start = offset
        end = start + len(text)
        page_map.append(
            {
                "page_number": page.page_number,
                "start_offset": start,
                "end_offset": end,
                "page_text_sha256": sha256(text.encode("utf-8")).hexdigest(),
            }
        )
        page_texts.append(text)
        offset = end + (len(PAGE_SEPARATOR) if index < len(pages) - 1 else 0)
    return page_map, PAGE_SEPARATOR.join(page_texts)


def _media_warnings(declared: str | None, detected: str) -> tuple[str, ...]:
    normalized = _base_media_type(declared)
    if normalized == "application/pdf" and detected != "application/pdf":
        return ("declared_pdf_content_mismatch",)
    if normalized == "text/plain" and detected != "text/plain":
        return ("declared_text_content_mismatch",)
    return ()


def _base_media_type(value: str | None) -> str | None:
    return None if value is None else value.split(";", 1)[0].strip().lower()


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
