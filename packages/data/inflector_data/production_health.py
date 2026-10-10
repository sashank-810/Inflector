"""Read-only structural and operational health diagnostics for Production P."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute

from inflector_data.notification_delivery_policy import (
    load_notification_delivery_policy,
)
from inflector_data.operations_profile import load_operations_profile
from inflector_data.opportunity_change_policy import load_opportunity_change_policy
from inflector_data.opportunity_policy import load_opportunity_discovery_policy
from inflector_data.research_alert_policy import load_research_alert_policy
from inflector_data.research_profile import load_research_profile
from inflector_database.models import (
    ModelVersion,
    OperationalRun,
    ResearchNotificationDelivery,
    ResearchNotificationOutbox,
)

LATEST_MIGRATION = "20261005_0024"
ACCEPTED_CHECKSUMS = {
    "research_v4": "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1",
    "opportunity_discovery_v1": (
        "f90e463d3c8940cf9f93210e4d0806bdf4d535340a7d4140ba7d3e517d2e99d2"
    ),
    "opportunity_change_v1": (
        "2189bdcf6243545524fa8cca8721cd4c5301b775a8c9f89f83c481c55bd646a9"
    ),
    "research_alert_v1": (
        "e3d1a65eaa80017760d646ed8b03cb7fcf1d0107903c935c70be627a82e33619"
    ),
    "notification_delivery_v1": (
        "307756eac5ba4e63d7631e22e59057f9f301dd5ed431180e89dab54ed610360e"
    ),
    "operations_v6": (
        "f71631f7f5baddd42796aae67c42c2732b1435a4d0a13c22ebe06031e70ae41f"
    ),
}


def accepted_asset_readiness(repository_root: Path) -> dict[str, object]:
    """Load the accepted chain and fail closed on any checksum/binding mismatch."""

    root = repository_root.resolve()
    research = load_research_profile(root / "config/research/production_research_v4.json")
    discovery = load_opportunity_discovery_policy(
        root / "config/opportunity/production_opportunity_discovery_v1.json",
        repository_root=root,
    )
    change = load_opportunity_change_policy(
        root / "config/opportunity/production_opportunity_change_v1.json",
        repository_root=root,
    )
    alert = load_research_alert_policy(
        root / "config/notifications/production_research_alert_policy_v1.json",
        repository_root=root,
    )
    delivery = load_notification_delivery_policy(
        root / "config/notifications/production_notification_delivery_v1.json",
        repository_root=root,
    )
    operations = load_operations_profile(
        root / "config/operations/production_operations_v6.json"
    )
    actual = {
        "research_v4": research.checksum_sha256,
        "opportunity_discovery_v1": discovery.checksum_sha256,
        "opportunity_change_v1": change.checksum_sha256,
        "research_alert_v1": alert.checksum_sha256,
        "notification_delivery_v1": delivery.checksum_sha256,
        "operations_v6": operations.checksum_sha256,
    }
    mismatches = [
        name for name, checksum in actual.items() if checksum != ACCEPTED_CHECKSUMS[name]
    ]
    if mismatches:
        raise ValueError(f"accepted asset checksum mismatch: {', '.join(mismatches)}")
    bindings = {
        "research_to_k": discovery.research_profile_checksum_sha256
        == research.checksum_sha256,
        "k_to_l": change.discovery_policy_checksum_sha256 == discovery.checksum_sha256,
        "l_to_n": alert.opportunity_change_policy_checksum_sha256 == change.checksum_sha256,
        "n_to_o": delivery.research_alert_policy_checksum_sha256 == alert.checksum_sha256,
        "v6_to_k": operations.opportunity_discovery_policy_checksum_sha256
        == discovery.checksum_sha256,
        "v6_to_l": operations.opportunity_change_policy_checksum_sha256
        == change.checksum_sha256,
        "v6_to_n": operations.research_alert_policy_checksum_sha256
        == alert.checksum_sha256,
        "v6_to_o": operations.notification_delivery_policy_checksum_sha256
        == delivery.checksum_sha256,
    }
    invalid = [name for name, valid in bindings.items() if not valid]
    if invalid:
        raise ValueError(f"accepted asset binding mismatch: {', '.join(invalid)}")
    codes = {
        "research_v4": research.research_profile_code,
        "opportunity_discovery_v1": discovery.code,
        "opportunity_change_v1": change.code,
        "research_alert_v1": alert.code,
        "notification_delivery_v1": delivery.code,
        "operations_v6": operations.operations_profile_code,
    }
    return {
        name: {"status": "ready", "code": codes[name], "checksum_sha256": checksum}
        for name, checksum in actual.items()
    }


def production_health(
    *,
    repository_root: Path,
    observed_at: datetime,
    session: Session | None,
    raw_root: Path | None,
    gdelt_raw_root: Path | None,
    model_family: str | None,
    telegram_bot_token_present: bool,
    telegram_chat_id_present: bool,
) -> dict[str, object]:
    """Build one deterministic read-only production readiness report."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("health observed_at must be timezone-aware")
    issues: list[str] = []
    try:
        assets: dict[str, object] = accepted_asset_readiness(repository_root)
    except (OSError, ValueError) as error:
        assets = {"status": "broken", "error_code": type(error).__name__}
        issues.append("accepted_assets_broken")

    roots = {
        "raw_root": _root_status(raw_root),
        "gdelt_raw_root": _root_status(gdelt_raw_root),
    }
    for name, state in roots.items():
        if state["status"] != "ready":
            issues.append(f"{name}_{state['status']}")

    credentials = {
        "telegram_bot_token": "available" if telegram_bot_token_present else "unavailable",
        "telegram_chat_id": "available" if telegram_chat_id_present else "unavailable",
    }
    if not telegram_bot_token_present or not telegram_chat_id_present:
        issues.append("telegram_credentials_unavailable")

    if session is None:
        database: dict[str, object] = {"status": "configuration_unavailable"}
        model: dict[str, object] = {
            "status": "configuration_unavailable",
            "model_family": model_family,
        }
        issues.append("database_configuration_unavailable")
    else:
        database = _database_health(session, observed_at)
        model = _model_health(session, model_family)
        if database["status"] != "ready":
            issues.append("database_broken")
        if model["status"] != "ready":
            issues.append(f"model_{model['status']}")

    overall = "ready"
    if any(value.endswith("broken") for value in issues):
        overall = "broken"
    elif issues:
        overall = "configuration_unavailable"
    return {
        "status": overall,
        "observed_at": observed_at,
        "read_only": True,
        "accepted_chain": "Research V4 -> K V1 -> L V1 -> N V1 -> O V1 -> Operations V6",
        "assets": assets,
        "database": database,
        "raw_roots": roots,
        "model": model,
        "credentials": credentials,
        "issues": sorted(issues),
    }


def _root_status(path: Path | None) -> dict[str, object]:
    if path is None:
        return {"status": "configuration_unavailable", "configured": False}
    resolved = path.resolve()
    ready = resolved.is_dir() and os.access(resolved, os.W_OK)
    return {
        "status": "ready" if ready else "broken",
        "configured": True,
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_directory": resolved.is_dir(),
        "writable": ready,
    }


def _database_health(session: Session, observed_at: datetime) -> dict[str, object]:
    session.execute(text("SELECT 1")).scalar_one()
    migration = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    latest = session.execute(
        select(
            OperationalRun.id,
            OperationalRun.cycle_at,
            OperationalRun.status,
            OperationalRun.completed_at,
        ).order_by(OperationalRun.cycle_at.desc(), OperationalRun.id.asc())
    ).first()
    run_counts = _counts(session, OperationalRun.status)
    delivery_counts = _counts(session, ResearchNotificationDelivery.status)
    for status in ("pending", "claimed", "retry_wait", "delivered", "dead_letter"):
        delivery_counts.setdefault(status, 0)
    pending_outbox = session.scalar(
        select(func.count())
        .select_from(ResearchNotificationOutbox)
        .where(ResearchNotificationOutbox.delivery_status == "pending")
    )
    due_retries = session.scalar(
        select(func.count())
        .select_from(ResearchNotificationDelivery)
        .where(
            ResearchNotificationDelivery.status == "retry_wait",
            ResearchNotificationDelivery.next_attempt_at <= observed_at,
        )
    )
    expired_claims = session.scalar(
        select(func.count())
        .select_from(ResearchNotificationDelivery)
        .where(
            ResearchNotificationDelivery.status == "claimed",
            ResearchNotificationDelivery.claim_expires_at <= observed_at,
        )
    )
    due_now = session.scalar(
        select(func.count())
        .select_from(ResearchNotificationDelivery)
        .where(
            or_(
                ResearchNotificationDelivery.status == "pending",
                (
                    (ResearchNotificationDelivery.status == "retry_wait")
                    & (ResearchNotificationDelivery.next_attempt_at <= observed_at)
                ),
            )
        )
    )
    return {
        "status": "ready" if migration == LATEST_MIGRATION else "broken",
        "connectivity": "available",
        "migration": {
            "current": migration,
            "expected": LATEST_MIGRATION,
            "status": "ready" if migration == LATEST_MIGRATION else "mismatch",
        },
        "operational_runs": {
            "counts_by_status": run_counts,
            "latest": (
                None
                if latest is None
                else {
                    "id": latest.id,
                    "cycle_at": latest.cycle_at,
                    "status": latest.status,
                    "completed_at": latest.completed_at,
                }
            ),
        },
        "notifications": {"pending_outbox": int(pending_outbox or 0)},
        "deliveries": {
            "counts_by_status": delivery_counts,
            "due_now": int(due_now or 0),
            "due_retry_wait": int(due_retries or 0),
            "expired_claims": int(expired_claims or 0),
        },
    }


def _model_health(session: Session, model_family: str | None) -> dict[str, object]:
    if model_family is None:
        return {"status": "configuration_unavailable", "model_family": None}
    counts = {
        status: count
        for status, count in session.execute(
            select(ModelVersion.status, func.count())
            .where(ModelVersion.model_family == model_family)
            .group_by(ModelVersion.status)
            .order_by(ModelVersion.status)
        )
    }
    return {
        "status": "ready" if counts else "unavailable",
        "model_family": model_family,
        "versions_by_status": counts,
    }


def _counts(session: Session, column: InstrumentedAttribute[str]) -> dict[str, int]:
    rows = session.execute(
        select(column, func.count()).group_by(column).order_by(column)
    )
    return {status: int(count) for status, count in rows}
