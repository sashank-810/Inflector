"""Append-only persistence boundary for strict historical research evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import BacktestObservation, BacktestOutcome, BacktestRun


class BacktestIntegrityError(RuntimeError):
    """Raised when immutable historical evaluation state conflicts."""


@dataclass(frozen=True, slots=True)
class BacktestObservationWrite:
    company_id: UUID
    security_id: UUID
    score_snapshot_id: UUID | None
    symbol: str
    knowledge_cutoff: datetime
    observation_status: str
    selected_fiscal_year: int | None
    selected_fiscal_quarter: int | None
    selected_filing_scope: str | None
    selected_period_end: date | None
    snapshot_status: str | None
    snapshot_fingerprint_sha256: str | None
    research_state_projection_version: str
    research_state_sha256: str
    research_state_changed: bool
    detail_json: dict[str, object]


@dataclass(frozen=True, slots=True)
class BacktestOutcomeWrite:
    horizon_observations: int
    outcome_status: str
    entry_trading_date: date | None
    exit_trading_date: date | None
    entry_adjusted_close: Decimal | None
    exit_adjusted_close: Decimal | None
    security_return: Decimal | None
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    outcome_data_cutoff: datetime
    provenance_json: dict[str, object]


class BacktestRepository:
    """Create once, then advance only the explicit backtest lifecycle."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_run(self, run_id: UUID) -> BacktestRun | None:
        return self._session.scalar(
            select(BacktestRun)
            .options(
                selectinload(BacktestRun.observations).selectinload(BacktestObservation.outcomes)
            )
            .where(BacktestRun.id == run_id)
        )

    def get_by_key(self, run_key_sha256: str) -> BacktestRun | None:
        return self._session.scalar(
            select(BacktestRun).where(BacktestRun.run_key_sha256 == run_key_sha256)
        )

    def create_run(
        self,
        *,
        run_key_sha256: str,
        backtest_policy_code: str,
        backtest_policy_checksum_sha256: str,
        availability_manifest_code: str,
        availability_manifest_checksum_sha256: str,
        scoring_configuration_id: UUID,
        scoring_configuration_checksum_sha256: str,
        research_profile_code: str,
        research_profile_checksum_sha256: str,
        financial_primitive_policy_checksum_sha256: str,
        financial_endpoint_policy_checksum_sha256: str,
        source_state_checksum_sha256: str,
        model_family: str,
        cutoff_start: datetime,
        cutoff_end: datetime,
        cutoff_cadence: str,
        universe_policy: str,
        benchmark_code: str,
        return_horizons: tuple[int, ...],
        ordered_symbols: tuple[str, ...],
        symbol_set_checksum_sha256: str,
        inputs_json: dict[str, object],
    ) -> tuple[BacktestRun, bool]:
        existing = self.get_by_key(run_key_sha256)
        if existing is not None:
            return existing, False
        record = BacktestRun(
            run_key_sha256=run_key_sha256,
            backtest_policy_code=backtest_policy_code,
            backtest_policy_checksum_sha256=backtest_policy_checksum_sha256,
            availability_manifest_code=availability_manifest_code,
            availability_manifest_checksum_sha256=availability_manifest_checksum_sha256,
            scoring_configuration_id=scoring_configuration_id,
            scoring_configuration_checksum_sha256=scoring_configuration_checksum_sha256,
            research_profile_code=research_profile_code,
            research_profile_checksum_sha256=research_profile_checksum_sha256,
            financial_primitive_policy_checksum_sha256=(financial_primitive_policy_checksum_sha256),
            financial_endpoint_policy_checksum_sha256=financial_endpoint_policy_checksum_sha256,
            source_state_checksum_sha256=source_state_checksum_sha256,
            model_family=model_family,
            cutoff_start=_utc(cutoff_start, "cutoff_start"),
            cutoff_end=_utc(cutoff_end, "cutoff_end"),
            cutoff_cadence=cutoff_cadence,
            universe_policy=universe_policy,
            benchmark_code=benchmark_code,
            return_horizons_json=list(return_horizons),
            ordered_symbols_json=list(ordered_symbols),
            symbol_set_checksum_sha256=symbol_set_checksum_sha256,
            inputs_json=inputs_json,
            status="planned",
            summary_json={},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_observation(
        self, run: BacktestRun, value: BacktestObservationWrite
    ) -> tuple[BacktestObservation, bool]:
        existing = self._session.scalar(
            select(BacktestObservation).where(
                BacktestObservation.backtest_run_id == run.id,
                BacktestObservation.security_id == value.security_id,
                BacktestObservation.knowledge_cutoff
                == _utc(value.knowledge_cutoff, "knowledge_cutoff"),
            )
        )
        if existing is not None:
            if _observation_projection(existing) != _observation_write_projection(value):
                raise BacktestIntegrityError("existing backtest observation conflicts")
            return existing, False
        record = BacktestObservation(
            backtest_run_id=run.id,
            company_id=value.company_id,
            security_id=value.security_id,
            score_snapshot_id=value.score_snapshot_id,
            symbol=value.symbol,
            knowledge_cutoff=_utc(value.knowledge_cutoff, "knowledge_cutoff"),
            observation_status=value.observation_status,
            selected_fiscal_year=value.selected_fiscal_year,
            selected_fiscal_quarter=value.selected_fiscal_quarter,
            selected_filing_scope=value.selected_filing_scope,
            selected_period_end=value.selected_period_end,
            snapshot_status=value.snapshot_status,
            snapshot_fingerprint_sha256=value.snapshot_fingerprint_sha256,
            research_state_projection_version=value.research_state_projection_version,
            research_state_sha256=value.research_state_sha256,
            research_state_changed=value.research_state_changed,
            detail_json=value.detail_json,
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_outcome(
        self, observation: BacktestObservation, value: BacktestOutcomeWrite
    ) -> tuple[BacktestOutcome, bool]:
        existing = self._session.scalar(
            select(BacktestOutcome).where(
                BacktestOutcome.backtest_observation_id == observation.id,
                BacktestOutcome.horizon_observations == value.horizon_observations,
            )
        )
        if existing is not None:
            if _outcome_projection(existing) != _outcome_write_projection(value):
                raise BacktestIntegrityError("existing backtest outcome conflicts")
            return existing, False
        record = BacktestOutcome(
            backtest_observation_id=observation.id,
            horizon_observations=value.horizon_observations,
            outcome_status=value.outcome_status,
            entry_trading_date=value.entry_trading_date,
            exit_trading_date=value.exit_trading_date,
            entry_adjusted_close=value.entry_adjusted_close,
            exit_adjusted_close=value.exit_adjusted_close,
            security_return=value.security_return,
            benchmark_return=value.benchmark_return,
            excess_return=value.excess_return,
            outcome_data_cutoff=_utc(value.outcome_data_cutoff, "outcome_data_cutoff"),
            provenance_json=value.provenance_json,
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def freeze_outcome_cutoff(self, run: BacktestRun, cutoff: datetime) -> None:
        value = _utc(cutoff, "outcome_data_cutoff")
        existing = _utc_or_none(run.outcome_data_cutoff)
        if existing is not None and existing != value:
            raise BacktestIntegrityError("outcome data cutoff is already frozen")
        run.outcome_data_cutoff = value
        self._session.flush()

    def set_status(
        self,
        run: BacktestRun,
        status: str,
        *,
        summary: dict[str, object] | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        transitions = {
            "planned": {"snapshots_built", "failed"},
            "snapshots_built": {"outcomes_built", "failed"},
            "outcomes_built": {"completed", "failed"},
            "completed": set(),
            "failed": set(),
        }
        if status != run.status and status not in transitions[run.status]:
            raise BacktestIntegrityError(f"invalid backtest transition {run.status} -> {status}")
        run.status = status
        if summary is not None:
            run.summary_json = summary
        if completed_at is not None:
            run.completed_at = _utc(completed_at, "completed_at")
        self._session.flush()


def _observation_projection(value: BacktestObservation) -> tuple[object, ...]:
    return (
        value.company_id,
        value.score_snapshot_id,
        value.symbol,
        value.observation_status,
        value.selected_fiscal_year,
        value.selected_fiscal_quarter,
        value.selected_filing_scope,
        value.selected_period_end,
        value.snapshot_status,
        value.snapshot_fingerprint_sha256,
        value.research_state_projection_version,
        value.research_state_sha256,
        value.research_state_changed,
        value.detail_json,
    )


def _observation_write_projection(value: BacktestObservationWrite) -> tuple[object, ...]:
    return (
        value.company_id,
        value.score_snapshot_id,
        value.symbol,
        value.observation_status,
        value.selected_fiscal_year,
        value.selected_fiscal_quarter,
        value.selected_filing_scope,
        value.selected_period_end,
        value.snapshot_status,
        value.snapshot_fingerprint_sha256,
        value.research_state_projection_version,
        value.research_state_sha256,
        value.research_state_changed,
        value.detail_json,
    )


def _outcome_projection(value: BacktestOutcome) -> tuple[object, ...]:
    return (
        value.outcome_status,
        value.entry_trading_date,
        value.exit_trading_date,
        value.entry_adjusted_close,
        value.exit_adjusted_close,
        value.security_return,
        value.benchmark_return,
        value.excess_return,
        _utc(value.outcome_data_cutoff, "outcome_data_cutoff"),
        value.provenance_json,
    )


def _outcome_write_projection(value: BacktestOutcomeWrite) -> tuple[object, ...]:
    return (
        value.outcome_status,
        value.entry_trading_date,
        value.exit_trading_date,
        value.entry_adjusted_close,
        value.exit_adjusted_close,
        value.security_return,
        value.benchmark_return,
        value.excess_return,
        _utc(value.outcome_data_cutoff, "outcome_data_cutoff"),
        value.provenance_json,
    )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value, "datetime")
