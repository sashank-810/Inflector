"""Provider-independent document acquisition and deterministic text contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

DEFAULT_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


class DocumentFetchError(ValueError):
    """Raised when a fetcher cannot safely return complete document bytes."""


class DocumentExtractionError(ValueError):
    """Deterministic extractor failure carrying a stable warning code."""

    def __init__(self, warning_code: str) -> None:
        super().__init__(warning_code)
        self.warning_code = warning_code


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """Exact bytes and operational retrieval metadata returned by a fetcher."""

    requested_uri: str
    resolved_uri: str | None
    content: bytes
    media_type: str | None
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")


class DocumentFetcher(Protocol):
    """Database-free port for bounded retrieval of exact document bytes."""

    def fetch(self, *, uri: str, max_bytes: int) -> FetchedDocument:
        """Fetch a complete document without exceeding the explicit byte limit."""

        ...


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """One independently extracted, one-based document page."""

    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class ExtractedDocumentText:
    """Deterministic page text without interpretation."""

    pages: tuple[ExtractedPage, ...]
    warnings: tuple[str, ...] = ()


class DocumentTextExtractor(Protocol):
    """Database-free deterministic text extractor contract."""

    extractor_code: str
    extractor_semantic_version: str
    extractor_runtime_version: str
    supported_media_types: frozenset[str]

    def extract(self, *, content: bytes, detected_media_type: str) -> ExtractedDocumentText:
        """Extract page text from exact immutable bytes without interpretation."""

        ...
