"""Append-only persistence for deterministic Production K run comparisons."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import OpportunityChangeItem, OpportunityChangeRun


class OpportunityChangeIntegrityError(RuntimeError):
    """Raised when immutable comparison state conflicts with persisted state."""


@dataclass(frozen=True, slots=True)
class OpportunityChangeItemWrite:
    identity_key: str
    identity_basis: str
    company_id: UUID | None
    security_id: UUID | None
    baseline_symbol: str | None
    current_symbol: str | None
    baseline_discovery_item_id: UUID | None
    current_discovery_item_id: UUID | None
    baseline_score_snapshot_id: UUID | None
    current_score_snapshot_id: UUID | None
    change_codes_json: list[object]
    changed: bool
    baseline_rankable: bool | None
    current_rankable: bool | None
    baseline_unranked_reason: str | None
    current_unranked_reason: str | None
    baseline_final_score: Decimal | None
    current_final_score: Decimal | None
    score_delta: Decimal | None
    baseline_score_rank: int | None
    current_score_rank: int | None
    rank_delta: int | None
    baseline_confidence: Decimal | None
    current_confidence: Decimal | None
    confidence_delta: Decimal | None
    components_gained_json: list[object]
    components_lost_json: list[object]
    component_change_detail_json: dict[str, object]
    detail_json: dict[str, object]


class OpportunityChangeRepository:
    """Persist immutable comparison runs and canonical per-identity change records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_run(self, run_id: UUID) -> OpportunityChangeRun | None:
        return self._session.scalar(
            select(OpportunityChangeRun)
            .options(selectinload(OpportunityChangeRun.items))
            .where(OpportunityChangeRun.id == run_id)
        )

    def get_by_key(self, run_key_sha256: str) -> OpportunityChangeRun | None:
        return self._session.scalar(
            select(OpportunityChangeRun).where(
                OpportunityChangeRun.run_key_sha256 == run_key_sha256
            )
        )

    def create_run(
        self,
        *,
        run_key_sha256: str,
        change_policy_code: str,
        change_policy_checksum_sha256: str,
        baseline_discovery_run_id: UUID,
        current_discovery_run_id: UUID,
        baseline_run_key_sha256: str,
        current_run_key_sha256: str,
        baseline_snapshot_set_checksum_sha256: str,
        current_snapshot_set_checksum_sha256: str,
        comparison_version: str,
        inputs_json: dict[str, object],
    ) -> tuple[OpportunityChangeRun, bool]:
        projection = (
            change_policy_code,
            change_policy_checksum_sha256,
            baseline_discovery_run_id,
            current_discovery_run_id,
            baseline_run_key_sha256,
            current_run_key_sha256,
            baseline_snapshot_set_checksum_sha256,
            current_snapshot_set_checksum_sha256,
            comparison_version,
            inputs_json,
        )
        existing = self.get_by_key(run_key_sha256)
        if existing is not None:
            if _run_projection(existing) != projection:
                raise OpportunityChangeIntegrityError(
                    "existing opportunity change run key conflicts"
                )
            return existing, False
        record = OpportunityChangeRun(
            run_key_sha256=run_key_sha256,
            change_policy_code=change_policy_code,
            change_policy_checksum_sha256=change_policy_checksum_sha256,
            baseline_discovery_run_id=baseline_discovery_run_id,
            current_discovery_run_id=current_discovery_run_id,
            baseline_run_key_sha256=baseline_run_key_sha256,
            current_run_key_sha256=current_run_key_sha256,
            baseline_snapshot_set_checksum_sha256=(
                baseline_snapshot_set_checksum_sha256
            ),
            current_snapshot_set_checksum_sha256=current_snapshot_set_checksum_sha256,
            comparison_version=comparison_version,
            inputs_json=inputs_json,
            status="planned",
            summary_json={},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_item(
        self, run: OpportunityChangeRun, value: OpportunityChangeItemWrite
    ) -> tuple[OpportunityChangeItem, bool]:
        existing = self._session.scalar(
            select(OpportunityChangeItem).where(
                OpportunityChangeItem.opportunity_change_run_id == run.id,
                OpportunityChangeItem.identity_key == value.identity_key,
            )
        )
        if existing is not None:
            if _item_projection(existing) != _write_projection(value):
                raise OpportunityChangeIntegrityError(
                    "existing opportunity change item conflicts"
                )
            return existing, False
        record = OpportunityChangeItem(
            opportunity_change_run_id=run.id,
            **{field.name: getattr(value, field.name) for field in fields(value)},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def complete(
        self, run: OpportunityChangeRun, *, summary: dict[str, object]
    ) -> OpportunityChangeRun:
        if run.status == "completed":
            if run.summary_json != summary:
                raise OpportunityChangeIntegrityError(
                    "completed opportunity change summary conflicts"
                )
            return run
        if run.status != "planned":
            raise OpportunityChangeIntegrityError(
                f"invalid opportunity change transition {run.status} -> completed"
            )
        self._session.execute(
            update(OpportunityChangeRun)
            .where(OpportunityChangeRun.id == run.id)
            .values(status="completed", summary_json=summary, completed_at=func.now())
        )
        self._session.flush()
        self._session.refresh(run)
        return run


def _run_projection(value: OpportunityChangeRun) -> tuple[object, ...]:
    return (
        value.change_policy_code,
        value.change_policy_checksum_sha256,
        value.baseline_discovery_run_id,
        value.current_discovery_run_id,
        value.baseline_run_key_sha256,
        value.current_run_key_sha256,
        value.baseline_snapshot_set_checksum_sha256,
        value.current_snapshot_set_checksum_sha256,
        value.comparison_version,
        value.inputs_json,
    )


def _item_projection(value: OpportunityChangeItem) -> tuple[object, ...]:
    return tuple(getattr(value, field.name) for field in fields(OpportunityChangeItemWrite))


def _write_projection(value: OpportunityChangeItemWrite) -> tuple[object, ...]:
    return tuple(getattr(value, field.name) for field in fields(value))
