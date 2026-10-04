"""Append-only persistence for Production Q cohorts and outcome labels."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import (
    HistoricalUniverseMemberRecord,
    HistoricalUniverseRun,
    MultibaggerLabelRun,
    MultibaggerOutcomeLabel,
)


class HistoricalEvaluationIntegrityError(RuntimeError):
    """Raised when an immutable Q identity conflicts with persisted state."""


@dataclass(frozen=True, slots=True)
class HistoricalUniverseMemberWrite:
    historical_symbol: str
    historical_isin: str | None
    exchange: str
    series: str
    membership_date: date
    company_id: UUID | None
    security_id: UUID | None
    exchange_listing_id: UUID | None
    membership_status: str
    reason_code: str | None
    source_record_id: UUID
    member_fingerprint_sha256: str
    provenance_json: dict[str, object]


@dataclass(frozen=True, slots=True)
class MultibaggerLabelWrite:
    backtest_observation_id: UUID
    contract_code: str
    classification: str
    unavailable_reason: str | None
    threshold_multiple: Decimal
    horizon_calendar_years: int
    entry_trading_date: date | None
    adjusted_entry_close: Decimal | None
    horizon_end_date: date | None
    first_threshold_hit_trading_date: date | None
    trading_observations_available: int
    peak_adjusted_close: Decimal | None
    peak_price_multiple: Decimal | None
    maximum_forward_price_return: Decimal | None
    endpoint_adjusted_close: Decimal | None
    endpoint_return: Decimal | None
    calendar_days_to_threshold: int | None
    trading_observations_to_threshold: int | None
    maximum_drawdown: Decimal | None
    listing_valid_to: date | None
    label_matured_at: datetime | None
    outcome_data_cutoff: datetime
    provenance_json: dict[str, object]
    label_fingerprint_sha256: str


class HistoricalEvaluationRepository:
    """Create/reuse immutable Q runs and facts, failing closed on conflicts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_universe_run(self, run_id: UUID) -> HistoricalUniverseRun | None:
        return self._session.scalar(
            select(HistoricalUniverseRun)
            .options(selectinload(HistoricalUniverseRun.members))
            .where(HistoricalUniverseRun.id == run_id)
        )

    def create_universe_run(
        self,
        *,
        run_key_sha256: str,
        universe_policy_code: str,
        universe_policy_checksum_sha256: str,
        cutoff: datetime,
        source_provider_dataset_id: UUID,
        source_state_checksum_sha256: str,
        inputs_json: dict[str, object],
    ) -> tuple[HistoricalUniverseRun, bool]:
        existing = self._session.scalar(
            select(HistoricalUniverseRun).where(
                HistoricalUniverseRun.run_key_sha256 == run_key_sha256
            )
        )
        if existing is not None:
            projection = (
                existing.universe_policy_code,
                existing.universe_policy_checksum_sha256,
                _stored_utc(existing.cutoff),
                existing.source_provider_dataset_id,
                existing.source_state_checksum_sha256,
                existing.inputs_json,
            )
            expected = (
                universe_policy_code,
                universe_policy_checksum_sha256,
                _utc(cutoff),
                source_provider_dataset_id,
                source_state_checksum_sha256,
                inputs_json,
            )
            if projection != expected:
                raise HistoricalEvaluationIntegrityError(
                    "existing historical universe run conflicts"
                )
            return existing, False
        record = HistoricalUniverseRun(
            run_key_sha256=run_key_sha256,
            universe_policy_code=universe_policy_code,
            universe_policy_checksum_sha256=universe_policy_checksum_sha256,
            cutoff=_utc(cutoff),
            source_provider_dataset_id=source_provider_dataset_id,
            source_state_checksum_sha256=source_state_checksum_sha256,
            member_count=0,
            status="planned",
            inputs_json=inputs_json,
            summary_json={},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_universe_member(
        self, run: HistoricalUniverseRun, value: HistoricalUniverseMemberWrite
    ) -> tuple[HistoricalUniverseMemberRecord, bool]:
        existing = self._session.scalar(
            select(HistoricalUniverseMemberRecord).where(
                HistoricalUniverseMemberRecord.historical_universe_run_id == run.id,
                HistoricalUniverseMemberRecord.member_fingerprint_sha256
                == value.member_fingerprint_sha256,
            )
        )
        if existing is not None:
            if _universe_member_projection(existing) != _universe_member_write_projection(value):
                raise HistoricalEvaluationIntegrityError(
                    "existing historical universe member conflicts"
                )
            return existing, False
        record = HistoricalUniverseMemberRecord(
            historical_universe_run_id=run.id,
            historical_symbol=value.historical_symbol,
            historical_isin=value.historical_isin,
            exchange=value.exchange,
            series=value.series,
            membership_date=value.membership_date,
            company_id=value.company_id,
            security_id=value.security_id,
            exchange_listing_id=value.exchange_listing_id,
            membership_status=value.membership_status,
            reason_code=value.reason_code,
            source_record_id=value.source_record_id,
            member_fingerprint_sha256=value.member_fingerprint_sha256,
            provenance_json=value.provenance_json,
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def complete_universe_run(
        self,
        run: HistoricalUniverseRun,
        *,
        member_count: int,
        summary_json: dict[str, object],
        completed_at: datetime,
    ) -> None:
        if run.status == "completed":
            if run.member_count != member_count or run.summary_json != summary_json:
                raise HistoricalEvaluationIntegrityError(
                    "completed historical universe run conflicts"
                )
            return
        if run.status != "planned":
            raise HistoricalEvaluationIntegrityError("historical universe run is not completable")
        run.member_count = member_count
        run.summary_json = summary_json
        run.status = "completed"
        run.completed_at = _utc(completed_at)
        self._session.flush()

    def get_label_run(self, run_id: UUID) -> MultibaggerLabelRun | None:
        return self._session.scalar(
            select(MultibaggerLabelRun)
            .options(selectinload(MultibaggerLabelRun.labels))
            .where(MultibaggerLabelRun.id == run_id)
        )

    def create_label_run(
        self,
        *,
        run_key_sha256: str,
        label_policy_code: str,
        label_policy_checksum_sha256: str,
        source_backtest_run_id: UUID,
        source_backtest_run_key_sha256: str,
        outcome_data_cutoff: datetime,
        market_provider_dataset_id: UUID,
        corporate_action_provider_dataset_id: UUID,
        source_state_checksum_sha256: str,
        ordered_contracts_json: list[object],
        algorithm_versions_json: dict[str, object],
        inputs_json: dict[str, object],
    ) -> tuple[MultibaggerLabelRun, bool]:
        existing = self._session.scalar(
            select(MultibaggerLabelRun).where(MultibaggerLabelRun.run_key_sha256 == run_key_sha256)
        )
        if existing is not None:
            projection = (
                existing.label_policy_code,
                existing.label_policy_checksum_sha256,
                existing.source_backtest_run_id,
                existing.source_backtest_run_key_sha256,
                _stored_utc(existing.outcome_data_cutoff),
                existing.market_provider_dataset_id,
                existing.corporate_action_provider_dataset_id,
                existing.source_state_checksum_sha256,
                existing.ordered_contracts_json,
                existing.algorithm_versions_json,
                existing.inputs_json,
            )
            expected = (
                label_policy_code,
                label_policy_checksum_sha256,
                source_backtest_run_id,
                source_backtest_run_key_sha256,
                _utc(outcome_data_cutoff),
                market_provider_dataset_id,
                corporate_action_provider_dataset_id,
                source_state_checksum_sha256,
                ordered_contracts_json,
                algorithm_versions_json,
                inputs_json,
            )
            if projection != expected:
                raise HistoricalEvaluationIntegrityError("existing label run conflicts")
            return existing, False
        record = MultibaggerLabelRun(
            run_key_sha256=run_key_sha256,
            label_policy_code=label_policy_code,
            label_policy_checksum_sha256=label_policy_checksum_sha256,
            source_backtest_run_id=source_backtest_run_id,
            source_backtest_run_key_sha256=source_backtest_run_key_sha256,
            outcome_data_cutoff=_utc(outcome_data_cutoff),
            market_provider_dataset_id=market_provider_dataset_id,
            corporate_action_provider_dataset_id=corporate_action_provider_dataset_id,
            source_state_checksum_sha256=source_state_checksum_sha256,
            ordered_contracts_json=ordered_contracts_json,
            algorithm_versions_json=algorithm_versions_json,
            inputs_json=inputs_json,
            status="planned",
            summary_json={},
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_label(
        self, run: MultibaggerLabelRun, value: MultibaggerLabelWrite
    ) -> tuple[MultibaggerOutcomeLabel, bool]:
        existing = self._session.scalar(
            select(MultibaggerOutcomeLabel).where(
                MultibaggerOutcomeLabel.multibagger_label_run_id == run.id,
                MultibaggerOutcomeLabel.backtest_observation_id == value.backtest_observation_id,
                MultibaggerOutcomeLabel.contract_code == value.contract_code,
            )
        )
        if existing is not None:
            if _label_projection(existing) != _label_write_projection(value):
                raise HistoricalEvaluationIntegrityError("existing multibagger label conflicts")
            return existing, False
        record = MultibaggerOutcomeLabel(
            multibagger_label_run_id=run.id,
            **asdict(value),
        )
        self._session.add(record)
        self._session.flush()
        return record, True

    def complete_label_run(
        self,
        run: MultibaggerLabelRun,
        *,
        summary_json: dict[str, object],
        completed_at: datetime,
    ) -> None:
        if run.status == "completed":
            if run.summary_json != summary_json:
                raise HistoricalEvaluationIntegrityError("completed label run conflicts")
            return
        if run.status != "planned":
            raise HistoricalEvaluationIntegrityError("multibagger label run is not completable")
        run.summary_json = summary_json
        run.status = "completed"
        run.completed_at = _utc(completed_at)
        self._session.flush()


def _universe_member_projection(value: HistoricalUniverseMemberRecord) -> tuple[object, ...]:
    return (
        value.historical_symbol,
        value.historical_isin,
        value.exchange,
        value.series,
        value.membership_date,
        value.company_id,
        value.security_id,
        value.exchange_listing_id,
        value.membership_status,
        value.reason_code,
        value.source_record_id,
        value.provenance_json,
    )


def _universe_member_write_projection(value: HistoricalUniverseMemberWrite) -> tuple[object, ...]:
    return (
        value.historical_symbol,
        value.historical_isin,
        value.exchange,
        value.series,
        value.membership_date,
        value.company_id,
        value.security_id,
        value.exchange_listing_id,
        value.membership_status,
        value.reason_code,
        value.source_record_id,
        value.provenance_json,
    )


def _label_projection(value: MultibaggerOutcomeLabel) -> tuple[object, ...]:
    return (
        value.classification,
        value.unavailable_reason,
        value.threshold_multiple,
        value.horizon_calendar_years,
        value.entry_trading_date,
        value.adjusted_entry_close,
        value.horizon_end_date,
        value.first_threshold_hit_trading_date,
        value.trading_observations_available,
        value.peak_adjusted_close,
        value.peak_price_multiple,
        value.maximum_forward_price_return,
        value.endpoint_adjusted_close,
        value.endpoint_return,
        value.calendar_days_to_threshold,
        value.trading_observations_to_threshold,
        value.maximum_drawdown,
        value.listing_valid_to,
        _utc_or_none(value.label_matured_at),
        _stored_utc(value.outcome_data_cutoff),
        value.provenance_json,
        value.label_fingerprint_sha256,
    )


def _label_write_projection(value: MultibaggerLabelWrite) -> tuple[object, ...]:
    return (
        value.classification,
        value.unavailable_reason,
        value.threshold_multiple,
        value.horizon_calendar_years,
        value.entry_trading_date,
        value.adjusted_entry_close,
        value.horizon_end_date,
        value.first_threshold_hit_trading_date,
        value.trading_observations_available,
        value.peak_adjusted_close,
        value.peak_price_multiple,
        value.maximum_forward_price_return,
        value.endpoint_adjusted_close,
        value.endpoint_return,
        value.calendar_days_to_threshold,
        value.trading_observations_to_threshold,
        value.maximum_drawdown,
        value.listing_valid_to,
        _utc_or_none(value.label_matured_at),
        _utc(value.outcome_data_cutoff),
        value.provenance_json,
        value.label_fingerprint_sha256,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _stored_utc(value)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
