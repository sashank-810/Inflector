"""Append-only persistence boundary for Production R evaluation records."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from inflector_database.models import (
    MultibaggerConfusionRow,
    MultibaggerEvaluationCohort,
    MultibaggerEvaluationRun,
    MultibaggerPredictionRow,
)


class MultibaggerEvaluationIntegrityError(RuntimeError):
    """Raised when an immutable evaluation identity is reused inconsistently."""


class MultibaggerEvaluationRepository:
    """Narrow create/reuse and retrieval boundary; it never rewrites completed data."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_run(self, run_id: UUID) -> MultibaggerEvaluationRun | None:
        return self._session.scalar(
            select(MultibaggerEvaluationRun)
            .options(
                selectinload(MultibaggerEvaluationRun.cohorts)
                .selectinload(MultibaggerEvaluationCohort.predictions)
                .selectinload(MultibaggerPredictionRow.confusions)
            )
            .where(MultibaggerEvaluationRun.id == run_id)
        )

    def get_by_key(self, key: str) -> MultibaggerEvaluationRun | None:
        return self._session.scalar(
            select(MultibaggerEvaluationRun).where(MultibaggerEvaluationRun.run_key_sha256 == key)
        )

    def create_or_reuse_run(
        self, *, run_key_sha256: str, projection: dict[str, object]
    ) -> tuple[MultibaggerEvaluationRun, bool]:
        existing = self.get_by_key(run_key_sha256)
        if existing is not None:
            if _run_projection(existing) != projection:
                raise MultibaggerEvaluationIntegrityError("evaluation run key conflicts")
            return existing, False
        record = MultibaggerEvaluationRun(run_key_sha256=run_key_sha256, **projection)
        self._session.add(record)
        self._session.flush()
        return record, True

    def add_cohort(
        self, run: MultibaggerEvaluationRun, projection: dict[str, object]
    ) -> MultibaggerEvaluationCohort:
        existing = self._session.scalar(
            select(MultibaggerEvaluationCohort).where(
                MultibaggerEvaluationCohort.multibagger_evaluation_run_id == run.id,
                MultibaggerEvaluationCohort.historical_universe_run_id
                == projection["historical_universe_run_id"],
            )
        )
        if existing is not None:
            if _cohort_projection(existing) != projection:
                raise MultibaggerEvaluationIntegrityError("evaluation cohort conflicts")
            return existing
        record = MultibaggerEvaluationCohort(
            multibagger_evaluation_run_id=run.id,
            **projection,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def add_prediction(
        self, cohort: MultibaggerEvaluationCohort, projection: dict[str, object]
    ) -> MultibaggerPredictionRow:
        existing = self._session.scalar(
            select(MultibaggerPredictionRow).where(
                MultibaggerPredictionRow.multibagger_evaluation_cohort_id == cohort.id,
                MultibaggerPredictionRow.security_id == projection["security_id"],
            )
        )
        if existing is not None:
            if _prediction_projection(existing) != projection:
                raise MultibaggerEvaluationIntegrityError("evaluation prediction conflicts")
            return existing
        record = MultibaggerPredictionRow(
            multibagger_evaluation_cohort_id=cohort.id,
            **projection,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def add_confusion(
        self, prediction: MultibaggerPredictionRow, projection: dict[str, object]
    ) -> MultibaggerConfusionRow:
        existing = self._session.scalar(
            select(MultibaggerConfusionRow).where(
                MultibaggerConfusionRow.multibagger_prediction_row_id == prediction.id,
                MultibaggerConfusionRow.outcome_contract_code
                == projection["outcome_contract_code"],
                MultibaggerConfusionRow.selection_contract_code
                == projection["selection_contract_code"],
                MultibaggerConfusionRow.evaluation_view == projection["evaluation_view"],
            )
        )
        if existing is not None:
            if _confusion_projection(existing) != projection:
                raise MultibaggerEvaluationIntegrityError("evaluation confusion row conflicts")
            return existing
        record = MultibaggerConfusionRow(multibagger_prediction_row_id=prediction.id, **projection)
        self._session.add(record)
        self._session.flush()
        return record

    def complete(
        self, run: MultibaggerEvaluationRun, *, summary: dict[str, object], completed_at: datetime
    ) -> None:
        if run.status == "completed":
            if run.summary_json != summary:
                raise MultibaggerEvaluationIntegrityError("completed evaluation summary conflicts")
            return
        if run.status != "planned":
            raise MultibaggerEvaluationIntegrityError("evaluation run cannot be completed")
        run.status = "completed"
        run.summary_json = summary
        run.completed_at = _utc(completed_at)
        self._session.flush()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _projection(value: object, fields: tuple[str, ...]) -> dict[str, object]:
    return {field: getattr(value, field) for field in fields}


def _run_projection(value: MultibaggerEvaluationRun) -> dict[str, object]:
    return _projection(value, _RUN_FIELDS)


def _cohort_projection(value: MultibaggerEvaluationCohort) -> dict[str, object]:
    return _projection(value, _COHORT_FIELDS)


def _prediction_projection(value: MultibaggerPredictionRow) -> dict[str, object]:
    return _projection(value, _PREDICTION_FIELDS)


def _confusion_projection(value: MultibaggerConfusionRow) -> dict[str, object]:
    return _projection(value, _CONFUSION_FIELDS)


_RUN_FIELDS = (
    "evaluation_policy_code",
    "evaluation_policy_checksum_sha256",
    "backtest_policy_checksum_sha256",
    "universe_policy_checksum_sha256",
    "outcome_policy_checksum_sha256",
    "research_profile_checksum_sha256",
    "model_family",
    "scoring_configuration_checksum_sha256",
    "ordered_bundle_checksums_json",
    "selection_contracts_json",
    "outcome_contracts_json",
    "algorithm_versions_json",
    "inputs_json",
    "status",
    "summary_json",
    "completed_at",
)
_COHORT_FIELDS = (
    "historical_universe_run_id",
    "historical_cutoff",
    "universe_run_key_sha256",
    "eligible_resolved_count",
    "unresolved_count",
    "ambiguous_count",
    "unsupported_count",
    "ordered_security_checksum_sha256",
    "backtest_run_ids_json",
    "label_run_ids_json",
    "outcome_data_cutoff",
    "bundle_checksum_sha256",
    "rankable_count",
    "unrankable_count",
    "calendar_integrity_status",
    "status",
    "summary_json",
)
_PREDICTION_FIELDS = (
    "backtest_observation_id",
    "company_id",
    "security_id",
    "historical_symbol",
    "score_snapshot_id",
    "snapshot_fingerprint_sha256",
    "snapshot_status",
    "rankable",
    "unranked_reason",
    "final_score",
    "dense_score_rank",
    "display_order",
    "available_component_codes_json",
    "prediction_fingerprint_sha256",
)
_CONFUSION_FIELDS = (
    "multibagger_outcome_label_id",
    "outcome_contract_code",
    "selection_contract_code",
    "evaluation_view",
    "selected",
    "actual_label",
    "confusion_class",
    "threshold_hit_trading_date",
    "endpoint_return",
    "peak_price_multiple",
    "maximum_drawdown",
    "label_provenance_json",
    "confusion_fingerprint_sha256",
)
