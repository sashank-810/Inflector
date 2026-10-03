"""Thin Production M orchestration over accepted K and L module APIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from inflector_data.operations_profile import ProductionOperationsProfile
from inflector_data.opportunity_change import build_opportunity_change
from inflector_data.opportunity_change_policy import (
    OpportunityChangePolicy,
    load_opportunity_change_policy,
)
from inflector_data.opportunity_discovery import build_opportunity_discovery
from inflector_data.opportunity_policy import (
    OpportunityDiscoveryPolicy,
    load_opportunity_discovery_policy,
)
from inflector_database.models import OperationalRun, OperationalRunStage
from inflector_database.operations_repository import OperationsRepository
from inflector_database.opportunity_repository import OpportunityDiscoveryRepository

if TYPE_CHECKING:
    from inflector_data.production_operations import CyclePlan


@dataclass(frozen=True, slots=True)
class OpportunityMonitoringPolicies:
    discovery: OpportunityDiscoveryPolicy
    change: OpportunityChangePolicy


def load_monitoring_policies(
    profile: ProductionOperationsProfile, *, repository_root: Path
) -> OpportunityMonitoringPolicies:
    """Load actual K/L assets and fail closed against Operations V4 bindings."""

    if not profile.opportunity_monitoring_enabled:
        raise ValueError("opportunity monitoring is disabled by operations profile")
    discovery_asset = _bound_asset(
        repository_root, profile.opportunity_discovery_policy_asset, "discovery"
    )
    change_asset = _bound_asset(
        repository_root, profile.opportunity_change_policy_asset, "change"
    )
    discovery = load_opportunity_discovery_policy(
        discovery_asset, repository_root=repository_root
    )
    change = load_opportunity_change_policy(change_asset, repository_root=repository_root)
    if (
        discovery.code != profile.opportunity_discovery_policy_code
        or discovery.checksum_sha256
        != profile.opportunity_discovery_policy_checksum_sha256
    ):
        raise ValueError("Operations V4 discovery policy binding mismatch")
    if (
        change.code != profile.opportunity_change_policy_code
        or change.checksum_sha256 != profile.opportunity_change_policy_checksum_sha256
    ):
        raise ValueError("Operations V4 change policy binding mismatch")
    if (
        change.discovery_policy_code != discovery.code
        or change.discovery_policy_checksum_sha256 != discovery.checksum_sha256
    ):
        raise ValueError("bound change policy does not match bound discovery policy")
    return OpportunityMonitoringPolicies(discovery=discovery, change=change)


class OpportunityMonitoringOrchestrator:
    """Run accepted K/L APIs and persist only bounded operational references."""

    def __init__(self, repository_root: Path) -> None:
        self._repository_root = repository_root.resolve()

    def execute_discovery(
        self, session: Session, plan: CyclePlan
    ) -> tuple[dict[str, object], bool]:
        policies = load_monitoring_policies(
            plan.operations_profile, repository_root=self._repository_root
        )
        result = build_opportunity_discovery(
            session,
            policy=policies.discovery,
            research_profile=plan.research_profile,
            model_family=plan.inputs.model_family,
            symbols=plan.inputs.symbols,
            discovery_cutoff=plan.inputs.knowledge_cutoff,
            repository_root=self._repository_root,
        )
        return {
            "status": "completed",
            "opportunity_discovery_run_id": str(result.run_id),
            "opportunity_discovery_run_key": result.run_key_sha256,
            "discovery_cutoff": plan.inputs.knowledge_cutoff.isoformat(),
            "selected_snapshot_set_checksum_sha256": (
                result.selected_snapshot_set_checksum_sha256
            ),
            "requested_symbol_checksum_sha256": plan.symbol_set_checksum_sha256,
            "requested_securities": result.requested_securities,
            "rankable_securities": result.rankable_securities,
            "unrankable_securities": result.unranked_securities,
            "created_run": result.created_run,
            "already_completed": result.already_completed,
        }, True

    def execute_change(
        self, session: Session, plan: CyclePlan
    ) -> tuple[dict[str, object], bool]:
        policies = load_monitoring_policies(
            plan.operations_profile, repository_root=self._repository_root
        )
        operational = OperationsRepository(session).get_by_key(plan.run_key_sha256)
        if operational is None:
            raise ValueError("current operational monitoring run is unavailable")
        discovery_stage = _stage(operational, "opportunity_discovery")
        if discovery_stage.status != "completed":
            raise ValueError("opportunity discovery stage must complete before change")
        current_discovery_id = _result_uuid(
            discovery_stage.result_summary_json, "opportunity_discovery_run_id"
        )
        current_discovery = OpportunityDiscoveryRepository(session).get_run(
            current_discovery_id
        )
        if current_discovery is None or current_discovery.status != "completed":
            raise ValueError("current completed opportunity discovery run is unavailable")

        change_stage = _stage(operational, "opportunity_change")
        frozen = change_stage.result_summary_json
        if frozen.get("baseline_selection_frozen") is True:
            stored_current = _result_uuid(frozen, "current_discovery_run_id")
            if stored_current != current_discovery_id:
                raise ValueError("frozen monitoring current discovery identity conflicts")
            baseline_discovery_id = _optional_result_uuid(
                frozen, "baseline_discovery_run_id"
            )
        else:
            baseline_discovery_id = _select_baseline(
                session,
                plan=plan,
                operational_run=operational,
                current_discovery_id=current_discovery_id,
            )
            change_stage.result_summary_json = {
                "status": (
                    "baseline_frozen"
                    if baseline_discovery_id is not None
                    else "no_compatible_baseline"
                ),
                "baseline_selection_frozen": True,
                "baseline_selection_semantics": (
                    plan.operations_profile.opportunity_change_baseline_semantics
                ),
                "baseline_discovery_run_id": (
                    str(baseline_discovery_id)
                    if baseline_discovery_id is not None
                    else None
                ),
                "current_discovery_run_id": str(current_discovery_id),
            }
            session.commit()

        if baseline_discovery_id is None:
            return {
                **change_stage.result_summary_json,
                "status": "no_compatible_baseline",
                "opportunity_change_run_id": None,
                "opportunity_change_run_key": None,
                "compared_identities": 0,
                "changed_identities": 0,
            }, True
        result = build_opportunity_change(
            session,
            policy=policies.change,
            baseline_run_id=baseline_discovery_id,
            current_run_id=current_discovery_id,
        )
        return {
            **change_stage.result_summary_json,
            "status": "completed",
            "opportunity_change_run_id": str(result.run_id),
            "opportunity_change_run_key": result.run_key_sha256,
            "baseline_discovery_run_id": str(baseline_discovery_id),
            "current_discovery_run_id": str(current_discovery_id),
            "compared_identities": result.compared_identities,
            "changed_identities": result.changed_identities,
            "created_run": result.created_run,
            "already_completed": result.already_completed,
        }, True


def monitoring_baseline_status(session: Session, plan: CyclePlan) -> dict[str, object]:
    """Return informational prior monitored-stage availability for doctor output."""

    candidates = _prior_monitoring_runs(session, plan=plan, current_run_id=None)
    if not candidates:
        return {"status": "no_compatible_baseline", "informational": True}
    candidate, discovery_id = candidates[0]
    return {
        "status": "available",
        "informational": True,
        "operational_run_id": str(candidate.id),
        "opportunity_discovery_run_id": str(discovery_id),
    }


def _select_baseline(
    session: Session,
    *,
    plan: CyclePlan,
    operational_run: OperationalRun,
    current_discovery_id: UUID,
) -> UUID | None:
    discovery_repository = OpportunityDiscoveryRepository(session)
    current = discovery_repository.get_run(current_discovery_id)
    if current is None:
        raise ValueError("current discovery run is unavailable")
    for _, candidate_id in _prior_monitoring_runs(
        session, plan=plan, current_run_id=operational_run.id
    ):
        candidate = discovery_repository.get_run(candidate_id)
        if candidate is None or candidate.status != "completed":
            continue
        if candidate.discovery_cutoff >= current.discovery_cutoff:
            continue
        if _compatible_discovery_semantics(candidate, current):
            return candidate.id
    return None


def _prior_monitoring_runs(
    session: Session, *, plan: CyclePlan, current_run_id: UUID | None
) -> list[tuple[OperationalRun, UUID]]:
    statement = (
        select(OperationalRun)
        .options(selectinload(OperationalRun.stages))
        .where(
            OperationalRun.operations_profile_code
            == plan.operations_profile.operations_profile_code,
            OperationalRun.operations_profile_checksum_sha256
            == plan.operations_profile.checksum_sha256,
            OperationalRun.research_profile_code
            == plan.research_profile.research_profile_code,
            OperationalRun.research_profile_checksum_sha256
            == plan.research_profile.checksum_sha256,
            OperationalRun.model_family == plan.inputs.model_family,
            OperationalRun.knowledge_cutoff < plan.inputs.knowledge_cutoff,
            OperationalRun.status.in_(("completed", "completed_with_symbol_failures")),
        )
        .order_by(OperationalRun.knowledge_cutoff.desc(), OperationalRun.run_key_sha256.asc())
    )
    if current_run_id is not None:
        statement = statement.where(OperationalRun.id != current_run_id)
    results: list[tuple[OperationalRun, UUID]] = []
    for run in session.scalars(statement):
        stage = next(
            (
                item
                for item in run.stages
                if item.stage_name == "opportunity_discovery" and item.status == "completed"
            ),
            None,
        )
        if stage is None:
            continue
        try:
            discovery_id = _result_uuid(
                stage.result_summary_json, "opportunity_discovery_run_id"
            )
        except ValueError:
            continue
        results.append((run, discovery_id))
    return results


def _compatible_discovery_semantics(left: object, right: object) -> bool:
    fields = (
        "discovery_policy_checksum_sha256",
        "scoring_configuration_checksum_sha256",
        "research_profile_checksum_sha256",
        "financial_primitive_policy_checksum_sha256",
        "financial_endpoint_policy_checksum_sha256",
        "ranking_version",
        "snapshot_selection_version",
        "model_family",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _stage(run: OperationalRun, name: str) -> OperationalRunStage:
    stage = next((item for item in run.stages if item.stage_name == name), None)
    if stage is None:
        raise ValueError(f"operational monitoring stage is unavailable: {name}")
    return stage


def _result_uuid(value: dict[str, object], field: str) -> UUID:
    item = value.get(field)
    if not isinstance(item, str):
        raise ValueError(f"operational stage result {field} is unavailable")
    try:
        return UUID(item)
    except ValueError as error:
        raise ValueError(f"operational stage result {field} is malformed") from error


def _optional_result_uuid(value: dict[str, object], field: str) -> UUID | None:
    return None if value.get(field) is None else _result_uuid(value, field)


def _bound_asset(repository_root: Path, value: str | None, label: str) -> Path:
    if value is None:
        raise ValueError(f"Operations V4 {label} policy asset is unavailable")
    root = repository_root.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Operations V4 {label} policy asset escapes repository") from error
    return candidate
