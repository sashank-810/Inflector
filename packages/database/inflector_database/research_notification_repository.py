"""Append-only persistence for transport-neutral research notifications."""

from __future__ import annotations

from dataclasses import dataclass, fields
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_database.models import ResearchNotificationOutbox


class ResearchNotificationIntegrityError(RuntimeError):
    """Raised when a deterministic notification key conflicts."""


@dataclass(frozen=True, slots=True)
class ResearchNotificationWrite:
    notification_key_sha256: str
    alert_policy_code: str
    alert_policy_checksum_sha256: str
    payload_schema_version: str
    source_change_run_id: UUID
    source_change_item_id: UUID
    company_id: UUID | None
    security_id: UUID | None
    baseline_symbol: str | None
    current_symbol: str | None
    matched_trigger_codes_json: list[object]
    payload_json: dict[str, object]
    delivery_status: str = "pending"


class ResearchNotificationRepository:
    """Create or reuse immutable pending notification rows without delivery."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_key(self, notification_key_sha256: str) -> ResearchNotificationOutbox | None:
        return self._session.scalar(
            select(ResearchNotificationOutbox).where(
                ResearchNotificationOutbox.notification_key_sha256
                == notification_key_sha256
            )
        )

    def create_or_reuse(
        self, value: ResearchNotificationWrite
    ) -> tuple[ResearchNotificationOutbox, bool]:
        if value.delivery_status != "pending":
            raise ResearchNotificationIntegrityError(
                "Production N may create pending notifications only"
            )
        existing = self.get_by_key(value.notification_key_sha256)
        if existing is not None:
            if _record_projection(existing) != _write_projection(value):
                raise ResearchNotificationIntegrityError(
                    "existing research notification key conflicts"
                )
            return existing, False
        record = ResearchNotificationOutbox(
            **{field.name: getattr(value, field.name) for field in fields(value)}
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def list_pending(self, *, limit: int = 100) -> tuple[ResearchNotificationOutbox, ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("pending notification limit must be between 1 and 5000")
        return tuple(
            self._session.scalars(
                select(ResearchNotificationOutbox)
                .where(ResearchNotificationOutbox.delivery_status == "pending")
                .order_by(
                    ResearchNotificationOutbox.current_symbol.asc(),
                    ResearchNotificationOutbox.baseline_symbol.asc(),
                    ResearchNotificationOutbox.security_id.asc(),
                    ResearchNotificationOutbox.notification_key_sha256.asc(),
                )
                .limit(limit)
            )
        )


def _record_projection(value: ResearchNotificationOutbox) -> tuple[object, ...]:
    return tuple(getattr(value, field.name) for field in fields(ResearchNotificationWrite))


def _write_projection(value: ResearchNotificationWrite) -> tuple[object, ...]:
    return tuple(getattr(value, field.name) for field in fields(value))
