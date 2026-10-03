"""Thin Operations V6 adapter for Production O delivery."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from inflector_data.notification_delivery_policy import (
    NotificationDeliveryPolicy,
    load_notification_delivery_policy,
)
from inflector_data.operations_profile import ProductionOperationsProfile
from inflector_data.research_notification_delivery import (
    drain_notification_deliveries,
    prepare_notification_deliveries,
)
from inflector_data.telegram_notification_transport import TelegramBotApiTransport
from inflector_database.research_notification_delivery_repository import (
    ResearchNotificationDeliveryRepository,
)


def load_bound_notification_delivery_policy(
    profile: ProductionOperationsProfile, *, repository_root: Path
) -> NotificationDeliveryPolicy:
    """Load the actual O policy and fail closed against Operations V6."""

    if not profile.notification_delivery_enabled:
        raise ValueError("notification delivery is disabled by operations profile")
    if profile.notification_delivery_policy_asset is None:
        raise ValueError("Operations V6 notification delivery policy asset is unavailable")
    root = repository_root.resolve()
    candidate = (root / profile.notification_delivery_policy_asset).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Operations V6 delivery policy asset escapes repository") from error
    policy = load_notification_delivery_policy(candidate, repository_root=root)
    if (
        policy.code != profile.notification_delivery_policy_code
        or policy.checksum_sha256
        != profile.notification_delivery_policy_checksum_sha256
    ):
        raise ValueError("Operations V6 notification delivery policy binding mismatch")
    if (
        policy.research_alert_policy_code != profile.research_alert_policy_code
        or policy.research_alert_policy_checksum_sha256
        != profile.research_alert_policy_checksum_sha256
    ):
        raise ValueError("Operations V6 delivery policy does not bind its N policy")
    return policy


class NotificationDeliveryOrchestrator:
    """Prepare and drain O backlog without duplicating delivery semantics."""

    def __init__(
        self,
        repository_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        transport: TelegramBotApiTransport | None = None,
    ) -> None:
        self._repository_root = repository_root.resolve()
        self._environment = environment if environment is not None else os.environ
        self._clock = clock or (lambda: datetime.now(UTC))
        self._transport = transport or TelegramBotApiTransport()

    def execute(
        self, session: Session, plan: object
    ) -> tuple[dict[str, object], bool]:
        profile = getattr(plan, "operations_profile", None)
        run_key = getattr(plan, "run_key_sha256", None)
        if not isinstance(profile, ProductionOperationsProfile) or not isinstance(
            run_key, str
        ):
            raise ValueError("notification delivery operational plan is malformed")
        policy = load_bound_notification_delivery_policy(
            profile, repository_root=self._repository_root
        )
        token = self._environment.get("INFLECTOR_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = self._environment.get("INFLECTOR_TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            preparation = prepare_notification_deliveries(session, policy=policy)
            repository = ResearchNotificationDeliveryRepository(session)
            return {
                "status": "transport_unavailable",
                "delivery_policy_code": policy.code,
                "delivery_policy_checksum_sha256": policy.checksum_sha256,
                "transport_code": policy.transport_code,
                "target_code": policy.target_code,
                "credentials_available": False,
                "delivery_rows_created": preparation.delivery_rows_created,
                "delivery_rows_reused": preparation.delivery_rows_reused,
                "attempts_started": 0,
                "delivered": 0,
                "retry_wait": 0,
                "dead_letter": 0,
                "backlog_count": repository.backlog_count(),
            }, True
        result = drain_notification_deliveries(
            session,
            policy=policy,
            transport=self._transport,
            bot_token=token,
            chat_id=chat_id,
            worker_token=f"operations:{run_key}",
            clock=self._clock,
        )
        return {
            "status": "completed",
            "delivery_policy_code": policy.code,
            "delivery_policy_checksum_sha256": policy.checksum_sha256,
            "transport_code": policy.transport_code,
            "target_code": policy.target_code,
            "credentials_available": True,
            "delivery_rows_created": result.preparation.delivery_rows_created,
            "delivery_rows_reused": result.preparation.delivery_rows_reused,
            "attempts_started": result.attempts_started,
            "delivered": result.delivered,
            "retry_wait": result.retry_wait,
            "dead_letter": result.dead_letter,
            "backlog_count": result.backlog_count,
            "due_count": result.due_count,
        }, True
