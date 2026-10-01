"""Bounded GDELT DOC 2.0 news-count acquisition for production attention evidence."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.cookiejar import CookieJar
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

from inflector_core.providers import (
    AttentionObservationRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
)

GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_ALLOWED_HOSTS = frozenset({"api.gdeltproject.org"})
GDELT_PROVIDER_CODE = "gdelt"
GDELT_NEWS_DATASET_CODE = "gdelt_doc_company_news_mentions"
GDELT_NEWS_METHOD_VERSION = "gdelt_exact_company_name_news_v1"
GDELT_NEWS_SCOPE_CODE = "exact_quoted_canonical_company_name_doc20_timelinevolraw"
GDELT_USER_AGENT = "Inflector-Personal-Research/1.0 (aggregate-news-counts)"
GDELT_MAXIMUM_RESPONSE_BYTES = 2_000_000

GDELT_NEWS_METHOD_DEFINITION = (
    "GDELT DOC 2.0 TimelineVolRaw article-count sum for one exact quoted canonical "
    "company name over an explicit UTC half-open window; no ticker, brand, subsidiary, "
    "executive, language, country, domain, tone, or article-body expansion."
)
GDELT_NEWS_METHOD_DEFINITION_SHA256 = sha256(
    GDELT_NEWS_METHOD_DEFINITION.encode("utf-8")
).hexdigest()


class GDELTAttentionError(RuntimeError):
    """A GDELT request or response cannot establish complete count evidence."""


class GDELTResponseTooLargeError(GDELTAttentionError):
    """The aggregate response exceeded the configured byte bound."""


class GDELTHostNotAllowedError(GDELTAttentionError):
    """The request or redirect escaped the exact GDELT API host."""


@dataclass(frozen=True, slots=True)
class AcquiredGDELTResponse:
    source_uri: str
    payload: bytes
    retrieved_at: datetime


class _HTTPResponse(Protocol):
    headers: object

    def read(self, amount: int = -1) -> bytes: ...

    def geturl(self) -> str: ...

    def close(self) -> None: ...


class GDELTResponseSource(Protocol):
    def acquire(self, url: str) -> AcquiredGDELTResponse: ...


def validate_gdelt_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise GDELTHostNotAllowedError("GDELT acquisition requires HTTPS")
    if parsed.hostname is None or parsed.hostname.lower() not in GDELT_ALLOWED_HOSTS:
        raise GDELTHostNotAllowedError("GDELT URL host is not allowlisted")
    if parsed.username is not None or parsed.password is not None:
        raise GDELTHostNotAllowedError("credentials are not permitted in GDELT URLs")


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
        validate_gdelt_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener() -> OpenerDirector:
    return build_opener(HTTPCookieProcessor(CookieJar()), _AllowlistedRedirectHandler())


class GDELTHttpClient:
    """Small exact-host HTTPS client with bounded retries and response bytes."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        maximum_response_bytes: int = GDELT_MAXIMUM_RESPONSE_BYTES,
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

    def acquire(self, url: str) -> AcquiredGDELTResponse:
        validate_gdelt_url(url)
        for attempt in range(1, self._maximum_attempts + 1):
            request = Request(
                url,
                headers={
                    "User-Agent": GDELT_USER_AGENT,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
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
                validate_gdelt_url(final_url)
                payload = response.read(self._maximum_response_bytes + 1)
                if len(payload) > self._maximum_response_bytes:
                    raise GDELTResponseTooLargeError("GDELT response exceeds byte limit")
                retrieved_at = self._clock()
                if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                    raise GDELTAttentionError("retrieval clock returned a naive timestamp")
                return AcquiredGDELTResponse(
                    source_uri=final_url,
                    payload=payload,
                    retrieved_at=retrieved_at.astimezone(UTC),
                )
            except HTTPError as error:
                if error.code != 429 and not 500 <= error.code <= 599:
                    raise GDELTAttentionError(
                        f"GDELT request failed with HTTP {error.code}"
                    ) from error
                if attempt == self._maximum_attempts:
                    raise GDELTAttentionError(
                        f"GDELT request failed after {attempt} attempts"
                    ) from error
            except (TimeoutError, URLError, OSError) as error:
                if attempt == self._maximum_attempts:
                    raise GDELTAttentionError(
                        f"GDELT request failed after {attempt} attempts"
                    ) from error
            finally:
                if response is not None:
                    response.close()
            self._sleeper(float(attempt))
        raise AssertionError("bounded retry loop did not terminate")


def gdelt_news_url(
    company_name: str,
    window_start_at: datetime,
    window_end_at: datetime,
) -> str:
    name = company_name.strip()
    if not name or name != company_name:
        raise ValueError("company_name must be non-empty and trimmed")
    start = _aware_utc(window_start_at, "window_start_at")
    end = _aware_utc(window_end_at, "window_end_at")
    if start >= end:
        raise ValueError("news window start must be before end")
    query = urlencode(
        {
            "query": f'"{name}"',
            "mode": "TimelineVolRaw",
            "format": "json",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        }
    )
    return f"{GDELT_DOC_API_URL}?{query}"


class GDELTNewsAttentionProvider:
    """Emit one company-level complete raw article-count observation."""

    def __init__(
        self,
        *,
        client: GDELTResponseSource,
        metadata: ProviderMetadata,
        company_legal_name: str,
        window_start_at: datetime,
        window_end_at: datetime,
    ) -> None:
        if metadata.provider_code != GDELT_PROVIDER_CODE:
            raise ValueError("GDELT provider code mismatch")
        if metadata.dataset_code != GDELT_NEWS_DATASET_CODE:
            raise ValueError("GDELT dataset code mismatch")
        self._client = client
        self._metadata = metadata
        self._company_name = company_legal_name
        self._window_start = _aware_utc(window_start_at, "window_start_at")
        self._window_end = _aware_utc(window_end_at, "window_end_at")
        self._source_uri: str | None = None
        self._retrieved_at: datetime | None = None

    @property
    def source_uri(self) -> str | None:
        return self._source_uri

    @property
    def retrieved_at(self) -> datetime | None:
        return self._retrieved_at

    def fetch_attention_data(self) -> ProviderBatch[AttentionObservationRecord]:
        requested_url = gdelt_news_url(self._company_name, self._window_start, self._window_end)
        response = self._client.acquire(requested_url)
        self._source_uri = response.source_uri
        self._retrieved_at = response.retrieved_at
        if self._window_end > response.retrieved_at:
            raise GDELTAttentionError("news window ends after observed retrieval time")
        count = _timeline_raw_count(
            response.payload,
            self._company_name,
            self._window_start,
            self._window_end,
        )
        digest = sha256(response.payload).hexdigest()
        external_id = (
            f"gdelt-news:{sha256(self._company_name.encode('utf-8')).hexdigest()[:16]}:"
            f"{self._window_start.strftime('%Y%m%d%H%M%S')}:"
            f"{self._window_end.strftime('%Y%m%d%H%M%S')}"
        )
        record = AttentionObservationRecord(
            company_legal_name=self._company_name,
            security_isin=None,
            metric_code="news_mentions_count",
            reported_count=count,
            reported_unit="count",
            scope_code=GDELT_NEWS_SCOPE_CODE,
            methodology_version=GDELT_NEWS_METHOD_VERSION,
            measurement_definition_sha256=GDELT_NEWS_METHOD_DEFINITION_SHA256,
            coverage_status="complete",
            observation_date=None,
            window_start_at=self._window_start,
            window_end_at=self._window_end,
        )
        envelope = IngestionEnvelope(
            provider=self._metadata,
            external_record_id=external_id,
            source_uri=response.source_uri,
            raw_payload_reference="json:timeline:series:Article Count",
            content_sha256=digest,
            retrieved_at=response.retrieved_at,
            available_at=response.retrieved_at,
            record=record,
        )
        return ProviderBatch(
            provider=self._metadata,
            source_uri=response.source_uri,
            raw_payload=response.payload,
            retrieved_at=response.retrieved_at,
            records=(envelope,),
        )


def _timeline_raw_count(
    payload: bytes,
    company_name: str,
    window_start_at: datetime,
    window_end_at: datetime,
) -> int:
    try:
        decoded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GDELTAttentionError("GDELT response is not valid UTF-8 JSON") from error
    if not isinstance(decoded, dict):
        raise GDELTAttentionError("GDELT response root must be an object")
    query_details = decoded.get("query_details")
    if not isinstance(query_details, dict):
        raise GDELTAttentionError("GDELT response lacks query_details")
    title = query_details.get("title")
    if title not in {company_name, f'"{company_name}"'}:
        raise GDELTAttentionError("GDELT response query identity does not match request")
    resolution = query_details.get("date_resolution")
    if not isinstance(resolution, str):
        raise GDELTAttentionError("GDELT response lacks a date resolution")
    resolution_step = {
        "15m": timedelta(minutes=15),
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
    }.get(resolution)
    if resolution_step is None:
        raise GDELTAttentionError("GDELT response has an unsupported date resolution")
    timeline = decoded.get("timeline")
    if not isinstance(timeline, list):
        raise GDELTAttentionError("GDELT response lacks timeline")
    series = [
        item
        for item in timeline
        if isinstance(item, dict) and item.get("series") == "Article Count"
    ]
    if len(series) != 1 or not isinstance(series[0].get("data"), list):
        raise GDELTAttentionError("GDELT response lacks one raw article-count series")
    data = series[0]["data"]
    if not data:
        raise GDELTAttentionError("GDELT response does not structurally cover the requested window")
    start = _aware_utc(window_start_at, "window_start_at")
    end = _aware_utc(window_end_at, "window_end_at")
    total = 0
    timestamps: list[datetime] = []
    for item in data:
        if not isinstance(item, dict):
            raise GDELTAttentionError("GDELT timeline point must be an object")
        value = item.get("value")
        norm = item.get("norm")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GDELTAttentionError("GDELT raw article count must be a non-negative integer")
        if isinstance(norm, bool) or not isinstance(norm, int) or norm < 0:
            raise GDELTAttentionError("GDELT timeline norm must be a non-negative integer")
        timestamp_text = item.get("date")
        if not isinstance(timestamp_text, str):
            raise GDELTAttentionError("GDELT timeline point lacks a date")
        try:
            timestamp = datetime.strptime(timestamp_text, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError as error:
            raise GDELTAttentionError("GDELT timeline point has an invalid date") from error
        if not start <= timestamp < end:
            raise GDELTAttentionError("GDELT timeline point falls outside the requested window")
        timestamps.append(timestamp)
        total += value
    if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
        raise GDELTAttentionError("GDELT timeline dates must be unique and ordered")
    if timestamps[0] - start > resolution_step or end - timestamps[-1] > resolution_step:
        raise GDELTAttentionError("GDELT timeline does not span the requested window")
    if any(
        current - previous > resolution_step
        for previous, current in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise GDELTAttentionError("GDELT timeline has a gap in the requested window")
    return total


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
