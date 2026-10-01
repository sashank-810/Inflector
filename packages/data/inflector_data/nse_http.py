"""Bounded HTTPS acquisition for allowlisted official NSE artifacts."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import CookieJar
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

NSE_ALLOWED_HOSTS = frozenset({"www.nseindia.com", "nsearchives.nseindia.com"})
NSE_HOMEPAGE_URL = "https://www.nseindia.com/"
NSE_USER_AGENT = (
    "Mozilla/5.0 (compatible; Inflector-Personal-Research/1.0; +local-research)"
)


class NSEAcquisitionError(RuntimeError):
    """Official artifact acquisition failed without producing provider data."""


class NSESourceNotAvailableError(NSEAcquisitionError):
    """The exact requested official artifact returned HTTP 404."""


class NSEArtifactTooLargeError(NSEAcquisitionError):
    """An official response exceeded the configured byte bound."""


class NSEHostNotAllowedError(NSEAcquisitionError):
    """A URL or redirect escaped the explicit NSE HTTPS allowlist."""


@dataclass(frozen=True, slots=True)
class AcquiredNSEArtifact:
    """Exact official response bytes and the observed UTC retrieval time."""

    source_uri: str
    payload: bytes
    retrieved_at: datetime
    media_type: str | None = None


class _HTTPResponse(Protocol):
    headers: object

    def read(self, amount: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def close(self) -> None: ...


def validate_nse_url(url: str) -> None:
    """Require HTTPS and one of the two approved official NSE hosts."""

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise NSEHostNotAllowedError("official NSE acquisition requires HTTPS")
    if parsed.hostname is None or parsed.hostname.lower() not in NSE_ALLOWED_HOSTS:
        raise NSEHostNotAllowedError("official NSE URL host is not allowlisted")
    if parsed.username is not None or parsed.password is not None:
        raise NSEHostNotAllowedError("credentials are not permitted in official NSE URLs")


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        validate_nse_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> OpenerDirector:
    return build_opener(HTTPCookieProcessor(CookieJar()), _AllowlistedRedirectHandler())


class NSEHttpClient:
    """Small cookie-preserving client with bounded retries and response sizes."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        maximum_response_bytes: int = 25_000_000,
        maximum_attempts: int = 3,
        opener: OpenerDirector | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if maximum_response_bytes <= 0:
            raise ValueError("maximum_response_bytes must be positive")
        if maximum_attempts < 1 or maximum_attempts > 4:
            raise ValueError("maximum_attempts must be between 1 and 4")
        self._timeout_seconds = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes
        self._maximum_attempts = maximum_attempts
        self._opener = opener or _default_opener()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper
        self._warmed_up = False

    def acquire(
        self,
        url: str,
        *,
        warm_up: bool = True,
        maximum_response_bytes: int | None = None,
    ) -> AcquiredNSEArtifact:
        """Return exact response bytes, never an inferred publication time."""

        validate_nse_url(url)
        requires_warm_up = urlparse(url).hostname == "www.nseindia.com"
        if warm_up and requires_warm_up and not self._warmed_up:
            self._download(NSE_HOMEPAGE_URL)
            self._warmed_up = True
        payload, final_url, media_type = self._download(
            url, maximum_response_bytes=maximum_response_bytes
        )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise NSEAcquisitionError("retrieval clock returned a naive timestamp")
        return AcquiredNSEArtifact(
            final_url, payload, retrieved_at.astimezone(UTC), media_type
        )

    def _download(
        self, url: str, *, maximum_response_bytes: int | None = None
    ) -> tuple[bytes, str, str | None]:
        validate_nse_url(url)
        byte_limit = maximum_response_bytes or self._maximum_response_bytes
        if byte_limit <= 0 or byte_limit > self._maximum_response_bytes:
            raise ValueError(
                "maximum_response_bytes must be positive and no greater than the client bound"
            )
        for attempt in range(1, self._maximum_attempts + 1):
            request = Request(
                url,
                headers={
                    "User-Agent": NSE_USER_AGENT,
                    "Accept": (
                        "application/json, application/xml, text/xml, text/csv, "
                        "application/zip, application/octet-stream, */*;q=0.2"
                    ),
                    "Accept-Encoding": "identity",
                    "Accept-Language": "en-IN,en;q=0.9",
                    "Referer": "https://www.nseindia.com/",
                    "Connection": "close",
                },
                method="GET",
            )
            response: _HTTPResponse | None = None
            try:
                response = cast(
                    _HTTPResponse,
                    self._opener.open(request, timeout=self._timeout_seconds),
                )
                final_url = response.geturl()
                validate_nse_url(final_url)
                content_length = self._content_length(response)
                if content_length is not None and content_length > byte_limit:
                    raise NSEArtifactTooLargeError("official NSE response exceeds byte limit")
                payload = response.read(byte_limit + 1)
                if len(payload) > byte_limit:
                    raise NSEArtifactTooLargeError("official NSE response exceeds byte limit")
                return payload, final_url, self._media_type(response)
            except HTTPError as error:
                if error.code == 404:
                    raise NSESourceNotAvailableError(
                        f"official NSE artifact is not available: {url}"
                    ) from error
                if error.code != 429 and not 500 <= error.code <= 599:
                    raise NSEAcquisitionError(
                        f"official NSE request failed with HTTP {error.code}"
                    ) from error
                if attempt == self._maximum_attempts:
                    raise NSEAcquisitionError(
                        f"official NSE request failed after {attempt} attempts"
                    ) from error
            except (TimeoutError, URLError, OSError) as error:
                if attempt == self._maximum_attempts:
                    raise NSEAcquisitionError(
                        f"official NSE request failed after {attempt} attempts"
                    ) from error
            finally:
                if response is not None:
                    response.close()
            self._sleeper(float(attempt))
        raise AssertionError("bounded retry loop did not terminate")

    @staticmethod
    def _content_length(response: _HTTPResponse) -> int | None:
        headers = response.headers
        getter = getattr(headers, "get", None)
        if getter is None:
            return None
        raw = getter("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _media_type(response: _HTTPResponse) -> str | None:
        getter = getattr(response.headers, "get", None)
        if getter is None:
            return None
        raw = getter("Content-Type")
        if not isinstance(raw, str) or not raw.strip():
            return None
        return raw.split(";", maxsplit=1)[0].strip().lower() or None
