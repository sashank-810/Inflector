"""Forward outcome construction over already-frozen historical research states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.backtest_policy import BacktestPolicy
from inflector_data.corporate_action_pit import PointInTimeCorporateActionReader
from inflector_data.market_adjustments import AdjustedMarketSeries, MarketAdjustmentPrimitives
from inflector_data.market_pit import (
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)
from inflector_data.production_policy import resolve_profile_datasets
from inflector_data.research_profile import ProductionResearchProfile
from inflector_database.backtest_repository import (
    BacktestOutcomeWrite,
    BacktestRepository,
)
from inflector_database.models import BacktestObservation, BacktestRun, ExchangeListing


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    horizon_observations: int
    status: str
    entry_trading_date: date | None
    exit_trading_date: date | None
    entry_adjusted_close: Decimal | None
    exit_adjusted_close: Decimal | None
    security_return: Decimal | None
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    provenance: dict[str, object]


@dataclass(frozen=True, slots=True)
class OutcomeBuildResult:
    run_id: UUID
    already_built: bool
    outcomes_created: int
    outcomes_reused: int
    available: int
    security_only: int
    unavailable: int


def compute_forward_outcome(
    *,
    series: AdjustedMarketSeries,
    benchmark_bars: tuple[PointInTimeBenchmarkBar, ...],
    entry_raw_bar: PointInTimeMarketBar | None,
    knowledge_cutoff: datetime,
    horizon: int,
    listing_valid_to: date | None,
) -> ForwardOutcome:
    """Compute one exact observation horizon without reading research inputs."""

    cutoff = _utc(knowledge_cutoff, "knowledge_cutoff")
    if horizon <= 0:
        raise ValueError("forward horizon must be positive")
    future = [bar for bar in series.bars if bar.raw_bar.trading_date > cutoff.date()]
    if entry_raw_bar is None:
        return _unavailable(horizon, "entry_price_unavailable_at_knowledge_cutoff")
    entry = next(
        (bar for bar in series.bars if bar.raw_bar.id == entry_raw_bar.id),
        None,
    )
    if entry is None:
        return _unavailable(
            horizon,
            "entry_price_bar_not_represented_in_outcome_series",
            provenance={"pit_entry_price_bar_id": str(entry_raw_bar.id)},
        )
    if len(future) < horizon:
        status = (
            "outcome_unavailable_due_to_delisting"
            if listing_valid_to is not None
            and cutoff.date() < listing_valid_to <= series.as_of.date()
            else "insufficient_future_history"
        )
        return ForwardOutcome(
            horizon_observations=horizon,
            status=status,
            entry_trading_date=entry.raw_bar.trading_date,
            exit_trading_date=None,
            entry_adjusted_close=entry.adjusted_close,
            exit_adjusted_close=None,
            security_return=None,
            benchmark_return=None,
            excess_return=None,
            provenance={
                "available_forward_observations": len(future),
                "required_forward_observations": horizon,
                "listing_valid_to": listing_valid_to.isoformat() if listing_valid_to else None,
            },
        )
    exit_bar = future[horizon - 1]
    unsupported = [
        action
        for action in series.selected_actions
        if action.action_type in {"rights", "security_replacement"}
        and action.event_anchor is not None
        and entry.raw_bar.trading_date < action.event_anchor <= exit_bar.raw_bar.trading_date
    ]
    if unsupported:
        return ForwardOutcome(
            horizon_observations=horizon,
            status="unsupported_corporate_action_state",
            entry_trading_date=entry.raw_bar.trading_date,
            exit_trading_date=exit_bar.raw_bar.trading_date,
            entry_adjusted_close=entry.adjusted_close,
            exit_adjusted_close=exit_bar.adjusted_close,
            security_return=None,
            benchmark_return=None,
            excess_return=None,
            provenance={
                "unsupported_action_ids": [str(action.id) for action in unsupported],
                "unsupported_action_types": [action.action_type for action in unsupported],
            },
        )
    if entry.adjusted_close <= 0:
        return _unavailable(horizon, "non_positive_entry_price")
    security_return = exit_bar.adjusted_close / entry.adjusted_close - Decimal("1")
    benchmark_by_date = {bar.trading_date: bar for bar in benchmark_bars}
    benchmark_entry = benchmark_by_date.get(entry.raw_bar.trading_date)
    benchmark_exit = benchmark_by_date.get(exit_bar.raw_bar.trading_date)
    benchmark_return: Decimal | None = None
    if (
        benchmark_entry is not None
        and benchmark_exit is not None
        and benchmark_entry.close_value > 0
    ):
        benchmark_return = benchmark_exit.close_value / benchmark_entry.close_value - Decimal("1")
    status = "available" if benchmark_return is not None else "security_return_available"
    return ForwardOutcome(
        horizon_observations=horizon,
        status=status,
        entry_trading_date=entry.raw_bar.trading_date,
        exit_trading_date=exit_bar.raw_bar.trading_date,
        entry_adjusted_close=entry.adjusted_close,
        exit_adjusted_close=exit_bar.adjusted_close,
        security_return=security_return,
        benchmark_return=benchmark_return,
        excess_return=(
            security_return - benchmark_return if benchmark_return is not None else None
        ),
        provenance={
            "entry_price_bar_id": str(entry.raw_bar.id),
            "exit_price_bar_id": str(exit_bar.raw_bar.id),
            "applied_corporate_action_ids": sorted(
                {
                    str(adjustment.action.id)
                    for bar in (entry, exit_bar)
                    for adjustment in bar.applied_adjustments
                }
            ),
            "benchmark_entry_bar_id": (
                str(benchmark_entry.id) if benchmark_entry is not None else None
            ),
            "benchmark_exit_bar_id": (
                str(benchmark_exit.id) if benchmark_exit is not None else None
            ),
            "return_definition": "price_return_excluding_cash_dividends",
        },
    )


def build_forward_outcomes(
    session: Session,
    *,
    run_id: UUID,
    policy: BacktestPolicy,
    research_profile: ProductionResearchProfile,
    outcome_data_cutoff: datetime,
) -> OutcomeBuildResult:
    """Compute outcomes after snapshot freeze; never invoke research assembly."""

    data_cutoff = _utc(outcome_data_cutoff, "outcome_data_cutoff")
    repository = BacktestRepository(session)
    run = repository.get_run(run_id)
    if run is None:
        raise ValueError("backtest run is unavailable")
    _validate_run_bindings(run, policy, research_profile)
    if run.status in {"outcomes_built", "completed"}:
        return _outcome_result(run, already_built=True)
    if run.status != "snapshots_built":
        raise ValueError("backtest snapshots must be frozen before outcome construction")
    repository.freeze_outcome_cutoff(run, data_cutoff)
    dataset_ids = resolve_profile_datasets(session, research_profile)
    market_id = _required_id(dataset_ids, "market")
    benchmark_id = _required_id(dataset_ids, "benchmark")
    actions_id = _required_id(dataset_ids, "corporate_actions")
    market_reader = PointInTimeMarketReader(session)
    adjustments = MarketAdjustmentPrimitives(
        market_reader, PointInTimeCorporateActionReader(session)
    )
    benchmark = tuple(
        market_reader.benchmark_series_as_of(
            provider_dataset_id=benchmark_id,
            benchmark_code=policy.benchmark_code,
            interval=research_profile.market_interval,
            as_of=data_cutoff,
        )
    )
    counts = {"created": 0, "reused": 0, "available": 0, "security_only": 0, "unavailable": 0}
    for observation in sorted(
        run.observations, key=lambda item: (item.knowledge_cutoff, item.symbol, str(item.id))
    ):
        if observation.score_snapshot_id is None:
            outcomes = tuple(
                _unavailable(horizon, "research_snapshot_unavailable")
                for horizon in policy.forward_return_horizons
            )
        else:
            series = adjustments.adjusted_market_series_as_of(
                market_provider_dataset_id=market_id,
                corporate_action_provider_dataset_id=actions_id,
                security_id=observation.security_id,
                interval=research_profile.market_interval,
                as_of=data_cutoff,
            )
            entry_raw_bar = market_reader.latest_market_bar_as_of(
                provider_dataset_id=market_id,
                security_id=observation.security_id,
                interval=research_profile.market_interval,
                as_of=observation.knowledge_cutoff,
                on_or_before=observation.knowledge_cutoff.date(),
            )
            listing_valid_to = _listing_valid_to(session, observation)
            outcomes = tuple(
                compute_forward_outcome(
                    series=series,
                    benchmark_bars=benchmark,
                    entry_raw_bar=entry_raw_bar,
                    knowledge_cutoff=observation.knowledge_cutoff,
                    horizon=horizon,
                    listing_valid_to=listing_valid_to,
                )
                for horizon in policy.forward_return_horizons
            )
        for outcome in outcomes:
            _, created = repository.add_outcome(
                observation,
                BacktestOutcomeWrite(
                    horizon_observations=outcome.horizon_observations,
                    outcome_status=outcome.status,
                    entry_trading_date=outcome.entry_trading_date,
                    exit_trading_date=outcome.exit_trading_date,
                    entry_adjusted_close=outcome.entry_adjusted_close,
                    exit_adjusted_close=outcome.exit_adjusted_close,
                    security_return=outcome.security_return,
                    benchmark_return=outcome.benchmark_return,
                    excess_return=outcome.excess_return,
                    outcome_data_cutoff=data_cutoff,
                    provenance_json={
                        **outcome.provenance,
                        "market_provider_dataset_id": str(market_id),
                        "benchmark_provider_dataset_id": str(benchmark_id),
                        "corporate_action_provider_dataset_id": str(actions_id),
                        "price_adjustment_version": "adjusted_market_price_v1",
                        "cash_dividends_included": False,
                    },
                ),
            )
            counts["created" if created else "reused"] += 1
            if outcome.status == "available":
                counts["available"] += 1
            elif outcome.status == "security_return_available":
                counts["security_only"] += 1
            else:
                counts["unavailable"] += 1
        session.commit()
    summary = dict(run.summary_json)
    summary["outcomes"] = {
        "created": counts["created"],
        "reused": counts["reused"],
        "available": counts["available"],
        "security_return_available": counts["security_only"],
        "unavailable": counts["unavailable"],
        "outcome_data_cutoff": data_cutoff.isoformat(),
    }
    repository.set_status(run, "outcomes_built", summary=summary)
    session.commit()
    return OutcomeBuildResult(
        run_id=run.id,
        already_built=False,
        outcomes_created=counts["created"],
        outcomes_reused=counts["reused"],
        available=counts["available"],
        security_only=counts["security_only"],
        unavailable=counts["unavailable"],
    )


def _validate_run_bindings(
    run: BacktestRun, policy: BacktestPolicy, profile: ProductionResearchProfile
) -> None:
    if (
        run.backtest_policy_checksum_sha256 != policy.checksum_sha256
        or run.availability_manifest_checksum_sha256
        != policy.historical_availability_manifest_checksum_sha256
        or run.research_profile_checksum_sha256 != profile.checksum_sha256
        or run.financial_primitive_policy_checksum_sha256
        != policy.financial_primitive_policy_checksum_sha256
        or run.financial_endpoint_policy_checksum_sha256
        != policy.financial_endpoint_policy_checksum_sha256
    ):
        raise ValueError("backtest run policy bindings conflict with requested configuration")


def _listing_valid_to(session: Session, observation: BacktestObservation) -> date | None:
    rows = list(
        session.scalars(
            select(ExchangeListing).where(
                ExchangeListing.security_id == observation.security_id,
                ExchangeListing.exchange == "NSE",
                ExchangeListing.valid_from <= observation.knowledge_cutoff.date(),
                (
                    ExchangeListing.valid_to.is_(None)
                    | (ExchangeListing.valid_to >= observation.knowledge_cutoff.date())
                ),
            )
        )
    )
    future_ends = [row.valid_to for row in rows if row.valid_to is not None]
    return min(future_ends) if future_ends else None


def _unavailable(
    horizon: int, status: str, *, provenance: dict[str, object] | None = None
) -> ForwardOutcome:
    return ForwardOutcome(
        horizon_observations=horizon,
        status=status,
        entry_trading_date=None,
        exit_trading_date=None,
        entry_adjusted_close=None,
        exit_adjusted_close=None,
        security_return=None,
        benchmark_return=None,
        excess_return=None,
        provenance=provenance or {},
    )


def _required_id(values: dict[str, UUID | None], domain: str) -> UUID:
    value = values.get(domain)
    if value is None:
        raise ValueError(f"backtest requires provider dataset domain: {domain}")
    return value


def _outcome_result(run: BacktestRun, *, already_built: bool) -> OutcomeBuildResult:
    outcomes = [outcome for observation in run.observations for outcome in observation.outcomes]
    return OutcomeBuildResult(
        run_id=run.id,
        already_built=already_built,
        outcomes_created=0,
        outcomes_reused=len(outcomes),
        available=sum(item.outcome_status == "available" for item in outcomes),
        security_only=sum(item.outcome_status == "security_return_available" for item in outcomes),
        unavailable=sum(
            item.outcome_status not in {"available", "security_return_available"}
            for item in outcomes
        ),
    )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
