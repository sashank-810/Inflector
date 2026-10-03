"""Append-only persistence for deterministic current opportunity discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import (
    OpportunityDiscoveryItem,
    OpportunityDiscoveryRun,
    ScoreSnapshot,
)


class OpportunityDiscoveryIntegrityError(RuntimeError):
    """Raised when immutable discovery state conflicts with persisted state."""


@dataclass(frozen=True, slots=True)
class OpportunityDiscoveryItemWrite:
    company_id: UUID | None
    security_id: UUID | None
    score_snapshot_id: UUID | None
    symbol: str
    rankable: bool
    unranked_reason: str | None
    score_rank: int | None
    display_order: int | None
    snapshot_age_days: int | None
    freshness_state: str
    selected_snapshot_fingerprint_sha256: str | None
    detail_json: dict[str, object]


class OpportunityDiscoveryRepository:
    """Create immutable runs/items and allow only planned-to-completed transition."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_run(self, run_id: UUID) -> OpportunityDiscoveryRun | None:
        return self._session.scalar(
            select(OpportunityDiscoveryRun)
            .options(
                selectinload(OpportunityDiscoveryRun.items)
                .selectinload(OpportunityDiscoveryItem.score_snapshot)
                .selectinload(ScoreSnapshot.components)
            )
            .where(OpportunityDiscoveryRun.id == run_id)
        )

    def get_by_key(self, run_key_sha256: str) -> OpportunityDiscoveryRun | None:
        return self._session.scalar(
            select(OpportunityDiscoveryRun).where(
                OpportunityDiscoveryRun.run_key_sha256 == run_key_sha256
            )
        )

    def create_run(
        self,
        *,
        run_key_sha256: str,
        discovery_policy_code: str,
        discovery_policy_checksum_sha256: str,
        scoring_configuration_id: UUID,
        scoring_configuration_checksum_sha256: str,
        research_profile_code: str,
        research_profile_checksum_sha256: str,
        financial_primitive_policy_checksum_sha256: str,
        financial_endpoint_policy_checksum_sha256: str,
        model_family: str,
        discovery_cutoff: datetime,
        universe_mode: str,
        ordered_symbols: tuple[str, ...],
        symbol_set_checksum_sha256: str,
        selected_snapshot_set_checksum_sha256: str,
        snapshot_selection_version: str,
        ranking_version: str,
        inputs_json: dict[str, object],
    ) -> tuple[OpportunityDiscoveryRun, bool]:
        existing = self.get_by_key(run_key_sha256)
        if existing is not None:
            expected = (
                discovery_policy_code,
                discovery_policy_checksum_sha256,
                scoring_configuration_id,
                scoring_configuration_checksum_sha256,
                research_profile_code,
                research_profile_checksum_sha256,
                financial_primitive_policy_checksum_sha256,
                financial_endpoint_policy_checksum_sha256,
                model_family,
                _utc(discovery_cutoff, "discovery_cutoff"),
                universe_mode,
                list(ordered_symbols),
                symbol_set_checksum_sha256,
                selected_snapshot_set_checksum_sha256,
                snapshot_selection_version,
                ranking_version,
                inputs_json,
            )
            if _run_projection(existing) != expected:
                raise OpportunityDiscoveryIntegrityError(
                    "existing opportunity discovery run key conflicts"
                )
            return existing, False
        record = OpportunityDiscoveryRun(
            run_key_sha256=run_key_sha256,
            discovery_policy_code=discovery_policy_code,
            discovery_policy_checksum_sha256=discovery_policy_checksum_sha256,
            scoring_configuration_id=scoring_configuration_id,
            scoring_configuration_checksum_sha256=scoring_configuration_checksum_sha256,
            research_profile_code=research_profile_code,
            research_profile_checksum_sha256=research_profile_checksum_sha256,
            financial_primitive_policy_checksum_sha256=(
                financial_primitive_policy_checksum_sha256
            ),
            financial_endpoint_policy_checksum_sha256=(
                financial_endpoint_policy_checksum_sha256
            ),
            model_family=model_family,
            discovery_cutoff=_utc(discovery_cutoff, "discovery_cutoff"),
            universe_mode=universe_mode,
            ordered_symbols_json=list(ordered_symbols),
            symbol_set_checksum_sha256=symbol_set_checksum_sha256,
            selected_snapshot_set_checksum_sha256=selected_snapshot_set_checksum_sha256,
            snapshot_selection_version=snapshot_selection_version,
            ranking_version=ranking_version,
            inputs_json=inputs_json,
            status="planned",
            summary_json={},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_item(
        self,
        run: OpportunityDiscoveryRun,
        value: OpportunityDiscoveryItemWrite,
    ) -> tuple[OpportunityDiscoveryItem, bool]:
        existing = self._session.scalar(
            select(OpportunityDiscoveryItem).where(
                OpportunityDiscoveryItem.opportunity_discovery_run_id == run.id,
                OpportunityDiscoveryItem.symbol == value.symbol,
            )
        )
        if existing is not None:
            if _item_projection(existing) != _write_projection(value):
                raise OpportunityDiscoveryIntegrityError(
                    "existing opportunity discovery item conflicts"
                )
            return existing, False
        record = OpportunityDiscoveryItem(
            opportunity_discovery_run_id=run.id,
            company_id=value.company_id,
            security_id=value.security_id,
            score_snapshot_id=value.score_snapshot_id,
            symbol=value.symbol,
            rankable=value.rankable,
            unranked_reason=value.unranked_reason,
            score_rank=value.score_rank,
            display_order=value.display_order,
            snapshot_age_days=value.snapshot_age_days,
            freshness_state=value.freshness_state,
            selected_snapshot_fingerprint_sha256=(
                value.selected_snapshot_fingerprint_sha256
            ),
            detail_json=value.detail_json,
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def complete(
        self, run: OpportunityDiscoveryRun, *, summary: dict[str, object]
    ) -> OpportunityDiscoveryRun:
        if run.status == "completed":
            if run.summary_json != summary:
                raise OpportunityDiscoveryIntegrityError(
                    "completed opportunity discovery summary conflicts"
                )
            return run
        if run.status != "planned":
            raise OpportunityDiscoveryIntegrityError(
                f"invalid opportunity discovery transition {run.status} -> completed"
            )
        self._session.execute(
            update(OpportunityDiscoveryRun)
            .where(OpportunityDiscoveryRun.id == run.id)
            .values(status="completed", summary_json=summary, completed_at=func.now())
        )
        self._session.flush()
        self._session.refresh(run)
        return run


def _item_projection(value: OpportunityDiscoveryItem) -> tuple[object, ...]:
    return (
        value.company_id,
        value.security_id,
        value.score_snapshot_id,
        value.symbol,
        value.rankable,
        value.unranked_reason,
        value.score_rank,
        value.display_order,
        value.snapshot_age_days,
        value.freshness_state,
        value.selected_snapshot_fingerprint_sha256,
        value.detail_json,
    )


def _run_projection(value: OpportunityDiscoveryRun) -> tuple[object, ...]:
    return (
        value.discovery_policy_code,
        value.discovery_policy_checksum_sha256,
        value.scoring_configuration_id,
        value.scoring_configuration_checksum_sha256,
        value.research_profile_code,
        value.research_profile_checksum_sha256,
        value.financial_primitive_policy_checksum_sha256,
        value.financial_endpoint_policy_checksum_sha256,
        value.model_family,
        _stored_utc(value.discovery_cutoff),
        value.universe_mode,
        value.ordered_symbols_json,
        value.symbol_set_checksum_sha256,
        value.selected_snapshot_set_checksum_sha256,
        value.snapshot_selection_version,
        value.ranking_version,
        value.inputs_json,
    )


def _write_projection(value: OpportunityDiscoveryItemWrite) -> tuple[object, ...]:
    return (
        value.company_id,
        value.security_id,
        value.score_snapshot_id,
        value.symbol,
        value.rankable,
        value.unranked_reason,
        value.score_rank,
        value.display_order,
        value.snapshot_age_days,
        value.freshness_state,
        value.selected_snapshot_fingerprint_sha256,
        value.detail_json,
    )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
