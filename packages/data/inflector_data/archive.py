"""Immutable content-addressed raw object storage."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
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
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
        return ArchivedRawObject(digest, object_key, len(content))
