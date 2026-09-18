"""Canonical JSON audit serialization and SHA-256 fingerprinting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID


def canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def canonical_audit_value(value: object) -> object:
    """Convert supported audit values to deterministic JSON-compatible values."""

    if isinstance(value, float):
        raise TypeError("binary floats are not permitted in audit payloads")
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("audit datetimes must be timezone-aware")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): canonical_audit_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonical_audit_value(item) for item in value]
    if value is None or isinstance(value, (bool, str, int)):
        return value
    raise TypeError(f"unsupported audit value type: {type(value).__name__}")


def canonical_audit_json(value: object) -> str:
    return json.dumps(
        canonical_audit_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def audit_fingerprint_sha256(value: object) -> str:
    return hashlib.sha256(canonical_audit_json(value).encode("utf-8")).hexdigest()
