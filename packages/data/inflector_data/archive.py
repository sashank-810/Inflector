"""Immutable content-addressed raw object storage."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from re import fullmatch
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ArchivedRawObject:
    """Immutable object metadata persisted alongside source observations."""

    content_sha256: str
    object_key: str
    size_bytes: int


class RawObjectStore(Protocol):
    """Port for immutable raw-object storage; S3 can implement this later."""

    def put(self, content: bytes) -> ArchivedRawObject:
        """Store content once and return its stable content-addressed key."""

        ...

    def get(self, object_key: str) -> bytes:
        """Return exact immutable bytes for a valid content-addressed key."""

        ...


class ObjectStoreError(ValueError):
    """Base error for immutable object-store access."""


class InvalidObjectKeyError(ObjectStoreError):
    """Raised when an object key is not an approved content-addressed key."""


class ObjectNotFoundError(ObjectStoreError):
    """Raised when a valid immutable object key is absent."""


class ObjectStoreIntegrityError(ObjectStoreError):
    """Raised when stored bytes do not match their content-addressed identity."""


class LocalRawObjectStore:
    """Filesystem implementation for development and deterministic tests."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, content: bytes) -> ArchivedRawObject:
        """Write bytes once under a SHA-256-derived relative key."""

        digest = sha256(content).hexdigest()
        object_key = f"sha256/{digest[:2]}/{digest}"
        target = self._root / object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_bytes()
            if sha256(existing).hexdigest() != digest:
                raise ObjectStoreIntegrityError("existing object content does not match its key")
        else:
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        return ArchivedRawObject(digest, object_key, len(content))

    def get(self, object_key: str) -> bytes:
        """Read exact bytes only from a validated SHA-256 content key."""

        match = fullmatch(r"sha256/([0-9a-f]{2})/([0-9a-f]{64})", object_key)
        if match is None or match.group(1) != match.group(2)[:2]:
            raise InvalidObjectKeyError("invalid content-addressed object key")
        root = self._root.resolve()
        target = (root / object_key).resolve()
        if not target.is_relative_to(root):
            raise InvalidObjectKeyError("object key escapes configured root")
        if not target.is_file():
            raise ObjectNotFoundError("content-addressed object does not exist")
        content = target.read_bytes()
        if sha256(content).hexdigest() != match.group(2):
            raise ObjectStoreIntegrityError("stored object SHA-256 does not match its key")
        return content
