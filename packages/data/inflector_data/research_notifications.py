"""Deterministic factual notification projection from immutable L records."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.research_alert_policy import (
    ResearchAlertPolicy,
    canonical_json_sha256,
)
from inflector_database.models import OpportunityChangeItem, OpportunityChangeRun
from inflector_database.opportunity_change_repository import OpportunityChangeRepository
from inflector_database.research_notification_repository import (
    ResearchNotificationRepository,
    ResearchNotificationWrite,
)


@dataclass(frozen=True, slots=True)
class ResearchNotificationProjectionResult:
    source_change_run_id: UUID
    source_total_items: int
    source_changed_items: int
    eligible_items: int
    outbox_rows_created: int
    outbox_rows_reused: int


def project_research_notifications(
    session: Session,
    *,
    policy: ResearchAlertPolicy,
    source_change_run_id: UUID,
) -> ResearchNotificationProjectionResult:
    """Project one pending event per eligible immutable L change item."""

    change_run = OpportunityChangeRepository(session).get_run(source_change_run_id)
    if change_run is None:
        raise ValueError("source opportunity change run is unavailable")
    _validate_source_run(change_run, policy)
    repository = ResearchNotificationRepository(session)
    created = 0
    reused = 0
    eligible = 0
    for item in sorted(change_run.items, key=_item_order):
        codes = _change_codes(item, policy)
        matched = tuple(code for code in policy.included_trigger_codes if code in codes)
        if not matched:
            continue
        eligible += 1
        payload = _payload(policy, change_run, item, matched, codes)
        notification_key = canonical_json_sha256(
            {
                "alert_policy_checksum_sha256": policy.checksum_sha256,
                "source_change_run_id": str(change_run.id),
                "source_change_run_key_sha256": change_run.run_key_sha256,
                "source_change_item_id": str(item.id),
                "matched_trigger_codes": list(matched),
                "payload_schema_version": policy.payload_schema_version,
            }
        )
        _, was_created = repository.create_or_reuse(
            ResearchNotificationWrite(
                notification_key_sha256=notification_key,
                alert_policy_code=policy.code,
                alert_policy_checksum_sha256=policy.checksum_sha256,
                payload_schema_version=policy.payload_schema_version,
                source_change_run_id=change_run.id,
                source_change_item_id=item.id,
                company_id=item.company_id,
                security_id=item.security_id,
                baseline_symbol=item.baseline_symbol,
                current_symbol=item.current_symbol,
                matched_trigger_codes_json=list(matched),
                payload_json=payload,
            )
        )
        created += int(was_created)
        reused += int(not was_created)
    return ResearchNotificationProjectionResult(
        source_change_run_id=change_run.id,
        source_total_items=len(change_run.items),
        source_changed_items=sum(item.changed for item in change_run.items),
        eligible_items=eligible,
        outbox_rows_created=created,
        outbox_rows_reused=reused,
    )


def _validate_source_run(
    run: OpportunityChangeRun, policy: ResearchAlertPolicy
) -> None:
    if run.status != "completed":
        raise ValueError("source opportunity change run must be completed")
    if (
        run.change_policy_code != policy.opportunity_change_policy_code
        or run.change_policy_checksum_sha256
        != policy.opportunity_change_policy_checksum_sha256
    ):
        raise ValueError("source opportunity change policy binding mismatch")
    expected = {
        "baseline_discovery_run_id": str(run.baseline_discovery_run_id),
        "current_discovery_run_id": str(run.current_discovery_run_id),
        "baseline_run_key": run.baseline_run_key_sha256,
        "current_run_key": run.current_run_key_sha256,
        "baseline_snapshot_set_checksum": (
            run.baseline_snapshot_set_checksum_sha256
        ),
        "current_snapshot_set_checksum": run.current_snapshot_set_checksum_sha256,
    }
    for field, value in expected.items():
        if run.inputs_json.get(field) != value:
            raise ValueError(f"source opportunity change lineage mismatch: {field}")


def _change_codes(
    item: OpportunityChangeItem, policy: ResearchAlertPolicy
) -> tuple[str, ...]:
    codes = tuple(value for value in item.change_codes_json if isinstance(value, str))
    if len(codes) != len(item.change_codes_json) or len(codes) != len(set(codes)):
        raise ValueError("source opportunity change codes are malformed")
    if not set(codes) <= policy.allowed_change_codes:
        raise ValueError("source opportunity change item contains unknown change codes")
    return codes


def _payload(
    policy: ResearchAlertPolicy,
    run: OpportunityChangeRun,
    item: OpportunityChangeItem,
    matched: tuple[str, ...],
    all_codes: tuple[str, ...],
) -> dict[str, object]:
    return {
        "event_type": "research_change_notification",
        "payload_schema_version": policy.payload_schema_version,
        "alert_policy_code": policy.code,
        "alert_policy_checksum_sha256": policy.checksum_sha256,
        "opportunity_change_policy_code": run.change_policy_code,
        "opportunity_change_policy_checksum_sha256": (
            run.change_policy_checksum_sha256
        ),
        "source_change_run_id": str(run.id),
        "source_change_run_key_sha256": run.run_key_sha256,
        "source_change_item_id": str(item.id),
        "source_baseline_discovery_run_id": str(run.baseline_discovery_run_id),
        "source_current_discovery_run_id": str(run.current_discovery_run_id),
        "company_id": _uuid(item.company_id),
        "security_id": _uuid(item.security_id),
        "baseline_symbol": item.baseline_symbol,
        "current_symbol": item.current_symbol,
        "matched_trigger_codes": list(matched),
        "all_change_codes": list(all_codes),
        "baseline_rankable": item.baseline_rankable,
        "current_rankable": item.current_rankable,
        "baseline_unranked_reason": item.baseline_unranked_reason,
        "current_unranked_reason": item.current_unranked_reason,
        "baseline_final_score": _decimal(item.baseline_final_score),
        "current_final_score": _decimal(item.current_final_score),
        "score_delta": _decimal(item.score_delta),
        "baseline_score_rank": item.baseline_score_rank,
        "current_score_rank": item.current_score_rank,
        "rank_delta": item.rank_delta,
        "baseline_confidence": _decimal(item.baseline_confidence),
        "current_confidence": _decimal(item.current_confidence),
        "confidence_delta": _decimal(item.confidence_delta),
        "components_gained": list(item.components_gained_json),
        "components_lost": list(item.components_lost_json),
        "component_changes": item.component_change_detail_json,
        "change_detail": item.detail_json,
    }


def _item_order(item: OpportunityChangeItem) -> tuple[str, str, str, str]:
    return (
        item.current_symbol or "",
        item.baseline_symbol or "",
        str(item.security_id or ""),
        str(item.id),
    )


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _uuid(value: UUID | None) -> str | None:
    return str(value) if value is not None else None
