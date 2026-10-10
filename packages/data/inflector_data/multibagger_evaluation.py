"""Production R: frozen prediction evaluation over immutable J and Q records.

This module intentionally builds its prediction projection before reading labels.
It has no score calculation, ranking policy tuning, or current-state lookup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.historical_evaluation_policy import (
    EvaluationSelectionContract,
    MultibaggerEvaluationPolicy,
    canonical_json_sha256,
    load_multibagger_evaluation_policy,
)
from inflector_database.models import (
    BacktestObservation,
    BacktestRun,
    HistoricalUniverseMemberRecord,
    HistoricalUniverseRun,
    MultibaggerLabelRun,
    MultibaggerOutcomeLabel,
    ScoreSnapshot,
)
from inflector_database.multibagger_evaluation_repository import (
    MultibaggerEvaluationIntegrityError,
    MultibaggerEvaluationRepository,
)
from inflector_database.score_repository import ScoreSnapshotIntegrityError, ScoreSnapshotRepository

OUTCOME_CONTRACTS = ("MB_2X_2Y", "MB_3X_3Y", "MB_5X_5Y")
V5_SNAPSHOT_VERSION = "score_snapshot_v5"
Q_V1_CONTRACT_PROJECTION = (
    {"code": "MB_2X_2Y", "threshold_multiple": "2", "horizon_calendar_years": 2},
    {"code": "MB_3X_3Y", "threshold_multiple": "3", "horizon_calendar_years": 3},
    {"code": "MB_5X_5Y", "threshold_multiple": "5", "horizon_calendar_years": 5},
)


class EvaluationIntegrityError(RuntimeError):
    """A supplied historical bundle cannot safely represent a complete cohort."""


@dataclass(frozen=True, slots=True)
class EvaluationCohortBundle:
    historical_universe_run_id: UUID
    backtest_run_ids: tuple[UUID, ...]
    label_run_ids: tuple[UUID, ...]
    # This is an explicit readiness assertion for fixture/operational reporting;
    # it is never used to relax Q's label-completeness rules.
    calendar_integrity_status: str = "not_independently_verified"


@dataclass(frozen=True, slots=True)
class PredictionState:
    observation: BacktestObservation
    snapshot: ScoreSnapshot | None
    rankable: bool
    unranked_reason: str | None
    final_score: Decimal | None
    dense_score_rank: int | None
    display_order: int | None
    available_components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LabelRunMetadata:
    """Q run identity without the selectin-loaded outcome-label relationship."""

    id: UUID
    run_key_sha256: str
    label_policy_code: str
    label_policy_checksum_sha256: str
    source_backtest_run_id: UUID
    source_backtest_run_key_sha256: str
    outcome_data_cutoff: datetime
    ordered_contracts_json: tuple[object, ...]
    status: str


@dataclass(frozen=True, slots=True)
class MultibaggerEvaluationBuildResult:
    run_id: UUID
    run_key_sha256: str
    created_run: bool
    cohort_count: int
    prediction_count: int
    confusion_count: int
    summary: dict[str, object]


def load_evaluation_manifest(path: Path) -> tuple[EvaluationCohortBundle, ...]:
    """Parse transport-only IDs; all referenced records are validated independently."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("evaluation cohort manifest is not valid JSON") from error
    if not isinstance(value, list) or not value:
        raise ValueError("evaluation cohort manifest must be a non-empty array")
    result: list[EvaluationCohortBundle] = []
    for item in value:
        allowed_fields = (
            {"historical_universe_run_id", "backtest_run_ids", "label_run_ids"},
            {
                "historical_universe_run_id",
                "backtest_run_ids",
                "label_run_ids",
                "calendar_integrity_status",
            },
        )
        if not isinstance(item, dict) or set(item) not in allowed_fields:
            raise ValueError("evaluation cohort bundle is malformed")
        try:
            backtests = tuple(UUID(str(raw)) for raw in item["backtest_run_ids"])
            labels = tuple(UUID(str(raw)) for raw in item["label_run_ids"])
            bundle = EvaluationCohortBundle(
                historical_universe_run_id=UUID(str(item["historical_universe_run_id"])),
                backtest_run_ids=backtests,
                label_run_ids=labels,
                calendar_integrity_status=str(
                    item.get("calendar_integrity_status", "not_independently_verified")
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("evaluation cohort IDs are invalid") from error
        # The JSON manifest is only transport. It cannot attest to a real
        # benchmark/session archive; fixture_verified is direct-test only.
        if bundle.calendar_integrity_status != "not_independently_verified":
            raise ValueError("manifest cannot self-certify calendar integrity")
        _validate_bundle_shape(bundle)
        result.append(bundle)
    if len({item.historical_universe_run_id for item in result}) != len(result):
        raise ValueError("historical universe can appear in only one evaluation bundle")
    return tuple(sorted(result, key=lambda item: str(item.historical_universe_run_id)))


def build_multibagger_evaluation(
    session: Session,
    *,
    policy: MultibaggerEvaluationPolicy,
    bundles: tuple[EvaluationCohortBundle, ...],
    completed_at: datetime,
) -> MultibaggerEvaluationBuildResult:
    """Validate all frozen inputs, persist prediction state, then join Q labels."""

    completed = _utc(completed_at, "completed_at")
    if not bundles:
        raise ValueError("at least one evaluation bundle is required")
    prepared = tuple(_prepare_bundle(session, policy=policy, bundle=item) for item in bundles)
    _validate_cross_bundle_compatibility(prepared)
    prepared = tuple(
        sorted(
            prepared,
            key=lambda item: (_stored_utc(item.universe.cutoff), item.bundle_checksum),
        )
    )
    ordered_bundle_checksums = [item.bundle_checksum for item in prepared]
    first = prepared[0]
    run_inputs = {
        "ordered_bundle_checksums": ordered_bundle_checksums,
        "calendar_integrity_statuses": [item.bundle.calendar_integrity_status for item in prepared],
        "prediction_selection_checksums": [item.prediction_selection_checksum for item in prepared],
    }
    run_key = canonical_json_sha256(
        {
            "evaluation_policy_checksum_sha256": policy.checksum_sha256,
            "ordered_bundle_checksums": ordered_bundle_checksums,
            "selection_contracts": [item.code for item in policy.selection_contracts],
            "outcome_contracts": list(OUTCOME_CONTRACTS),
            "algorithm_version": policy.algorithm_version,
        }
    )
    repository = MultibaggerEvaluationRepository(session)
    existing = repository.get_by_key(run_key)
    if existing is not None:
        if existing.status != "completed":
            raise MultibaggerEvaluationIntegrityError("existing evaluation run is not completed")
        return MultibaggerEvaluationBuildResult(
            run_id=existing.id,
            run_key_sha256=existing.run_key_sha256,
            created_run=False,
            cohort_count=len(existing.cohorts),
            prediction_count=sum(len(item.predictions) for item in existing.cohorts),
            confusion_count=sum(
                len(prediction.confusions)
                for item in existing.cohorts
                for prediction in item.predictions
            ),
            summary=dict(existing.summary_json),
        )
    run, created = repository.create_or_reuse_run(
        run_key_sha256=run_key,
        projection={
            "evaluation_policy_code": policy.code,
            "evaluation_policy_checksum_sha256": policy.checksum_sha256,
            "backtest_policy_checksum_sha256": policy.source_backtest_policy_checksum_sha256,
            "universe_policy_checksum_sha256": policy.historical_universe_policy_checksum_sha256,
            "outcome_policy_checksum_sha256": policy.multibagger_outcome_policy_checksum_sha256,
            "research_profile_checksum_sha256": policy.research_profile_checksum_sha256,
            "model_family": first.model_family,
            "scoring_configuration_checksum_sha256": first.configuration_checksum,
            "ordered_bundle_checksums_json": ordered_bundle_checksums,
            "selection_contracts_json": [item.code for item in policy.selection_contracts],
            "outcome_contracts_json": list(OUTCOME_CONTRACTS),
            "algorithm_versions_json": {
                "prediction_integrity": policy.prediction_integrity_version,
                "ranking": "v5_final_score_desc_symbol_security_v1",
                "metrics": "decimal_confusion_metrics_v1",
                "calendar_integrity": policy.calendar_integrity_requirement,
            },
            "inputs_json": run_inputs,
            "status": "planned",
            "summary_json": {},
            "completed_at": None,
        },
    )
    prediction_count = 0
    confusion_count = 0
    # First persist the complete prediction side for every cohort. Labels are
    # intentionally absent from PredictionState and prediction fingerprints.
    persisted: list[tuple[_PreparedBundle, dict[UUID, Any]]] = []
    for item in prepared:
        cohort = repository.add_cohort(run, item.cohort_projection())
        rows: dict[UUID, Any] = {}
        for state in item.predictions:
            record = repository.add_prediction(cohort, _prediction_projection(state))
            rows[state.observation.id] = record
            prediction_count += 1
        persisted.append((item, rows))
    # Make the prediction-only projection durable inside this transaction before
    # issuing any SQL against future outcome rows. A later label validation
    # failure still rolls the transaction back atomically.
    session.flush()
    # Only after every prediction rank and every policy selection flag is frozen
    # may classification values enter this process.
    for item, prediction_rows in persisted:
        labels = _exact_label_union(
            session,
            label_runs=item.label_runs,
            backtests=item.backtests,
            cohort_observations={
                state.observation.security_id: state.observation for state in item.predictions
            },
            policy=policy,
        )
        for state in item.predictions:
            prediction = prediction_rows[state.observation.id]
            for outcome_contract in OUTCOME_CONTRACTS:
                label = labels[(state.observation.id, outcome_contract)]
                for selection in policy.selection_contracts:
                    selected = item.selections[(state.observation.id, selection.code)]
                    for view in policy.evaluation_views:
                        confusion = _confusion_class(
                            rankable=state.rankable,
                            selected=selected,
                            actual_label=label.classification,
                            view=view,
                        )
                        repository.add_confusion(
                            prediction,
                            _confusion_projection(
                                label=label,
                                outcome_contract=outcome_contract,
                                selection_contract=selection.code,
                                view=view,
                                selected=selected,
                                confusion_class=confusion,
                            ),
                        )
                        confusion_count += 1
    summary = _summary(session, run.id)
    repository.complete(run, summary=summary, completed_at=completed)
    session.commit()
    return MultibaggerEvaluationBuildResult(
        run_id=run.id,
        run_key_sha256=run_key,
        created_run=created,
        cohort_count=len(prepared),
        prediction_count=prediction_count,
        confusion_count=confusion_count,
        summary=summary,
    )


@dataclass(frozen=True, slots=True)
class _PreparedBundle:
    bundle: EvaluationCohortBundle
    universe: HistoricalUniverseRun
    backtests: tuple[BacktestRun, ...]
    label_runs: tuple[_LabelRunMetadata, ...]
    predictions: tuple[PredictionState, ...]
    selections: dict[tuple[UUID, str], bool]
    prediction_selection_checksum: str
    eligible_resolved_count: int
    unresolved_count: int
    ambiguous_count: int
    unsupported_count: int
    ordered_security_checksum: str
    bundle_checksum: str
    rankable_count: int
    unrankable_count: int
    outcome_data_cutoff: datetime
    model_family: str
    configuration_checksum: str

    def cohort_projection(self) -> dict[str, object]:
        return {
            "historical_universe_run_id": self.universe.id,
            "historical_cutoff": self.universe.cutoff,
            "universe_run_key_sha256": self.universe.run_key_sha256,
            "eligible_resolved_count": self.eligible_resolved_count,
            "unresolved_count": self.unresolved_count,
            "ambiguous_count": self.ambiguous_count,
            "unsupported_count": self.unsupported_count,
            "ordered_security_checksum_sha256": self.ordered_security_checksum,
            "backtest_run_ids_json": [str(item.id) for item in self.backtests],
            "label_run_ids_json": [str(item.id) for item in self.label_runs],
            "outcome_data_cutoff": self.outcome_data_cutoff,
            "bundle_checksum_sha256": self.bundle_checksum,
            "rankable_count": self.rankable_count,
            "unrankable_count": self.unrankable_count,
            "calendar_integrity_status": self.bundle.calendar_integrity_status,
            "status": "completed",
            "summary_json": {
                "selection_boundary_ties": _boundary_ties(self.predictions),
                "prediction_selection_checksum_sha256": self.prediction_selection_checksum,
                "calendar_integrity_requirement": "verified_benchmark_session_archive_v1",
            },
        }


def _prepare_bundle(
    session: Session, *, policy: MultibaggerEvaluationPolicy, bundle: EvaluationCohortBundle
) -> _PreparedBundle:
    _validate_bundle_shape(bundle)
    universe = session.get(HistoricalUniverseRun, bundle.historical_universe_run_id)
    if universe is None or universe.status != "completed":
        raise EvaluationIntegrityError("historical universe run must exist and be completed")
    if (
        universe.universe_policy_code != policy.historical_universe_policy_code
        or universe.universe_policy_checksum_sha256
        != policy.historical_universe_policy_checksum_sha256
    ):
        raise EvaluationIntegrityError("historical universe policy binding mismatch")
    members = tuple(
        session.scalars(
            select(HistoricalUniverseMemberRecord).where(
                HistoricalUniverseMemberRecord.historical_universe_run_id == universe.id
            )
        )
    )
    eligible = tuple(item for item in members if item.membership_status == "eligible")
    if any(item.security_id is None or item.company_id is None for item in eligible):
        raise EvaluationIntegrityError("eligible historical member has no resolved identity")
    security_ids = [item.security_id for item in eligible if item.security_id is not None]
    if len(set(security_ids)) != len(security_ids):
        raise EvaluationIntegrityError("historical universe has duplicate resolved security")
    ordered_security_ids = tuple(sorted(str(item) for item in security_ids))
    ordered_security_checksum = canonical_json_sha256(list(ordered_security_ids))
    backtests = tuple(
        sorted(
            (_backtest(session, item) for item in bundle.backtest_run_ids),
            key=lambda item: item.run_key_sha256,
        )
    )
    _validate_backtests(backtests, universe=universe, policy=policy)
    observations = _exact_observation_union(
        session,
        backtests=backtests,
        expected_security_ids=set(security_ids),
        cutoff=universe.cutoff,
    )
    label_runs, outcome_cutoff = _label_run_metadata(
        session,
        label_run_ids=bundle.label_run_ids,
        backtests=backtests,
        policy=policy,
    )
    predictions = _rank_predictions(session, observations=observations, backtests=backtests)
    rankable_count = sum(item.rankable for item in predictions)
    unrankable_count = len(predictions) - rankable_count
    selections = {
        (item.observation.id, contract.code): _selected(item, contract, rankable_count)
        for item in predictions
        for contract in policy.selection_contracts
    }
    prediction_selection_checksum = canonical_json_sha256(
        _jsonable(
            {
                "predictions": [_prediction_projection(item) for item in predictions],
                "selection_flags": [
                    {
                        "observation_id": str(item.observation.id),
                        "selection_contract": contract.code,
                        "selected": selections[(item.observation.id, contract.code)],
                    }
                    for item in predictions
                    for contract in policy.selection_contracts
                ],
            }
        )
    )
    bundle_checksum = canonical_json_sha256(
        {
            "universe_run_id": str(universe.id),
            "universe_run_key_sha256": universe.run_key_sha256,
            "cutoff": _stored_utc(universe.cutoff).isoformat(),
            "ordered_security_checksum": ordered_security_checksum,
            "backtest_run_ids": [str(item.id) for item in backtests],
            "backtest_run_keys": [item.run_key_sha256 for item in backtests],
            "label_run_ids": [str(item.id) for item in label_runs],
            "label_run_keys": [item.run_key_sha256 for item in label_runs],
            "outcome_data_cutoff": outcome_cutoff.isoformat(),
            "calendar_integrity_status": bundle.calendar_integrity_status,
            "prediction_selection_checksum_sha256": prediction_selection_checksum,
        }
    )
    counts = {item.membership_status: 0 for item in members}
    for member in members:
        counts[member.membership_status] = counts.get(member.membership_status, 0) + 1
    return _PreparedBundle(
        bundle=bundle,
        universe=universe,
        backtests=backtests,
        label_runs=label_runs,
        predictions=predictions,
        selections=selections,
        prediction_selection_checksum=prediction_selection_checksum,
        eligible_resolved_count=len(eligible),
        unresolved_count=counts.get("unresolved_identity", 0),
        ambiguous_count=counts.get("ambiguous_identity", 0),
        unsupported_count=counts.get("unsupported_security_type", 0),
        ordered_security_checksum=ordered_security_checksum,
        bundle_checksum=bundle_checksum,
        rankable_count=rankable_count,
        unrankable_count=unrankable_count,
        outcome_data_cutoff=outcome_cutoff,
        model_family=backtests[0].model_family,
        configuration_checksum=backtests[0].scoring_configuration_checksum_sha256,
    )


def _backtest(session: Session, run_id: UUID) -> BacktestRun:
    run = session.get(BacktestRun, run_id)
    if run is None:
        raise EvaluationIntegrityError("referenced backtest run is unavailable")
    return run


def _validate_backtests(
    runs: tuple[BacktestRun, ...],
    *,
    universe: HistoricalUniverseRun,
    policy: MultibaggerEvaluationPolicy,
) -> None:
    if not runs:
        raise EvaluationIntegrityError("cohort requires at least one backtest shard")
    first = runs[0]
    expected = (
        first.scoring_configuration_id,
        first.scoring_configuration_checksum_sha256,
        first.model_family,
        first.research_profile_code,
        first.research_profile_checksum_sha256,
    )
    for run in runs:
        if run.status != "completed":
            raise EvaluationIntegrityError("backtest shard is not completed")
        if (
            run.backtest_policy_code != policy.source_backtest_policy_code
            or run.backtest_policy_checksum_sha256 != policy.source_backtest_policy_checksum_sha256
        ):
            raise EvaluationIntegrityError("backtest policy binding mismatch")
        if (run.research_profile_code, run.research_profile_checksum_sha256) != (
            policy.research_profile_code,
            policy.research_profile_checksum_sha256,
        ):
            raise EvaluationIntegrityError("backtest research profile binding mismatch")
        if (
            run.scoring_configuration_id,
            run.scoring_configuration_checksum_sha256,
            run.model_family,
            run.research_profile_code,
            run.research_profile_checksum_sha256,
        ) != expected:
            raise EvaluationIntegrityError("backtest shards mix frozen model semantics")
        if _stored_utc(run.cutoff_start) > _stored_utc(universe.cutoff) or _stored_utc(
            run.cutoff_end
        ) < _stored_utc(universe.cutoff):
            raise EvaluationIntegrityError("backtest shard cannot contain the cohort cutoff")


def _exact_observation_union(
    session: Session,
    *,
    backtests: tuple[BacktestRun, ...],
    expected_security_ids: set[UUID],
    cutoff: datetime,
) -> dict[UUID, BacktestObservation]:
    all_values = tuple(
        session.scalars(
            select(BacktestObservation).where(
                BacktestObservation.backtest_run_id.in_([item.id for item in backtests])
            )
        )
    )
    cutoff_utc = _stored_utc(cutoff)
    # A reviewed J shard may hold more than one historical cutoff. A bundle is
    # deliberately scoped to exactly one cutoff rather than treating the other
    # frozen observations as errors or folding them into this cohort.
    values = tuple(item for item in all_values if _stored_utc(item.knowledge_cutoff) == cutoff_utc)
    result: dict[UUID, BacktestObservation] = {}
    for observation in values:
        if observation.security_id in result:
            raise EvaluationIntegrityError("duplicate security observation across backtest shards")
        if observation.security_id not in expected_security_ids:
            raise EvaluationIntegrityError("backtest shard includes a non-universe security")
        result[observation.security_id] = observation
    if set(result) != expected_security_ids:
        raise EvaluationIntegrityError(
            "backtest shards do not exactly recompose the resolved universe"
        )
    return result


def _label_run_metadata(
    session: Session,
    *,
    label_run_ids: tuple[UUID, ...],
    backtests: tuple[BacktestRun, ...],
    policy: MultibaggerEvaluationPolicy,
) -> tuple[tuple[_LabelRunMetadata, ...], datetime]:
    """Validate Q run metadata without materializing its selectin label relationship."""

    rows = tuple(
        session.execute(
            select(
                MultibaggerLabelRun.id,
                MultibaggerLabelRun.run_key_sha256,
                MultibaggerLabelRun.label_policy_code,
                MultibaggerLabelRun.label_policy_checksum_sha256,
                MultibaggerLabelRun.source_backtest_run_id,
                MultibaggerLabelRun.source_backtest_run_key_sha256,
                MultibaggerLabelRun.outcome_data_cutoff,
                MultibaggerLabelRun.ordered_contracts_json,
                MultibaggerLabelRun.status,
            ).where(MultibaggerLabelRun.id.in_(label_run_ids))
        )
    )
    if len(rows) != len(label_run_ids):
        raise EvaluationIntegrityError("referenced multibagger label run is unavailable")
    label_runs: tuple[_LabelRunMetadata, ...] = tuple(
        sorted(
            (
                _LabelRunMetadata(
                    id=row.id,
                    run_key_sha256=row.run_key_sha256,
                    label_policy_code=row.label_policy_code,
                    label_policy_checksum_sha256=row.label_policy_checksum_sha256,
                    source_backtest_run_id=row.source_backtest_run_id,
                    source_backtest_run_key_sha256=row.source_backtest_run_key_sha256,
                    outcome_data_cutoff=row.outcome_data_cutoff,
                    ordered_contracts_json=tuple(row.ordered_contracts_json),
                    status=row.status,
                )
                for row in rows
            ),
            key=lambda item: item.run_key_sha256,
        )
    )
    by_backtest: dict[UUID, _LabelRunMetadata] = {}
    cutoff: datetime | None = None
    for run in label_runs:
        if run.status != "completed":
            raise EvaluationIntegrityError("multibagger label run is not completed")
        if (
            run.label_policy_code != policy.multibagger_outcome_policy_code
            or run.label_policy_checksum_sha256 != policy.multibagger_outcome_policy_checksum_sha256
            or run.source_backtest_run_id not in {item.id for item in backtests}
        ):
            raise EvaluationIntegrityError("multibagger label run binding mismatch")
        source_backtest = next(item for item in backtests if item.id == run.source_backtest_run_id)
        if run.source_backtest_run_key_sha256 != source_backtest.run_key_sha256:
            raise EvaluationIntegrityError("multibagger label run key binding mismatch")
        if run.ordered_contracts_json != Q_V1_CONTRACT_PROJECTION:
            raise EvaluationIntegrityError("multibagger label run has unsupported contract set")
        if run.source_backtest_run_id in by_backtest:
            raise EvaluationIntegrityError("backtest run has overlapping multibagger label runs")
        by_backtest[run.source_backtest_run_id] = run
        current_cutoff = _stored_utc(run.outcome_data_cutoff)
        if cutoff is None:
            cutoff = current_cutoff
        elif cutoff != current_cutoff:
            raise EvaluationIntegrityError("label runs mix outcome data cutoffs")
    if set(by_backtest) != {item.id for item in backtests}:
        raise EvaluationIntegrityError("every backtest shard requires exactly one label run")
    if cutoff is None:
        raise EvaluationIntegrityError("label runs are absent")
    return label_runs, cutoff


def _exact_label_union(
    session: Session,
    *,
    label_runs: tuple[_LabelRunMetadata, ...],
    backtests: tuple[BacktestRun, ...],
    cohort_observations: dict[UUID, BacktestObservation],
    policy: MultibaggerEvaluationPolicy,
) -> dict[tuple[UUID, str], MultibaggerOutcomeLabel]:
    """Validate whole Q runs before narrowing to the requested historical cutoff."""

    # Metadata was checked before prediction construction.  The first access to
    # actual Q classifications occurs only from this function after ranking and
    # selection state has been frozen and persisted.
    _, expected_cutoff = _label_run_metadata(
        session,
        label_run_ids=tuple(item.id for item in label_runs),
        backtests=backtests,
        policy=policy,
    )
    labels_for_cohort: dict[tuple[UUID, str], MultibaggerOutcomeLabel] = {}
    for run in label_runs:
        source_observations = tuple(
            session.scalars(
                select(BacktestObservation).where(
                    BacktestObservation.backtest_run_id == run.source_backtest_run_id
                )
            )
        )
        expected = {
            (item.id, code) for item in source_observations for code in OUTCOME_CONTRACTS
        }
        rows = tuple(
            session.scalars(
                select(MultibaggerOutcomeLabel).where(
                    MultibaggerOutcomeLabel.multibagger_label_run_id == run.id
                )
            )
        )
        seen: set[tuple[UUID, str]] = set()
        for label in rows:
            key = (label.backtest_observation_id, label.contract_code)
            if key in seen:
                raise EvaluationIntegrityError("duplicate multibagger label")
            seen.add(key)
            if key not in expected:
                raise EvaluationIntegrityError(
                    "label run contains a foreign observation or unsupported contract"
                )
            if (
                label.threshold_multiple != _contract_threshold(label.contract_code)
                or label.horizon_calendar_years != _contract_horizon(label.contract_code)
            ):
                raise EvaluationIntegrityError(
                    "label contract threshold or horizon conflicts with Q V1"
                )
            if _stored_utc(label.outcome_data_cutoff) != expected_cutoff:
                raise EvaluationIntegrityError("label outcome cutoff conflicts with label run")
            if label.backtest_observation_id in {item.id for item in cohort_observations.values()}:
                labels_for_cohort[key] = label
        if seen != expected:
            raise EvaluationIntegrityError(
                "label run does not contain exactly one label per source observation contract"
            )
    cohort_expected = {
        (item.id, code) for item in cohort_observations.values() for code in OUTCOME_CONTRACTS
    }
    if set(labels_for_cohort) != cohort_expected:
        raise EvaluationIntegrityError(
            "label runs do not exactly cover the evaluation observations"
        )
    return labels_for_cohort


def _contract_threshold(code: str) -> Decimal:
    return Decimal(
        next(
            item["threshold_multiple"] for item in Q_V1_CONTRACT_PROJECTION if item["code"] == code
        )
    )


def _contract_horizon(code: str) -> int:
    return int(
        next(
            item["horizon_calendar_years"]
            for item in Q_V1_CONTRACT_PROJECTION
            if item["code"] == code
        )
    )


def _rank_predictions(
    session: Session,
    *,
    observations: dict[UUID, BacktestObservation],
    backtests: tuple[BacktestRun, ...],
) -> tuple[PredictionState, ...]:
    run_by_id = {item.id: item for item in backtests}
    provisional = [
        _prediction_from_observation(session, observation, run_by_id[observation.backtest_run_id])
        for observation in observations.values()
    ]
    rankable = sorted(
        (item for item in provisional if item.rankable),
        key=lambda item: (
            -(item.final_score if item.final_score is not None else Decimal("0")),
            item.observation.symbol,
            str(item.observation.security_id),
        ),
    )
    ranked: dict[UUID, PredictionState] = {}
    previous_score: Decimal | None = None
    dense_rank = 0
    for ordinal, item in enumerate(rankable, start=1):
        if previous_score != item.final_score:
            dense_rank += 1
            previous_score = item.final_score
        ranked[item.observation.security_id] = PredictionState(
            observation=item.observation,
            snapshot=item.snapshot,
            rankable=True,
            unranked_reason=None,
            final_score=item.final_score,
            dense_score_rank=dense_rank,
            display_order=ordinal,
            available_components=item.available_components,
        )
    for item in provisional:
        if not item.rankable:
            ranked[item.observation.security_id] = item
    return tuple(sorted(ranked.values(), key=lambda item: str(item.observation.security_id)))


def _prediction_from_observation(
    session: Session, observation: BacktestObservation, run: BacktestRun
) -> PredictionState:
    if observation.score_snapshot_id is None:
        if (
            observation.snapshot_fingerprint_sha256 is not None
            or observation.snapshot_status is not None
            or observation.observation_status not in {"issuer_unavailable", "research_failed"}
        ):
            raise EvaluationIntegrityError(
                "no-snapshot backtest observation has contradictory persisted state"
            )
        return PredictionState(
            observation, None, False, _unranked_reason(observation), None, None, None, ()
        )
    if (
        observation.observation_status != "snapshot_frozen"
        or observation.snapshot_fingerprint_sha256 is None
        or observation.snapshot_status is None
    ):
        raise EvaluationIntegrityError(
            "snapshot backtest observation has contradictory persisted state"
        )
    snapshot = session.get(ScoreSnapshot, observation.score_snapshot_id)
    if snapshot is None:
        raise EvaluationIntegrityError("backtest observation references a missing score snapshot")
    try:
        ScoreSnapshotRepository(session).validate_persisted_snapshot(snapshot)
    except ScoreSnapshotIntegrityError as error:
        raise EvaluationIntegrityError("persisted score snapshot integrity failure") from error
    if (
        snapshot.selected_security_id != observation.security_id
        or snapshot.snapshot_fingerprint_sha256 != observation.snapshot_fingerprint_sha256
        or snapshot.scoring_configuration_id != run.scoring_configuration_id
        or snapshot.configuration_checksum_sha256 != run.scoring_configuration_checksum_sha256
        or _stored_utc(snapshot.knowledge_cutoff) > _stored_utc(observation.knowledge_cutoff)
        or snapshot.algorithm_version != V5_SNAPSHOT_VERSION
        or snapshot.snapshot_status != observation.snapshot_status
    ):
        raise EvaluationIntegrityError("frozen score snapshot binding conflicts with observation")
    available = tuple(sorted(str(item) for item in snapshot.available_component_codes_json))
    rankable = (
        snapshot.eligibility_eligible
        and snapshot.final_score is not None
        and snapshot.snapshot_status == "final_score_available"
    )
    return PredictionState(
        observation=observation,
        snapshot=snapshot,
        rankable=rankable,
        unranked_reason=None if rankable else _unranked_reason(observation, snapshot=snapshot),
        final_score=snapshot.final_score if rankable else None,
        dense_score_rank=None,
        display_order=None,
        available_components=available,
    )


def _unranked_reason(
    observation: BacktestObservation, *, snapshot: ScoreSnapshot | None = None
) -> str:
    if observation.observation_status == "issuer_unavailable":
        return "issuer_unavailable"
    if observation.observation_status == "research_failed":
        return "research_failed"
    if snapshot is None:
        return "no_snapshot"
    if not snapshot.eligibility_eligible:
        return "v5_ineligible"
    if snapshot.snapshot_status == "partial_component_set":
        return "partial_score"
    return "no_final_score"


def _prediction_projection(item: PredictionState) -> dict[str, object]:
    projection: dict[str, object] = {
        "backtest_observation_id": item.observation.id,
        "company_id": item.observation.company_id,
        "security_id": item.observation.security_id,
        "historical_symbol": item.observation.symbol,
        "score_snapshot_id": item.observation.score_snapshot_id,
        "snapshot_fingerprint_sha256": item.observation.snapshot_fingerprint_sha256,
        "snapshot_status": item.observation.snapshot_status,
        "rankable": item.rankable,
        "unranked_reason": item.unranked_reason,
        "final_score": item.final_score,
        "dense_score_rank": item.dense_score_rank,
        "display_order": item.display_order,
        "available_component_codes_json": list(item.available_components),
    }
    projection["prediction_fingerprint_sha256"] = canonical_json_sha256(_jsonable(projection))
    return projection


def _selection_count(contract: EvaluationSelectionContract, rankable_count: int) -> int:
    if rankable_count == 0:
        return 0
    if contract.kind == "count":
        return min(int(contract.value), rankable_count)
    # Decimal ceiling avoids binary-float percentage drift.
    count = int(
        (Decimal(rankable_count) * contract.value / Decimal("100")).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    return min(max(1, count), rankable_count)


def _selected(
    item: PredictionState, contract: EvaluationSelectionContract, rankable_count: int
) -> bool:
    return (
        item.rankable
        and item.display_order is not None
        and item.display_order <= _selection_count(contract, rankable_count)
    )


def _confusion_class(*, rankable: bool, selected: bool, actual_label: str, view: str) -> str:
    if actual_label == "unmatured":
        return "excluded_unmatured"
    if actual_label == "unavailable":
        return "excluded_unavailable"
    if view == "rankable_only" and not rankable:
        return "excluded_rankability_for_rankable_only"
    if actual_label == "positive":
        return "tp" if selected else "fn"
    if actual_label == "negative":
        return "fp" if selected else "tn"
    raise EvaluationIntegrityError("unsupported outcome label classification")


def _confusion_projection(
    *,
    label: MultibaggerOutcomeLabel,
    outcome_contract: str,
    selection_contract: str,
    view: str,
    selected: bool,
    confusion_class: str,
) -> dict[str, object]:
    projection: dict[str, object] = {
        "multibagger_outcome_label_id": label.id,
        "outcome_contract_code": outcome_contract,
        "selection_contract_code": selection_contract,
        "evaluation_view": view,
        "selected": selected,
        "actual_label": label.classification,
        "confusion_class": confusion_class,
        "threshold_hit_trading_date": label.first_threshold_hit_trading_date,
        "endpoint_return": label.endpoint_return,
        "peak_price_multiple": label.peak_price_multiple,
        "maximum_drawdown": label.maximum_drawdown,
        "label_provenance_json": label.provenance_json,
    }
    projection["confusion_fingerprint_sha256"] = canonical_json_sha256(_jsonable(projection))
    return projection


def _summary(session: Session, run_id: UUID) -> dict[str, object]:
    """Produce counts and Decimal metrics without choosing a winning contract."""

    from inflector_database.models import (
        MultibaggerConfusionRow,
        MultibaggerEvaluationCohort,
        MultibaggerPredictionRow,
    )

    rows = tuple(
        session.scalars(
            select(MultibaggerConfusionRow)
            .join(MultibaggerPredictionRow)
            .join(MultibaggerEvaluationCohort)
            .where(MultibaggerEvaluationCohort.multibagger_evaluation_run_id == run_id)
        )
    )
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        key = f"{row.outcome_contract_code}|{row.selection_contract_code}|{row.evaluation_view}"
        grouped.setdefault(key, []).append(row)
    result: dict[str, object] = {}
    for key, items in sorted(grouped.items()):
        view = items[0].evaluation_view
        view_items = (
            items if view == "end_to_end" else [item for item in items if item.prediction.rankable]
        )
        counts = {
            name: sum(item.confusion_class == name for item in view_items)
            for name in ("tp", "fp", "fn", "tn")
        }
        mature = sum(counts.values())
        actual_positive = counts["tp"] + counts["fn"]
        actual_negative = counts["fp"] + counts["tn"]
        selected = sum(item.selected for item in view_items)
        selected_mature = counts["tp"] + counts["fp"]
        rankable_count = sum(item.prediction.rankable for item in view_items)
        unrankable_count = len(view_items) - rankable_count
        unmatured_count = sum(
            item.confusion_class == "excluded_unmatured" for item in view_items
        )
        unavailable_count = sum(
            item.confusion_class == "excluded_unavailable" for item in view_items
        )
        if mature + unmatured_count + unavailable_count != len(view_items):
            raise EvaluationIntegrityError("evaluation view classification partition is invalid")
        positive_ranks = sorted(
            item.prediction.display_order
            for item in items
            if item.actual_label == "positive"
            and item.prediction.rankable
            and item.prediction.display_order is not None
        )
        cohorts = _cohort_metrics(all_items=items, view_items=view_items)
        result[key] = {
            **counts,
            "total_rows_in_view": len(view_items),
            "actual_positives": actual_positive,
            "actual_negatives": actual_negative,
            "selected_count": selected,
            "selected_mature_count": selected_mature,
            "mature_ground_truth_count": mature,
            "rankable_count": rankable_count,
            "unrankable_count": unrankable_count,
            "unmatured_count": unmatured_count,
            "unavailable_count": unavailable_count,
            "excluded_rankability_count": (
                sum(not item.prediction.rankable for item in items)
                if view == "rankable_only"
                else 0
            ),
            "precision": _decimal_json(_ratio(counts["tp"], selected_mature)),
            "recall": _decimal_json(_ratio(counts["tp"], actual_positive)),
            "f1": _decimal_json(_f1(counts["tp"], selected_mature, actual_positive)),
            "prevalence": _decimal_json(_ratio(actual_positive, mature)),
            "ground_truth_coverage": _decimal_json(_ratio(mature, len(view_items))),
            "prediction_coverage": _decimal_json(_ratio(rankable_count, len(view_items))),
            "positive_rank": _rank_statistics(positive_ranks),
            "cohort_weighted": cohorts,
        }
    return {
        "metrics": result,
        "score_buckets": _score_bucket_summary(rows),
        "grouped_diagnostics": _grouped_diagnostics(rows),
        "temporal_correlation": "observation_and_cohort_weighted_reporting_required_v1",
    }


def _cohort_metrics(
    *, all_items: list[Any], view_items: list[Any]
) -> dict[str, object]:
    all_cohort_ids = {item.prediction.cohort.id for item in all_items}
    by_cohort: dict[UUID, list[Any]] = {cohort_id: [] for cohort_id in all_cohort_ids}
    for item in view_items:
        by_cohort[item.prediction.cohort.id].append(item)
    hit_count = 0
    evaluable_cohort_count = 0
    precisions: list[Decimal] = []
    recalls: list[Decimal] = []
    f1s: list[Decimal] = []
    for cohort_items in by_cohort.values():
        mature_count = sum(
            item.confusion_class in {"tp", "fp", "fn", "tn"} for item in cohort_items
        )
        if mature_count:
            evaluable_cohort_count += 1
        if any(item.confusion_class == "tp" for item in cohort_items):
            hit_count += 1
        counts = {
            code: sum(item.confusion_class == code for item in cohort_items)
            for code in ("tp", "fp", "fn", "tn")
        }
        precision = _ratio(counts["tp"], counts["tp"] + counts["fp"])
        recall = _ratio(counts["tp"], counts["tp"] + counts["fn"])
        f1 = _f1(counts["tp"], counts["tp"] + counts["fp"], counts["tp"] + counts["fn"])
        if precision is not None:
            precisions.append(precision)
        if recall is not None:
            recalls.append(recall)
        if f1 is not None:
            f1s.append(f1)
    return {
        "cohort_count_total": len(by_cohort),
        "evaluable_cohort_count": evaluable_cohort_count,
        "cohorts_with_hit": hit_count,
        "cohort_hit_rate": _decimal_json(_ratio(hit_count, evaluable_cohort_count)),
        "macro_precision": _decimal_json(_mean(precisions)),
        "macro_precision_contributing_cohort_count": len(precisions),
        "macro_recall": _decimal_json(_mean(recalls)),
        "macro_recall_contributing_cohort_count": len(recalls),
        "macro_f1": _decimal_json(_mean(f1s)),
        "macro_f1_contributing_cohort_count": len(f1s),
    }


def _grouped_diagnostics(rows: tuple[Any, ...]) -> dict[str, object]:
    """Report fixed groups without mixing outcome, selection, or view contracts."""

    contract_rows: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        key = (
            row.outcome_contract_code,
            row.selection_contract_code,
            row.evaluation_view,
        )
        contract_rows.setdefault(key, []).append(row)
    output: dict[str, Any] = {}
    for (outcome, selection, view), items in sorted(contract_rows.items()):
        view_items = (
            items if view == "end_to_end" else [item for item in items if item.prediction.rankable]
        )
        groups: dict[str, dict[str, list[Any]]] = {
            "calendar_year": {},
            "historical_cutoff": {},
            "snapshot_status": {},
            "rankability": {},
            "component_availability_pattern": {},
            "score_bucket": {},
        }
        for row in view_items:
            prediction = row.prediction
            cohort = prediction.cohort
            values = {
                "calendar_year": str(_stored_utc(cohort.historical_cutoff).year),
                "historical_cutoff": _stored_utc(cohort.historical_cutoff).isoformat(),
                "snapshot_status": prediction.snapshot_status or "no_snapshot",
                "rankability": "rankable" if prediction.rankable else "unrankable",
                "component_availability_pattern": canonical_json_sha256(
                    list(prediction.available_component_codes_json)
                ),
                "score_bucket": _score_bucket_code(prediction.final_score),
            }
            for name, value in values.items():
                groups[name].setdefault(value, []).append(row)
        output.setdefault(outcome, {}).setdefault(selection, {})[view] = {
            name: {
                value: _diagnostic_counts(group_items)
                for value, group_items in sorted(values.items())
            }
            for name, values in groups.items()
        }
    return output


def _diagnostic_counts(items: list[Any]) -> dict[str, object]:
    counts = {
        code: sum(item.confusion_class == code for item in items)
        for code in ("tp", "fp", "fn", "tn")
    }
    mature = sum(counts.values())
    selected_mature = counts["tp"] + counts["fp"]
    positives = counts["tp"] + counts["fn"]
    return {
        **counts,
        "sample_count": len(items),
        "mature_ground_truth_count": mature,
        "unmatured_count": sum(item.actual_label == "unmatured" for item in items),
        "unavailable_count": sum(item.actual_label == "unavailable" for item in items),
        "precision": _decimal_json(_ratio(counts["tp"], selected_mature)),
        "recall": _decimal_json(_ratio(counts["tp"], positives)),
        "f1": _decimal_json(_f1(counts["tp"], selected_mature, positives)),
    }


def _score_bucket_code(score: Decimal | None) -> str:
    if score is None:
        return "unrankable"
    for code, minimum, maximum, inclusive in (
        ("[0,20)", Decimal("0"), Decimal("20"), False),
        ("[20,40)", Decimal("20"), Decimal("40"), False),
        ("[40,60)", Decimal("40"), Decimal("60"), False),
        ("[60,80)", Decimal("60"), Decimal("80"), False),
        ("[80,100]", Decimal("80"), Decimal("100"), True),
    ):
        if score >= minimum and (score <= maximum if inclusive else score < maximum):
            return code
    return "outside_j_v1_buckets"


def _rank_statistics(ranks: list[int]) -> dict[str, object]:
    if not ranks:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "best": None,
            "worst": None,
        }
    return {
        "count": len(ranks),
        "mean": _decimal_json(Decimal(sum(ranks)) / Decimal(len(ranks))),
        "median": _decimal_json(_median_decimal([Decimal(rank) for rank in ranks])),
        "best": ranks[0],
        "worst": ranks[-1],
    }


def _score_bucket_summary(rows: tuple[Any, ...]) -> dict[str, object]:
    """Use J's fixed score buckets; no outcome-derived segmentation is allowed."""

    boundaries = (
        ("[0,20)", Decimal("0"), Decimal("20"), False),
        ("[20,40)", Decimal("20"), Decimal("40"), False),
        ("[40,60)", Decimal("40"), Decimal("60"), False),
        ("[60,80)", Decimal("60"), Decimal("80"), False),
        ("[80,100]", Decimal("80"), Decimal("100"), True),
    )
    # Rows repeat per outcome/selection/view. Bucket facts are keyed by the
    # immutable prediction and outcome contract, then use a canonical TOP_5
    # end-to-end projection to avoid duplicate presentation counting.
    canonical = [
        item
        for item in rows
        if item.selection_contract_code == "TOP_5" and item.evaluation_view == "end_to_end"
    ]
    output: dict[str, object] = {}
    for contract in OUTCOME_CONTRACTS:
        contract_rows = [item for item in canonical if item.outcome_contract_code == contract]
        buckets: dict[str, object] = {}
        for code, minimum, maximum, upper_inclusive in boundaries:
            bucket = [
                item
                for item in contract_rows
                if item.prediction.final_score is not None
                and item.prediction.final_score >= minimum
                and (
                    item.prediction.final_score <= maximum
                    if upper_inclusive
                    else item.prediction.final_score < maximum
                )
            ]
            positives = sum(item.actual_label == "positive" for item in bucket)
            negatives = sum(item.actual_label == "negative" for item in bucket)
            mature = positives + negatives
            peak = [
                item.peak_price_multiple for item in bucket if item.peak_price_multiple is not None
            ]
            drawdown = [
                item.maximum_drawdown for item in bucket if item.maximum_drawdown is not None
            ]
            buckets[code] = {
                "sample_count": len(bucket),
                "positives": positives,
                "negatives": negatives,
                "unmatured": sum(item.actual_label == "unmatured" for item in bucket),
                "unavailable": sum(item.actual_label == "unavailable" for item in bucket),
                "insufficient_sample": len(bucket) < 20,
                "positive_rate_mature": _decimal_json(_ratio(positives, mature)),
                "mean_peak_multiple": _decimal_json(_mean(peak)),
                "median_peak_multiple": _decimal_json(_median_decimal(peak)),
                "mean_maximum_drawdown": _decimal_json(_mean(drawdown)),
            }
        output[contract] = buckets
    return output


def _mean(values: list[Decimal]) -> Decimal | None:
    return None if not values else sum(values, Decimal("0")) / Decimal(len(values))


def _median_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def summarize_multibagger_evaluation(session: Session, *, run_id: UUID) -> dict[str, object]:
    run = MultibaggerEvaluationRepository(session).get_run(run_id)
    if run is None or run.status != "completed":
        raise ValueError("completed multibagger evaluation run is unavailable")
    return {
        "run_id": str(run.id),
        "run_key_sha256": run.run_key_sha256,
        "summary": run.summary_json,
    }


def inspect_multibagger_errors(
    session: Session, *, run_id: UUID, confusion_class: str | None = None
) -> list[dict[str, object]]:
    """Return factual FP/FN projections; it intentionally supplies no explanations."""

    from inflector_database.models import (
        MultibaggerConfusionRow,
        MultibaggerEvaluationCohort,
        MultibaggerPredictionRow,
    )

    statement = (
        select(MultibaggerConfusionRow, MultibaggerPredictionRow, MultibaggerEvaluationCohort)
        .join(MultibaggerPredictionRow)
        .join(MultibaggerEvaluationCohort)
        .where(MultibaggerEvaluationCohort.multibagger_evaluation_run_id == run_id)
    )
    if confusion_class is not None:
        if confusion_class not in {"fp", "fn"}:
            raise ValueError("error inspection accepts only fp or fn")
        statement = statement.where(MultibaggerConfusionRow.confusion_class == confusion_class)
    else:
        statement = statement.where(MultibaggerConfusionRow.confusion_class.in_(("fp", "fn")))
    output: list[dict[str, object]] = []
    for confusion, prediction, cohort in session.execute(
        statement.order_by(
            MultibaggerEvaluationCohort.historical_cutoff,
            MultibaggerPredictionRow.historical_symbol,
        )
    ):
        boundary_count, boundary_score = _selection_boundary(
            session,
            prediction.multibagger_evaluation_cohort_id,
            confusion.selection_contract_code,
        )
        below_boundary = (
            None
            if not prediction.rankable or prediction.display_order is None
            else max(0, prediction.display_order - boundary_count)
        )
        score_gap = (
            None
            if prediction.final_score is None
            or boundary_score is None
            or prediction.display_order is None
            or prediction.display_order <= boundary_count
            else boundary_score - prediction.final_score
        )
        label = session.get(MultibaggerOutcomeLabel, confusion.multibagger_outcome_label_id)
        output.append(
            {
                "cutoff": _stored_utc(cohort.historical_cutoff).isoformat(),
                "security_id": str(prediction.security_id),
                "symbol": prediction.historical_symbol,
                "rankable": prediction.rankable,
                "unranked_reason": prediction.unranked_reason,
                "final_score": prediction.final_score,
                "dense_score_rank": prediction.dense_score_rank,
                "display_order": prediction.display_order,
                "selection_boundary_count": boundary_count,
                "selection_boundary_score": boundary_score,
                "rank_positions_below_selection_boundary": below_boundary,
                "score_gap_to_selection_boundary": score_gap,
                "selection_contract": confusion.selection_contract_code,
                "outcome_contract": confusion.outcome_contract_code,
                "evaluation_view": confusion.evaluation_view,
                "confusion_class": confusion.confusion_class,
                "threshold_hit_date": confusion.threshold_hit_trading_date,
                "time_to_threshold_calendar_days": None
                if label is None
                else label.calendar_days_to_threshold,
                "time_to_threshold_trading_observations": None
                if label is None
                else label.trading_observations_to_threshold,
                "endpoint_return": confusion.endpoint_return,
                "peak_price_multiple": confusion.peak_price_multiple,
                "maximum_drawdown": confusion.maximum_drawdown,
                "available_component_codes": prediction.available_component_codes_json,
                "label_provenance": confusion.label_provenance_json,
            }
        )
    return output


def _selection_boundary(
    session: Session, cohort_id: UUID, selection_code: str
) -> tuple[int, Decimal | None]:
    from inflector_database.models import MultibaggerPredictionRow

    policy = load_multibagger_evaluation_policy(
        Path(__file__).parents[3] / "config/backtest/production_multibagger_evaluation_v1.json",
        repository_root=Path(__file__).parents[3],
    )
    contract = next(item for item in policy.selection_contracts if item.code == selection_code)
    rows = tuple(
        session.scalars(
            select(MultibaggerPredictionRow)
            .where(
                MultibaggerPredictionRow.multibagger_evaluation_cohort_id == cohort_id,
                MultibaggerPredictionRow.rankable.is_(True),
            )
            .order_by(MultibaggerPredictionRow.display_order)
        )
    )
    count = _selection_count(contract, len(rows))
    return count, None if count == 0 else rows[count - 1].final_score


def _boundary_ties(predictions: tuple[PredictionState, ...]) -> dict[str, bool]:
    # Stored as audit metadata for every reviewed fixed contract. The values are
    # policy constants, never selected from label outcomes.
    ranked = {
        item.display_order: item
        for item in predictions
        if item.rankable and item.display_order is not None
    }
    contracts = (
        ("TOP_5", 5),
        ("TOP_10", 10),
        ("TOP_20", 20),
        (
            "TOP_1_PERCENT",
            _selection_count(
                EvaluationSelectionContract("TOP_1_PERCENT", "percent", Decimal("1")), len(ranked)
            ),
        ),
        (
            "TOP_5_PERCENT",
            _selection_count(
                EvaluationSelectionContract("TOP_5_PERCENT", "percent", Decimal("5")), len(ranked)
            ),
        ),
        (
            "TOP_10_PERCENT",
            _selection_count(
                EvaluationSelectionContract("TOP_10_PERCENT", "percent", Decimal("10")), len(ranked)
            ),
        ),
    )
    result: dict[str, bool] = {}
    for code, count in contracts:
        current = ranked.get(count)
        following = ranked.get(count + 1)
        result[code] = bool(current and following and current.final_score == following.final_score)
    return result


def _validate_cross_bundle_compatibility(prepared: tuple[_PreparedBundle, ...]) -> None:
    first = prepared[0]
    for item in prepared[1:]:
        if (item.model_family, item.configuration_checksum) != (
            first.model_family,
            first.configuration_checksum,
        ):
            raise EvaluationIntegrityError("evaluation bundles mix frozen model configuration")


def _validate_bundle_shape(bundle: EvaluationCohortBundle) -> None:
    if not bundle.backtest_run_ids or not bundle.label_run_ids:
        raise ValueError("cohort bundle requires backtest and label run IDs")
    if len(set(bundle.backtest_run_ids)) != len(bundle.backtest_run_ids):
        raise ValueError("cohort bundle repeats a backtest run")
    if len(set(bundle.label_run_ids)) != len(bundle.label_run_ids):
        raise ValueError("cohort bundle repeats a label run")
    if bundle.calendar_integrity_status not in {"fixture_verified", "not_independently_verified"}:
        raise ValueError("calendar integrity status is unsupported")


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    return None if denominator == 0 else Decimal(numerator) / Decimal(denominator)


def _f1(tp: int, selected: int, actual_positive: int) -> Decimal | None:
    precision = _ratio(tp, selected)
    recall = _ratio(tp, actual_positive)
    if precision is None or recall is None or precision + recall == 0:
        return None
    return Decimal("2") * precision * recall / (precision + recall)


def _decimal_json(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    """Normalize persisted timestamps; SQLite does not round-trip tzinfo."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _jsonable(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value, "timestamp").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value
