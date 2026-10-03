"""Durable claiming and append-only attempt audit for Production O."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import (
    ResearchNotificationDelivery,
    ResearchNotificationDeliveryAttempt,
)


class ResearchNotificationDeliveryIntegrityError(RuntimeError):
    """Raised when deterministic delivery or lifecycle state conflicts."""


@dataclass(frozen=True, slots=True)
class ResearchNotificationDeliveryWrite:
    delivery_key_sha256: str
    outbox_id: UUID
    notification_key_sha256: str
    delivery_policy_code: str
    delivery_policy_checksum_sha256: str
    transport_code: str
    target_code: str
    rendering_version: str


@dataclass(frozen=True, slots=True)
class ClaimedResearchNotification:
    delivery_id: UUID
    attempt_id: UUID
    attempt_number: int


class ResearchNotificationDeliveryRepository:
    """Persist immutable identities and guarded delivery transitions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_key(self, delivery_key_sha256: str) -> ResearchNotificationDelivery | None:
        return self._session.scalar(
            select(ResearchNotificationDelivery)
            .options(
                selectinload(ResearchNotificationDelivery.outbox),
                selectinload(ResearchNotificationDelivery.attempts),
            )
            .where(ResearchNotificationDelivery.delivery_key_sha256 == delivery_key_sha256)
        )

    def get(self, delivery_id: UUID) -> ResearchNotificationDelivery | None:
        return self._session.scalar(
            select(ResearchNotificationDelivery)
            .options(
                selectinload(ResearchNotificationDelivery.outbox),
                selectinload(ResearchNotificationDelivery.attempts),
            )
            .where(ResearchNotificationDelivery.id == delivery_id)
        )

    def create_or_reuse(
        self, value: ResearchNotificationDeliveryWrite
    ) -> tuple[ResearchNotificationDelivery, bool]:
        existing = self.get_by_key(value.delivery_key_sha256)
        if existing is not None:
            if _delivery_projection(existing) != _write_projection(value):
                raise ResearchNotificationDeliveryIntegrityError(
                    "existing research notification delivery key conflicts"
                )
            return existing, False
        record = ResearchNotificationDelivery(
            **{field.name: getattr(value, field.name) for field in fields(value)},
            status="pending",
            attempt_count=0,
            next_attempt_at=None,
            last_error_detail_json={},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def claim_next(
        self,
        *,
        worker_token: str,
        now: datetime,
        lease_seconds: int,
        maximum_attempts: int,
    ) -> ClaimedResearchNotification | None:
        owner = _token(worker_token)
        self._dead_letter_expired_exhausted(now=now, maximum_attempts=maximum_attempts)
        eligible = or_(
            ResearchNotificationDelivery.status == "pending",
            and_(
                ResearchNotificationDelivery.status == "retry_wait",
                ResearchNotificationDelivery.next_attempt_at.is_not(None),
                ResearchNotificationDelivery.next_attempt_at <= now,
            ),
            and_(
                ResearchNotificationDelivery.status == "claimed",
                ResearchNotificationDelivery.claim_expires_at.is_not(None),
                ResearchNotificationDelivery.claim_expires_at <= now,
            ),
        )
        candidate_ids = tuple(
            self._session.scalars(
                select(ResearchNotificationDelivery.id)
                .where(
                    eligible,
                    ResearchNotificationDelivery.attempt_count < maximum_attempts,
                )
                .order_by(
                    func.coalesce(
                        ResearchNotificationDelivery.next_attempt_at,
                        ResearchNotificationDelivery.created_at,
                    ).asc(),
                    ResearchNotificationDelivery.created_at.asc(),
                    ResearchNotificationDelivery.delivery_key_sha256.asc(),
                )
                .limit(20)
            )
        )
        for delivery_id in candidate_ids:
            claimed = self._session.execute(
                update(ResearchNotificationDelivery)
                .where(
                    ResearchNotificationDelivery.id == delivery_id,
                    eligible,
                    ResearchNotificationDelivery.attempt_count < maximum_attempts,
                )
                .values(
                    status="claimed",
                    claim_owner_token=owner,
                    claimed_at=now,
                    claim_expires_at=now + timedelta(seconds=lease_seconds),
                    next_attempt_at=None,
                    attempt_count=ResearchNotificationDelivery.attempt_count + 1,
                    updated_at=now,
                )
            )
            if not isinstance(claimed, CursorResult) or claimed.rowcount != 1:
                continue
            delivery = self.get(delivery_id)
            if delivery is None:
                raise ResearchNotificationDeliveryIntegrityError(
                    "claimed delivery disappeared"
                )
            attempt = ResearchNotificationDeliveryAttempt(
                delivery_id=delivery.id,
                attempt_number=delivery.attempt_count,
                worker_token=owner,
                started_at=now,
                outcome=None,
                detail_json={},
            )
            self._session.add(attempt)
            self._session.flush()
            self._assert_attempt_count(delivery)
            return ClaimedResearchNotification(
                delivery_id=delivery.id,
                attempt_id=attempt.id,
                attempt_number=attempt.attempt_number,
            )
        return None

    def record_delivered(
        self,
        *,
        claim: ClaimedResearchNotification,
        worker_token: str,
        completed_at: datetime,
        provider_message_id: str,
        http_status: int,
    ) -> None:
        delivery, attempt = self._active_claim(claim, worker_token)
        attempt.completed_at = completed_at
        attempt.outcome = "delivered"
        attempt.http_status = http_status
        attempt.provider_message_id = provider_message_id
        attempt.detail_json = {"provider_response": "verified_ok"}
        delivery.status = "delivered"
        delivery.next_attempt_at = None
        delivery.claim_owner_token = None
        delivery.claimed_at = None
        delivery.claim_expires_at = None
        delivery.last_error_code = None
        delivery.last_error_detail_json = {}
        delivery.provider_message_id = provider_message_id
        delivery.delivered_at = completed_at
        delivery.updated_at = completed_at
        self._session.flush()
        self._assert_attempt_count(delivery)

    def record_failure(
        self,
        *,
        claim: ClaimedResearchNotification,
        worker_token: str,
        completed_at: datetime,
        retryable: bool,
        retry_delay_seconds: int | None,
        maximum_attempts: int,
        error_code: str,
        detail: dict[str, object],
        http_status: int | None,
        telegram_error_code: int | None,
    ) -> None:
        delivery, attempt = self._active_claim(claim, worker_token)
        terminal = not retryable or delivery.attempt_count >= maximum_attempts
        attempt.completed_at = completed_at
        attempt.outcome = "permanent_failure" if terminal else "retryable_failure"
        attempt.http_status = http_status
        attempt.telegram_error_code = telegram_error_code
        attempt.error_code = error_code
        attempt.detail_json = dict(detail)
        delivery.status = "dead_letter" if terminal else "retry_wait"
        delivery.next_attempt_at = (
            None
            if terminal
            else completed_at + timedelta(seconds=_positive_delay(retry_delay_seconds))
        )
        delivery.claim_owner_token = None
        delivery.claimed_at = None
        delivery.claim_expires_at = None
        delivery.last_error_code = error_code
        delivery.last_error_detail_json = dict(detail)
        delivery.updated_at = completed_at
        self._session.flush()
        self._assert_attempt_count(delivery)

    def due_count(self, *, now: datetime) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(ResearchNotificationDelivery)
                .where(
                    or_(
                        ResearchNotificationDelivery.status == "pending",
                        and_(
                            ResearchNotificationDelivery.status == "retry_wait",
                            ResearchNotificationDelivery.next_attempt_at <= now,
                        ),
                        and_(
                            ResearchNotificationDelivery.status == "claimed",
                            ResearchNotificationDelivery.claim_expires_at <= now,
                        ),
                    )
                )
            )
            or 0
        )

    def backlog_count(self) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(ResearchNotificationDelivery)
                .where(
                    ResearchNotificationDelivery.status.in_(
                        ("pending", "claimed", "retry_wait")
                    )
                )
            )
            or 0
        )

    def _active_claim(
        self, claim: ClaimedResearchNotification, worker_token: str
    ) -> tuple[ResearchNotificationDelivery, ResearchNotificationDeliveryAttempt]:
        delivery = self.get(claim.delivery_id)
        if delivery is None:
            raise ResearchNotificationDeliveryIntegrityError("delivery is unavailable")
        owner = _token(worker_token)
        if delivery.status != "claimed" or delivery.claim_owner_token != owner:
            raise ResearchNotificationDeliveryIntegrityError(
                "delivery is not actively claimed by this worker"
            )
        attempt = self._session.get(ResearchNotificationDeliveryAttempt, claim.attempt_id)
        if (
            attempt is None
            or attempt.delivery_id != delivery.id
            or attempt.attempt_number != claim.attempt_number
            or attempt.completed_at is not None
            or attempt.outcome is not None
        ):
            raise ResearchNotificationDeliveryIntegrityError(
                "delivery attempt state conflicts"
            )
        return delivery, attempt

    def _dead_letter_expired_exhausted(
        self, *, now: datetime, maximum_attempts: int
    ) -> None:
        self._session.execute(
            update(ResearchNotificationDelivery)
            .where(
                ResearchNotificationDelivery.status == "claimed",
                ResearchNotificationDelivery.claim_expires_at <= now,
                ResearchNotificationDelivery.attempt_count >= maximum_attempts,
            )
            .values(
                status="dead_letter",
                claim_owner_token=None,
                claimed_at=None,
                claim_expires_at=None,
                next_attempt_at=None,
                last_error_code="claim_expired_maximum_attempts",
                last_error_detail_json={"reason": "claim_expired_after_final_attempt"},
                updated_at=now,
            )
        )

    def _assert_attempt_count(self, delivery: ResearchNotificationDelivery) -> None:
        actual = int(
            self._session.scalar(
                select(func.count())
                .select_from(ResearchNotificationDeliveryAttempt)
                .where(ResearchNotificationDeliveryAttempt.delivery_id == delivery.id)
            )
            or 0
        )
        if actual != delivery.attempt_count:
            raise ResearchNotificationDeliveryIntegrityError(
                "delivery attempt count conflicts with attempt audit"
            )


def _delivery_projection(value: ResearchNotificationDelivery) -> tuple[object, ...]:
    return tuple(
        getattr(value, field.name) for field in fields(ResearchNotificationDeliveryWrite)
    )


def _write_projection(value: ResearchNotificationDeliveryWrite) -> tuple[object, ...]:
    return tuple(getattr(value, field.name) for field in fields(value))


def _token(value: str) -> str:
    item = value.strip()
    if not item or len(item) > 160:
        raise ValueError("worker token must be a non-empty bounded string")
    return item


def _positive_delay(value: int | None) -> int:
    if value is None or value <= 0:
        raise ResearchNotificationDeliveryIntegrityError(
            "retryable failure requires a positive delay"
        )
    return value
