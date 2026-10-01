"""Narrow offline document fetchers for Phase 6B."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from inflector_core.document_processing import (
    DocumentFetchError,
    FetchedDocument,
)
from inflector_data.nse_http import (
    NSEAcquisitionError,
    NSEArtifactTooLargeError,
    NSEHttpClient,
    validate_nse_url,
)


class NSEOfficialDocumentFetcher:
    """Fetch only exact allowlisted official NSE document URLs."""

    def __init__(self, client: NSEHttpClient) -> None:
        self._client = client

    def fetch(self, *, uri: str, max_bytes: int) -> FetchedDocument:
        if max_bytes <= 0:
            raise DocumentFetchError("max_bytes must be positive")
        try:
            validate_nse_url(uri)
            artifact = self._client.acquire(
                uri,
                maximum_response_bytes=max_bytes,
            )
        except NSEArtifactTooLargeError as error:
            raise DocumentFetchError("document_exceeds_max_bytes") from error
        except (NSEAcquisitionError, ValueError) as error:
            raise DocumentFetchError("official_nse_document_fetch_failed") from error
        return FetchedDocument(
            requested_uri=uri,
            resolved_uri=artifact.source_uri,
            content=artifact.payload,
            media_type=artifact.media_type,
            retrieved_at=artifact.retrieved_at,
        )


class LocalFileDocumentFetcher:
    """Read fixture/development files only from one explicitly configured root."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._clock = clock or (lambda: datetime.now(UTC))

    def fetch(self, *, uri: str, max_bytes: int) -> FetchedDocument:
        if max_bytes <= 0:
            raise DocumentFetchError("max_bytes must be positive")
        direct_path = Path(uri)
        if direct_path.is_absolute():
            raw_path = uri
        else:
            parsed = urlparse(uri)
            if parsed.scheme not in {"", "file"}:
                raise DocumentFetchError("unsupported_document_uri_scheme")
            if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
                raise DocumentFetchError("file_uri_authority_is_not_allowed")
            raw_path = (
                url2pathname(unquote(parsed.path)) if parsed.scheme == "file" else uri
            )
        if not raw_path:
            raise DocumentFetchError("missing_document_path")
        candidate = Path(raw_path)
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self._root / candidate).resolve()
        )
        if not target.is_relative_to(self._root):
            raise DocumentFetchError("document_path_escapes_configured_root")
        if not target.is_file():
            raise DocumentFetchError("document_file_not_found")
        if target.stat().st_size > max_bytes:
            raise DocumentFetchError("document_exceeds_max_bytes")
        content = target.read_bytes()
        if len(content) > max_bytes:
            raise DocumentFetchError("document_exceeds_max_bytes")
        media_type, _ = mimetypes.guess_type(target.name)
        return FetchedDocument(
            requested_uri=uri,
            resolved_uri=target.as_uri(),
            content=content,
            media_type=media_type,
            retrieved_at=self._clock(),
        )


class MockDocumentFetcher:
    """Deterministic in-memory sequence fetcher for synthetic tests."""

    def __init__(self, responses: dict[str, tuple[FetchedDocument, ...]]) -> None:
        self._responses = responses
        self._positions: dict[str, int] = {}

    def fetch(self, *, uri: str, max_bytes: int) -> FetchedDocument:
        if max_bytes <= 0:
            raise DocumentFetchError("max_bytes must be positive")
        responses = self._responses.get(uri)
        if not responses:
            raise DocumentFetchError("mock_document_not_found")
        position = self._positions.get(uri, 0)
        response = responses[min(position, len(responses) - 1)]
        self._positions[uri] = position + 1
        if response.requested_uri != uri:
            raise DocumentFetchError("mock_requested_uri_mismatch")
        if len(response.content) > max_bytes:
            raise DocumentFetchError("document_exceeds_max_bytes")
        return response
