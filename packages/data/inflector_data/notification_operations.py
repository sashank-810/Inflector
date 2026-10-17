"""Thin Operations V5 adapter for accepted Production N projection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.model_release import resolve_cycle_release_context
from inflector_data.model_release_policy import load_model_release_policy
from inflector_data.operations_profile import ProductionOperationsProfile
from inflector_data.research_alert_policy import (
    ResearchAlertPolicy,
    load_research_alert_policy,
)
from inflector_data.research_notifications import project_research_notifications
from inflector_database.models import (
    ModelReleaseCycleBinding,
    OperationalRunStage,
    OpportunityChangeRun,
)
from inflector_database.operations_repository import OperationsRepository
from inflector_database.opportunity_change_repository import OpportunityChangeRepository

if TYPE_CHECKING:
    from inflector_data.production_operations import CyclePlan


def load_bound_research_alert_policy(
    profile: ProductionOperationsProfile, *, repository_root: Path
) -> ResearchAlertPolicy:
    """Load the actual N policy and fail closed against Operations V5."""

    if not profile.notification_projection_enabled:
        raise ValueError("notification projection is disabled by operations profile")
    if profile.research_alert_policy_asset is None:
        raise ValueError("Operations V5 research alert policy asset is unavailable")
    root = repository_root.resolve()
    candidate = (root / profile.research_alert_policy_asset).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("Operations V5 alert policy asset escapes repository") from error
    policy = load_research_alert_policy(candidate, repository_root=root)
    if (
        policy.code != profile.research_alert_policy_code
        or policy.checksum_sha256 != profile.research_alert_policy_checksum_sha256
    ):
        raise ValueError("Operations V5 research alert policy binding mismatch")
    if (
        policy.opportunity_change_policy_code != profile.opportunity_change_policy_code
        or policy.opportunity_change_policy_checksum_sha256
        != profile.opportunity_change_policy_checksum_sha256
    ):
        raise ValueError("Operations V5 alert policy does not bind its L policy")
    return policy


class NotificationProjectionOrchestrator:
    """Project only the L run frozen in the current operational stage ledger."""

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root.resolve()

    def execute(self, session: Session, plan: CyclePlan) -> tuple[dict[str, object], bool]:
        policy = load_bound_research_alert_policy(
            plan.operations_profile, repository_root=self._repository_root
        )
        operational = OperationsRepository(session).get_by_key(plan.run_key_sha256)
        if operational is None:
            raise ValueError("current operational notification run is unavailable")
        change_stage = _stage(operational.stages, "opportunity_change")
        if change_stage.status != "completed":
            raise ValueError("opportunity change stage must complete before notification")
        raw_change_run_id = change_stage.result_summary_json.get("opportunity_change_run_id")
        if raw_change_run_id is None:
            if change_stage.result_summary_json.get("status") != "no_compatible_baseline":
                raise ValueError("completed opportunity change stage lacks source run")
            return _no_change_run(policy), True
        if not isinstance(raw_change_run_id, str):
            raise ValueError("opportunity change run identity is malformed")
        try:
            change_run_id = UUID(raw_change_run_id)
        except ValueError as error:
            raise ValueError("opportunity change run identity is malformed") from error
        change_run = OpportunityChangeRepository(session).get_run(change_run_id)
        if change_run is None:
            raise ValueError("source opportunity change run is unavailable")
        _validate_stage_lineage(change_stage, change_run)
        binding = session.scalar(
            select(ModelReleaseCycleBinding).where(
                ModelReleaseCycleBinding.operational_run_id == operational.id
            )
        )
        release_identity: dict[str, object] | None = None
        if binding is not None:
            release_policy = load_model_release_policy(
                self._repository_root / "config/operations/production_model_release_v1.json"
            )
            context = resolve_cycle_release_context(
                session,
                policy=release_policy,
                operational_run_id=operational.id,
            )
            source_release = change_run.inputs_json.get("current_model_release")
            if (
                not isinstance(source_release, dict)
                or source_release.get("cycle_release_binding_key_sha256")
                != context.binding.binding_key_sha256
            ):
                raise ValueError("notification source does not use the frozen cycle release")
            release_identity = source_release
        result = project_research_notifications(
            session, policy=policy, source_change_run_id=change_run_id
        )
        summary: dict[str, object] = {
            "status": "completed",
            "source_change_run_id": str(result.source_change_run_id),
            "alert_policy_code": policy.code,
            "alert_policy_checksum_sha256": policy.checksum_sha256,
            "eligible_items": result.eligible_items,
            "outbox_rows_created": result.outbox_rows_created,
            "outbox_rows_reused": result.outbox_rows_reused,
            "source_changed_items": result.source_changed_items,
            "source_total_items": result.source_total_items,
        }
        if release_identity is not None:
            summary["model_release"] = release_identity
        return summary, True


def _validate_stage_lineage(stage: OperationalRunStage, run: OpportunityChangeRun) -> None:
    result = stage.result_summary_json
    expected = {
        "opportunity_change_run_key": run.run_key_sha256,
        "baseline_discovery_run_id": str(run.baseline_discovery_run_id),
        "current_discovery_run_id": str(run.current_discovery_run_id),
    }
    for field, value in expected.items():
        if result.get(field) != value:
            raise ValueError(f"operational opportunity change lineage mismatch: {field}")


def _no_change_run(policy: ResearchAlertPolicy) -> dict[str, object]:
    return {
        "status": "no_change_run",
        "source_change_run_id": None,
        "alert_policy_code": policy.code,
        "alert_policy_checksum_sha256": policy.checksum_sha256,
        "eligible_items": 0,
        "outbox_rows_created": 0,
        "outbox_rows_reused": 0,
        "source_changed_items": 0,
        "source_total_items": 0,
    }


def _stage(stages: list[OperationalRunStage], name: str) -> OperationalRunStage:
    stage = next((item for item in stages if item.stage_name == name), None)
    if stage is None:
        raise ValueError(f"operational notification dependency is unavailable: {name}")
    return stage
