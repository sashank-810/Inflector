"""Deterministic plain-text and PDF text extractors without OCR."""

from __future__ import annotations

import sys
from io import BytesIO

import pypdf
from pypdf import PdfReader

from inflector_core.document_processing import (
    DocumentExtractionError,
    ExtractedDocumentText,
    ExtractedPage,
)


def normalize_line_endings(value: str) -> str:
    """Apply the only Phase 6B canonical text normalization."""

    return value.replace("\r\n", "\n").replace("\r", "\n")


class PlainTextExtractor:
    """Strict UTF-8 plain-text extractor with line-ending normalization only."""

    extractor_code = "plain_text"
    extractor_semantic_version = "plain_text_v1"
    extractor_runtime_version = (
        f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    supported_media_types = frozenset({"text/plain"})

    def extract(self, *, content: bytes, detected_media_type: str) -> ExtractedDocumentText:
        if detected_media_type not in self.supported_media_types:
            raise DocumentExtractionError("unsupported_media_type")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DocumentExtractionError("invalid_utf8_text") from error
        return ExtractedDocumentText((ExtractedPage(1, normalize_line_endings(text)),))


class PyPdfTextExtractor:
    """Page-preserving pypdf extraction with no OCR or password guessing."""

    extractor_code = "pypdf"
    extractor_semantic_version = "pypdf_text_v1"
    extractor_runtime_version = pypdf.__version__
    supported_media_types = frozenset({"application/pdf"})

    def extract(self, *, content: bytes, detected_media_type: str) -> ExtractedDocumentText:
        if detected_media_type not in self.supported_media_types:
            raise DocumentExtractionError("unsupported_media_type")
        try:
            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise DocumentExtractionError("encrypted_or_unreadable_pdf")
            pages = tuple(
                ExtractedPage(index, normalize_line_endings(page.extract_text() or ""))
                for index, page in enumerate(reader.pages, start=1)
            )
        except DocumentExtractionError:
            raise
        except Exception as error:
            raise DocumentExtractionError("encrypted_or_unreadable_pdf") from error
        has_text = any(page.text for page in pages)
        warnings = ("empty_pdf_page",) if has_text and any(not page.text for page in pages) else ()
        return ExtractedDocumentText(pages, warnings)
