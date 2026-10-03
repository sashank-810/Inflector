"""Secret-safe Telegram Bot API boundary for Production O."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener


class _HTTPResponse(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _TelegramOpener(Protocol):
    def open(self, request: Request, *, timeout: int) -> _HTTPResponse: ...


@dataclass(frozen=True, slots=True)
class TelegramTransportResult:
    http_status: int | None
    ok: bool
    provider_message_id: str | None
    telegram_error_code: int | None
    retry_after_seconds: int | None
    error_code: str | None
    malformed_response: bool


class TelegramBotApiTransport:
    """Send one plain-text message with a finite timeout and sanitized results."""

    def __init__(self, opener: _TelegramOpener | None = None) -> None:
        self._opener = opener or cast(_TelegramOpener, build_opener())

    def send_message(
        self,
        *,
        bot_token: str,
        chat_id: str,
        text: str,
        timeout_seconds: int,
    ) -> TelegramTransportResult:
        token = _secret(bot_token, "Telegram bot token")
        target = _secret(chat_id, "Telegram chat ID")
        if timeout_seconds <= 0:
            raise ValueError("Telegram timeout must be positive")
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": target, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        response: _HTTPResponse | None = None
        try:
            response = cast(
                _HTTPResponse,
                self._opener.open(request, timeout=timeout_seconds),
            )
            return _parse_response(response.status, response.read(1_000_001))
        except HTTPError as error:
            return _parse_response(error.code, error.read(1_000_001))
        except TimeoutError:
            return _network_failure("telegram_timeout")
        except URLError as error:
            reason = error.reason
            code = "telegram_timeout" if isinstance(reason, TimeoutError) else "telegram_connection"
            return _network_failure(code)
        except OSError:
            return _network_failure("telegram_connection")
        finally:
            if response is not None:
                response.close()


def _parse_response(http_status: int, raw: bytes) -> TelegramTransportResult:
    if len(raw) > 1_000_000:
        return _malformed(http_status)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _malformed(http_status)
    if not isinstance(value, dict):
        return _malformed(http_status)
    ok = value.get("ok")
    error_code = _integer_or_none(value.get("error_code"))
    retry_after = None
    parameters = value.get("parameters")
    if isinstance(parameters, dict):
        retry_after = _positive_integer_or_none(parameters.get("retry_after"))
    if http_status == 200 and ok is True:
        result = value.get("result")
        message_id = (
            _integer_or_none(result.get("message_id"))
            if isinstance(result, dict)
            else None
        )
        if message_id is not None:
            return TelegramTransportResult(
                http_status=http_status,
                ok=True,
                provider_message_id=str(message_id),
                telegram_error_code=None,
                retry_after_seconds=None,
                error_code=None,
                malformed_response=False,
            )
    if not isinstance(ok, bool):
        return _malformed(http_status)
    return TelegramTransportResult(
        http_status=http_status,
        ok=False,
        provider_message_id=None,
        telegram_error_code=error_code,
        retry_after_seconds=retry_after,
        error_code="telegram_rejected_response",
        malformed_response=False,
    )


def _malformed(http_status: int) -> TelegramTransportResult:
    return TelegramTransportResult(
        http_status=http_status,
        ok=False,
        provider_message_id=None,
        telegram_error_code=None,
        retry_after_seconds=None,
        error_code="telegram_malformed_response",
        malformed_response=True,
    )


def _network_failure(error_code: str) -> TelegramTransportResult:
    return TelegramTransportResult(
        http_status=None,
        ok=False,
        provider_message_id=None,
        telegram_error_code=None,
        retry_after_seconds=None,
        error_code=error_code,
        malformed_response=False,
    )


def _secret(value: str, label: str) -> str:
    item = value.strip()
    if not item:
        raise ValueError(f"{label} is unavailable")
    return item


def _integer_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_integer_or_none(value: object) -> int | None:
    result = _integer_or_none(value)
    return result if result is not None and result > 0 else None
