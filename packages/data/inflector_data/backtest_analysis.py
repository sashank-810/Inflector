"""Deterministic descriptive summaries and derivative CSV export for backtests."""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_data.backtest_policy import BacktestPolicy
from inflector_database.backtest_repository import BacktestRepository
from inflector_database.models import (
    BacktestObservation,
    BacktestOutcome,
    BacktestRun,
    ScoreSnapshot,
)


@dataclass(frozen=True, slots=True)
class BacktestSummaryResult:
    run_id: UUID
    summary: dict[str, object]
    export_path: str | None


def summarize_backtest(
    session: Session,
    *,
    run_id: UUID,
    policy: BacktestPolicy,
    export_csv: Path | None = None,
) -> BacktestSummaryResult:
    """Describe frozen observations/outcomes; never acquire data or invoke research."""

    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        raise ValueError("backtest run is unavailable")
    if run.backtest_policy_checksum_sha256 != policy.checksum_sha256:
        raise ValueError("backtest policy does not match persisted run")
    if run.status not in {"outcomes_built", "completed"}:
        raise ValueError("backtest outcomes must be built before summary")
    observations = sorted(
        run.observations, key=lambda item: (item.knowledge_cutoff, item.symbol, str(item.id))
    )
    snapshots = {
        observation.id: session.get(ScoreSnapshot, observation.score_snapshot_id)
        for observation in observations
        if observation.score_snapshot_id is not None
    }
    snapshot_values = [value for value in snapshots.values() if value is not None]
    components: dict[str, int] = {}
    for snapshot in snapshot_values:
        for code in snapshot.available_component_codes_json:
            components[str(code)] = components.get(str(code), 0) + 1
    horizon_summaries = {
        str(horizon): _summarize_horizon(
            [
                outcome
                for observation in observations
                for outcome in observation.outcomes
                if outcome.horizon_observations == horizon
            ],
            policy=policy,
            horizon=horizon,
        )
        for horizon in policy.forward_return_horizons
    }
    years: dict[str, int] = {}
    for observation in observations:
        year = str(observation.knowledge_cutoff.year)
        years[year] = years.get(year, 0) + 1
    bucket_summary = _score_bucket_summary(snapshot_values, policy)
    summary: dict[str, object] = {
        "backtest_policy_code": policy.code,
        "backtest_policy_checksum": policy.checksum_sha256,
        "mode": policy.mode,
        "run_id": str(run.id),
        "run_key": run.run_key_sha256,
        "eligible_cutoffs": len({item.knowledge_cutoff for item in observations}),
        "companies_evaluated": len({item.company_id for item in observations}),
        "research_observations": len(observations),
        "research_snapshots": len(snapshot_values),
        "state_change_observations": sum(item.research_state_changed for item in observations),
        "final_score_snapshots": sum(
            snapshot.final_score is not None for snapshot in snapshot_values
        ),
        "partial_snapshots": sum(
            snapshot.snapshot_status == "partial_component_set" for snapshot in snapshot_values
        ),
        "component_availability_counts": dict(sorted(components.items())),
        "calendar_year_distribution": dict(sorted(years.items())),
        "forward_outcomes": horizon_summaries,
        "predefined_score_buckets": bucket_summary,
        "return_definition": "corporate-action-adjusted price return; cash dividends excluded",
        "benchmark": policy.benchmark_code,
        "universe_policy": policy.eligible_universe_policy,
        "survivorship_limitation": (
            "requested symbols require PIT-visible persisted identity and market evidence; "
            "the repository cannot claim a complete market-wide historical universe"
        ),
        "sample_warnings": _sample_warnings(snapshot_values, horizon_summaries, policy),
        "analysis_kind": "descriptive_precommitted_no_parameter_optimization",
        "dataset_build": dict(run.summary_json),
    }
    export_path = None
    if export_csv is not None:
        _export_csv(export_csv, run, observations, snapshots)
        export_path = str(export_csv.resolve())
    if run.status == "outcomes_built":
        repository.set_status(run, "completed", summary=summary)
        session.commit()
    return BacktestSummaryResult(run_id=run.id, summary=summary, export_path=export_path)


def _summarize_horizon(
    outcomes: list[BacktestOutcome], *, policy: BacktestPolicy, horizon: int
) -> dict[str, object]:
    security = [item.security_return for item in outcomes if item.security_return is not None]
    benchmark = [item.benchmark_return for item in outcomes if item.benchmark_return is not None]
    excess = [item.excess_return for item in outcomes if item.excess_return is not None]
    statuses: dict[str, int] = {}
    for item in outcomes:
        statuses[item.outcome_status] = statuses.get(item.outcome_status, 0) + 1
    result: dict[str, object] = {
        "total": len(outcomes),
        "status_counts": dict(sorted(statuses.items())),
        "security_return_available": len(security),
        "benchmark_return_available": len(benchmark),
        "excess_return_available": len(excess),
        "security_return_mean": _decimal_text(_mean(security)),
        "security_return_median": _decimal_text(_median(security)),
        "benchmark_return_mean": _decimal_text(_mean(benchmark)),
        "excess_return_mean": _decimal_text(_mean(excess)),
        "excess_return_median": _decimal_text(_median(excess)),
        "positive_return_rate": _decimal_text(_rate(security, lambda item: item > 0)),
    }
    if horizon == policy.multibagger_horizon_observations:
        result["multibagger_count"] = sum(
            item >= policy.multibagger_return_threshold for item in security
        )
    return result


def _sample_warnings(
    snapshots: list[ScoreSnapshot],
    horizons: dict[str, dict[str, object]],
    policy: BacktestPolicy,
) -> list[str]:
    warnings: list[str] = []
    if len(snapshots) < policy.minimum_bucket_sample_size:
        warnings.append("insufficient_sample_for_predefined_score_buckets")
    for horizon, values in horizons.items():
        available = values.get("security_return_available")
        if not isinstance(available, int) or available < policy.minimum_bucket_sample_size:
            warnings.append(f"insufficient_sample_for_{horizon}_observation_inference")
    return warnings


def _score_bucket_summary(
    snapshots: list[ScoreSnapshot], policy: BacktestPolicy
) -> list[dict[str, object]] | None:
    scored = [snapshot.final_score for snapshot in snapshots if snapshot.final_score is not None]
    if len(scored) < policy.minimum_bucket_sample_size:
        return None
    result: list[dict[str, object]] = []
    for bucket in policy.score_buckets:
        minimum = Decimal(bucket["minimum_inclusive"])
        maximum_text = bucket.get("maximum_exclusive")
        maximum_inclusive_text = bucket.get("maximum_inclusive")
        if maximum_text is not None:
            maximum = Decimal(maximum_text)
            count = sum(minimum <= value < maximum for value in scored)
        elif maximum_inclusive_text is not None:
            maximum = Decimal(maximum_inclusive_text)
            count = sum(minimum <= value <= maximum for value in scored)
        else:
            raise ValueError("score bucket has no maximum boundary")
        result.append({"boundaries": bucket, "snapshot_count": count})
    return result


def _export_csv(
    path: Path,
    run: BacktestRun,
    observations: Sequence[BacktestObservation],
    snapshots: dict[UUID, ScoreSnapshot | None],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "backtest_run_id",
        "backtest_run_key",
        "backtest_policy_checksum",
        "availability_manifest_checksum",
        "scoring_configuration_checksum",
        "research_profile_checksum",
        "financial_primitive_policy_checksum",
        "financial_endpoint_policy_checksum",
        "observation_id",
        "company_id",
        "security_id",
        "symbol",
        "knowledge_cutoff",
        "selected_fiscal_year",
        "selected_fiscal_quarter",
        "selected_filing_scope",
        "snapshot_id",
        "snapshot_fingerprint",
        "snapshot_status",
        "final_score",
        "available_components",
        "missing_components",
        "component_scores",
        "research_state_changed",
        "horizon_observations",
        "outcome_status",
        "security_return",
        "benchmark_return",
        "excess_return",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for observation in observations:
            snapshot = snapshots.get(observation.id)
            outcomes = sorted(observation.outcomes, key=lambda item: item.horizon_observations) or [
                None
            ]
            component_scores = (
                "|".join(
                    f"{item.component_code}:{_decimal_text(item.score)}"
                    for item in sorted(snapshot.components, key=lambda item: item.component_code)
                )
                if snapshot is not None
                else ""
            )
            for outcome in outcomes:
                writer.writerow(
                    {
                        "backtest_run_id": str(run.id),
                        "backtest_run_key": run.run_key_sha256,
                        "backtest_policy_checksum": run.backtest_policy_checksum_sha256,
                        "availability_manifest_checksum": (
                            run.availability_manifest_checksum_sha256
                        ),
                        "scoring_configuration_checksum": (
                            run.scoring_configuration_checksum_sha256
                        ),
                        "research_profile_checksum": run.research_profile_checksum_sha256,
                        "financial_primitive_policy_checksum": (
                            run.financial_primitive_policy_checksum_sha256
                        ),
                        "financial_endpoint_policy_checksum": (
                            run.financial_endpoint_policy_checksum_sha256
                        ),
                        "observation_id": str(observation.id),
                        "company_id": str(observation.company_id),
                        "security_id": str(observation.security_id),
                        "symbol": observation.symbol,
                        "knowledge_cutoff": observation.knowledge_cutoff.isoformat(),
                        "selected_fiscal_year": observation.selected_fiscal_year,
                        "selected_fiscal_quarter": observation.selected_fiscal_quarter,
                        "selected_filing_scope": observation.selected_filing_scope,
                        "snapshot_id": str(snapshot.id) if snapshot is not None else "",
                        "snapshot_fingerprint": (
                            snapshot.snapshot_fingerprint_sha256 if snapshot is not None else ""
                        ),
                        "snapshot_status": snapshot.snapshot_status if snapshot else "",
                        "final_score": _decimal_text(snapshot.final_score) if snapshot else "",
                        "available_components": (
                            "|".join(map(str, snapshot.available_component_codes_json))
                            if snapshot
                            else ""
                        ),
                        "missing_components": (
                            "|".join(map(str, snapshot.missing_component_codes_json))
                            if snapshot
                            else ""
                        ),
                        "component_scores": component_scores,
                        "research_state_changed": str(observation.research_state_changed).lower(),
                        "horizon_observations": (outcome.horizon_observations if outcome else ""),
                        "outcome_status": outcome.outcome_status if outcome else "",
                        "security_return": (
                            _decimal_text(outcome.security_return) if outcome else ""
                        ),
                        "benchmark_return": (
                            _decimal_text(outcome.benchmark_return) if outcome else ""
                        ),
                        "excess_return": (_decimal_text(outcome.excess_return) if outcome else ""),
                    }
                )


def _mean(values: list[Decimal]) -> Decimal | None:
    return None if not values else sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _rate(values: list[Decimal], predicate: Callable[[Decimal], bool]) -> Decimal | None:
    if not values:
        return None
    matches = sum(1 for item in values if predicate(item))
    return Decimal(matches) / Decimal(len(values))


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
