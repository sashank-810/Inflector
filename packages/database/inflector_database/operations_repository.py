"""Persistent state machine and lease repository for production operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import (
    OperationalRun,
    OperationalRunStage,
    OperationalRunSymbol,
)

RUN_TERMINAL_STATUSES = frozenset({"completed", "completed_with_symbol_failures", "failed"})
RUN_TRANSITIONS = {
    "planned": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "completed_with_symbol_failures", "failed", "stale"}),
    "stale": frozenset({"running", "failed"}),
    "completed": frozenset(),
    "completed_with_symbol_failures": frozenset(),
    "failed": frozenset({"running"}),
}
STAGE_TRANSITIONS = {
    "planned": frozenset({"running", "blocked", "skipped"}),
    "running": frozenset({"completed", "failed"}),
    "failed": frozenset({"running", "blocked"}),
    "blocked": frozenset({"running"}),
    "skipped": frozenset({"running"}),
    "completed": frozenset(),
}


class OperationsStateError(RuntimeError):
    """An operational state transition or immutable input is invalid."""


class OperationsLeaseError(RuntimeError):
    """The requested logical run is actively owned by another process."""


class OperationsRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_run(self, run_id: UUID) -> OperationalRun | None:
        return self._session.scalar(
            select(OperationalRun)
            .options(
                selectinload(OperationalRun.stages),
                selectinload(OperationalRun.symbols),
            )
            .where(OperationalRun.id == run_id)
        )

    def get_by_key(self, run_key_sha256: str) -> OperationalRun | None:
        return self._session.scalar(
            select(OperationalRun)
            .options(
                selectinload(OperationalRun.stages),
                selectinload(OperationalRun.symbols),
            )
            .where(OperationalRun.run_key_sha256 == run_key_sha256)
        )

    def recent_runs(self, limit: int) -> tuple[OperationalRun, ...]:
        return tuple(
            self._session.scalars(
                select(OperationalRun)
                .options(
                    selectinload(OperationalRun.stages),
                    selectinload(OperationalRun.symbols),
                )
                .order_by(OperationalRun.cycle_at.desc(), OperationalRun.run_key_sha256.asc())
                .limit(limit)
            )
        )

    def create_run(
        self,
        *,
        run_key_sha256: str,
        operations_profile_code: str,
        operations_profile_checksum_sha256: str,
        research_profile_code: str,
        research_profile_checksum_sha256: str,
        model_family: str,
        fiscal_year: int,
        fiscal_quarter: int,
        cycle_at: datetime,
        knowledge_cutoff: datetime,
        symbol_set_checksum_sha256: str,
        ordered_symbols: tuple[str, ...],
        inputs: dict[str, object],
        planned_at: datetime,
        stages: tuple[tuple[str, bool], ...],
    ) -> OperationalRun:
        record = OperationalRun(
            run_key_sha256=run_key_sha256,
            operations_profile_code=operations_profile_code,
            operations_profile_checksum_sha256=operations_profile_checksum_sha256,
            research_profile_code=research_profile_code,
            research_profile_checksum_sha256=research_profile_checksum_sha256,
            model_family=model_family,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            cycle_at=_aware_utc(cycle_at),
            knowledge_cutoff=_aware_utc(knowledge_cutoff),
            symbol_set_checksum_sha256=symbol_set_checksum_sha256,
            ordered_symbols_json=list(ordered_symbols),
            inputs_json=inputs,
            status="planned",
            planned_at=_aware_utc(planned_at),
            summary_json={},
        )
        for ordinal, (name, required) in enumerate(stages):
            record.stages.append(
                OperationalRunStage(
                    stage_name=name,
                    stage_order=ordinal,
                    required=required,
                    status="planned",
                    attempt_count=0,
                    result_summary_json={},
                    attempt_history_json=[],
                )
            )
        self._session.add(record)
        self._session.flush()
        return record

    def acquire_lease(
        self,
        *,
        run_id: UUID,
        owner_token: str,
        acquired_at: datetime,
        lease_seconds: int,
    ) -> OperationalRun:
        now = _aware_utc(acquired_at)
        result = self._session.execute(
            update(OperationalRun)
            .where(
                OperationalRun.id == run_id,
                OperationalRun.status.in_(("planned", "failed")),
                OperationalRun.lease_owner_token.is_(None),
            )
            .values(
                status="running",
                started_at=now,
                completed_at=None,
                lease_owner_token=owner_token,
                lease_acquired_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                error_code=None,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise OperationsLeaseError("operational run lease is unavailable")
        self._session.flush()
        self._session.expire_all()
        record = self.get_run(run_id)
        assert record is not None
        return record

    def recover_stale_lease(
        self,
        *,
        run_id: UUID,
        owner_token: str,
        recovered_at: datetime,
        lease_seconds: int,
    ) -> OperationalRun:
        now = _aware_utc(recovered_at)
        result = self._session.execute(
            update(OperationalRun)
            .where(
                OperationalRun.id == run_id,
                OperationalRun.status == "running",
                OperationalRun.lease_expires_at.is_not(None),
                OperationalRun.lease_expires_at <= now,
            )
            .values(
                status="running",
                lease_owner_token=owner_token,
                lease_acquired_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                error_code=None,
                error_message=None,
            )
            .execution_options(synchronize_session=False)
        )
        if not isinstance(result, CursorResult) or result.rowcount != 1:
            raise OperationsLeaseError("run is not stale or cannot be recovered")
        self._session.flush()
        self._session.expire_all()
        record = self.get_run(run_id)
        assert record is not None
        return record

    def lease_state(self, run: OperationalRun, at: datetime) -> str:
        now = _aware_utc(at)
        expires = _optional_utc(run.lease_expires_at)
        if run.lease_owner_token is None or expires is None:
            return "none"
        return "expired" if expires <= now else "active"

    def renew_lease(
        self, run: OperationalRun, *, owner_token: str, at: datetime, lease_seconds: int
    ) -> None:
        if run.lease_owner_token != owner_token or run.status != "running":
            raise OperationsLeaseError("operational run lease ownership was lost")
        now = _aware_utc(at)
        run.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self._session.flush()

    def start_stage(self, stage: OperationalRunStage, *, at: datetime) -> None:
        self._transition_stage(stage, "running")
        stage.attempt_count += 1
        stage.started_at = _aware_utc(at)
        stage.completed_at = None
        stage.error_code = None
        stage.error_message = None
        self._session.flush()

    def finish_stage(
        self,
        stage: OperationalRunStage,
        *,
        status: str,
        at: datetime,
        result: dict[str, object],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._transition_stage(stage, status)
        completed_at = _aware_utc(at)
        stage.completed_at = completed_at
        stage.result_summary_json = result
        history = list(stage.attempt_history_json)
        started_at = _optional_utc(stage.started_at)
        history.append(
            {
                "attempt": stage.attempt_count,
                "started_at": started_at.isoformat() if started_at else None,
                "completed_at": completed_at.isoformat(),
                "status": status,
                "error_code": error_code,
            }
        )
        stage.attempt_history_json = history
        stage.error_code = error_code
        stage.error_message = error_message
        self._session.flush()

    def block_remaining_stages(self, run: OperationalRun, *, at: datetime, reason: str) -> None:
        for stage in sorted(run.stages, key=lambda item: item.stage_order):
            if stage.status in {"planned", "failed"}:
                if stage.status == "failed":
                    continue
                self._transition_stage(stage, "blocked")
                stage.completed_at = _aware_utc(at)
                stage.error_code = "predecessor_failed"
                stage.error_message = reason
        self._session.flush()

    def upsert_symbol_result(
        self,
        run: OperationalRun,
        *,
        symbol: str,
        ordinal: int,
        status: str,
        snapshot_id: UUID | None,
        result: dict[str, object],
        change: dict[str, object],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> OperationalRunSymbol:
        existing = next((item for item in run.symbols if item.symbol == symbol), None)
        record = existing or OperationalRunSymbol(
            run=run,
            symbol=symbol,
            ordinal=ordinal,
            status=status,
            snapshot_id=snapshot_id,
            result_json=result,
            change_json=change,
        )
        if existing is None:
            self._session.add(record)
        else:
            record.status = status
            record.snapshot_id = snapshot_id
            record.result_json = result
            record.change_json = change
        record.error_code = error_code
        record.error_message = error_message
        self._session.flush()
        return record

    def finish_run(
        self,
        run: OperationalRun,
        *,
        status: str,
        at: datetime,
        summary: dict[str, object],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._transition_run(run, status)
        run.completed_at = _aware_utc(at)
        run.summary_json = summary
        run.error_code = error_code
        run.error_message = error_message
        run.lease_owner_token = None
        run.lease_acquired_at = None
        run.lease_expires_at = None
        self._session.flush()

    @staticmethod
    def assert_immutable_inputs(run: OperationalRun, expected: dict[str, object]) -> None:
        if run.inputs_json != expected:
            raise OperationsStateError("resume inputs conflict with immutable operational run")

    @staticmethod
    def _transition_run(run: OperationalRun, target: str) -> None:
        if target not in RUN_TRANSITIONS.get(run.status, frozenset()):
            raise OperationsStateError(
                f"illegal operational run transition: {run.status} -> {target}"
            )
        run.status = target

    @staticmethod
    def _transition_stage(stage: OperationalRunStage, target: str) -> None:
        if target not in STAGE_TRANSITIONS.get(stage.status, frozenset()):
            raise OperationsStateError(f"illegal stage transition: {stage.status} -> {target}")
        stage.status = target


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operational timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
