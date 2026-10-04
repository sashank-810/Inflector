"""Production Q factual multibagger labels over frozen J observations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.corporate_action_pit import PointInTimeCorporateActionReader
from inflector_data.historical_evaluation_policy import (
    MultibaggerContract,
    MultibaggerOutcomePolicy,
    canonical_json_sha256,
)
from inflector_data.market_adjustments import (
    ADJUSTED_MARKET_PRICE_VERSION,
    AdjustedMarketBar,
    AdjustedMarketSeries,
    MarketAdjustmentPrimitives,
)
from inflector_data.market_pit import (
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)
from inflector_data.production_policy import resolve_profile_datasets
from inflector_data.research_profile import ProductionResearchProfile
from inflector_database.backtest_repository import BacktestRepository
from inflector_database.historical_evaluation_repository import (
    HistoricalEvaluationRepository,
    MultibaggerLabelWrite,
)
from inflector_database.models import (
    BenchmarkBar,
    BenchmarkSeries,
    CorporateAction,
    ExchangeListing,
    MultibaggerLabelRun,
    PriceBar,
    SourceRecord,
)

OUTCOME_WINDOW_COMPLETENESS_VERSION = (
    "nifty50_session_dates_full_security_bar_coverage_v1"
)


@dataclass(frozen=True, slots=True)
class MultibaggerLabelMeasurement:
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
    provenance: dict[str, object]


@dataclass(frozen=True, slots=True)
class MultibaggerLabelBuildResult:
    run_id: UUID
    run_key_sha256: str
    created_run: bool
    labels_created: int
    labels_reused: int
    summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class _OutcomeWindowCoverage:
    complete: bool
    expected_dates: tuple[date, ...]
    missing_dates: tuple[date, ...]
    coverage_bar: PointInTimeBenchmarkBar | None
    provenance: dict[str, object]


def compute_multibagger_label(
    *,
    series: AdjustedMarketSeries,
    entry_raw_bar: PointInTimeMarketBar | None,
    contract: MultibaggerContract,
    outcome_data_cutoff: datetime,
    listing_valid_to: date | None,
    trading_calendar_bars: tuple[PointInTimeBenchmarkBar, ...],
) -> MultibaggerLabelMeasurement:
    """Classify one calendar-window price-multiple contract using exact Decimal."""

    data_cutoff = _utc(outcome_data_cutoff, "outcome_data_cutoff")
    if entry_raw_bar is None:
        return _unavailable(
            contract,
            data_cutoff,
            "entry_price_unavailable",
            listing_valid_to=listing_valid_to,
        )
    entry = next((bar for bar in series.bars if bar.raw_bar.id == entry_raw_bar.id), None)
    if entry is None:
        return _unavailable(
            contract,
            data_cutoff,
            "entry_price_bar_not_represented_in_outcome_series",
            listing_valid_to=listing_valid_to,
            provenance={"pit_entry_price_bar_id": str(entry_raw_bar.id)},
        )
    horizon_end = _add_calendar_years(entry.raw_bar.trading_date, contract.horizon_calendar_years)
    if entry.adjusted_close <= Decimal("0"):
        return _unavailable(
            contract,
            data_cutoff,
            "non_positive_entry_price",
            entry=entry,
            horizon_end=horizon_end,
            listing_valid_to=listing_valid_to,
        )
    future_all = [
        bar for bar in series.bars if bar.raw_bar.trading_date > entry.raw_bar.trading_date
    ]
    if len({bar.raw_bar.trading_date for bar in future_all}) != len(future_all):
        return _unavailable(
            contract,
            data_cutoff,
            "ambiguous_market_history",
            entry=entry,
            horizon_end=horizon_end,
            listing_valid_to=listing_valid_to,
        )
    future = [bar for bar in future_all if bar.raw_bar.trading_date <= horizon_end]
    provenance = _provenance(series, entry, future)
    unsupported = [
        action
        for action in series.selected_actions
        if action.action_type in {"rights", "security_replacement"}
        and action.event_anchor is not None
        and entry.raw_bar.trading_date < action.event_anchor <= horizon_end
    ]
    if unsupported:
        return _unavailable(
            contract,
            data_cutoff,
            "unsupported_corporate_action_state",
            entry=entry,
            horizon_end=horizon_end,
            listing_valid_to=listing_valid_to,
            provenance={
                **provenance,
                "unsupported_action_ids": [str(action.id) for action in unsupported],
                "unsupported_action_types": [action.action_type for action in unsupported],
            },
        )
    multiples = [bar.adjusted_close / entry.adjusted_close for bar in future]
    hit_index = next(
        (
            index
            for index, multiple in enumerate(multiples)
            if multiple >= contract.threshold_multiple
        ),
        None,
    )
    peak_close = max((bar.adjusted_close for bar in future), default=None)
    peak_multiple = peak_close / entry.adjusted_close if peak_close is not None else None
    endpoint = future[-1] if future else None
    maximum_drawdown = _maximum_drawdown(
        entry.adjusted_close,
        tuple(bar.adjusted_close for bar in future),
    )
    common = MultibaggerLabelMeasurement(
        contract_code=contract.code,
        classification="unmatured",
        unavailable_reason=None,
        threshold_multiple=contract.threshold_multiple,
        horizon_calendar_years=contract.horizon_calendar_years,
        entry_trading_date=entry.raw_bar.trading_date,
        adjusted_entry_close=entry.adjusted_close,
        horizon_end_date=horizon_end,
        first_threshold_hit_trading_date=None,
        trading_observations_available=len(future),
        peak_adjusted_close=peak_close,
        peak_price_multiple=peak_multiple,
        maximum_forward_price_return=(
            peak_multiple - Decimal("1") if peak_multiple is not None else None
        ),
        endpoint_adjusted_close=endpoint.adjusted_close if endpoint else None,
        endpoint_return=(
            endpoint.adjusted_close / entry.adjusted_close - Decimal("1") if endpoint else None
        ),
        calendar_days_to_threshold=None,
        trading_observations_to_threshold=None,
        maximum_drawdown=maximum_drawdown,
        listing_valid_to=listing_valid_to,
        label_matured_at=None,
        outcome_data_cutoff=data_cutoff,
        provenance=provenance,
    )
    if hit_index is not None:
        hit = future[hit_index]
        matured_at, maturity_action_ids = _positive_label_maturity(series, entry, hit)
        return replace(
            common,
            classification="positive",
            first_threshold_hit_trading_date=hit.raw_bar.trading_date,
            calendar_days_to_threshold=(hit.raw_bar.trading_date - entry.raw_bar.trading_date).days,
            trading_observations_to_threshold=hit_index + 1,
            label_matured_at=matured_at,
            provenance={
                **provenance,
                "positive_maturity_corporate_action_ids": maturity_action_ids,
            },
        )
    if listing_valid_to is not None and listing_valid_to <= horizon_end:
        return replace(
            common,
            classification="unavailable",
            unavailable_reason="unavailable_due_to_delisting",
        )
    if data_cutoff.date() < horizon_end:
        return common
    coverage = _outcome_window_coverage(
        entry_date=entry.raw_bar.trading_date,
        horizon_end=horizon_end,
        security_bars=tuple(future),
        trading_calendar_bars=trading_calendar_bars,
    )
    if not coverage.complete:
        return replace(
            common,
            classification="unavailable",
            unavailable_reason="incomplete_outcome_window",
            provenance={**provenance, **coverage.provenance},
        )
    assert coverage.coverage_bar is not None
    matured_at = _negative_label_maturity(
        series=series,
        entry=entry,
        future=tuple(future),
        horizon_end=horizon_end,
        calendar_bars=tuple(
            bar
            for bar in trading_calendar_bars
            if entry.raw_bar.trading_date < bar.trading_date <= coverage.coverage_bar.trading_date
        ),
    )
    return replace(
        common,
        classification="negative",
        label_matured_at=matured_at,
        provenance={
            **provenance,
            **coverage.provenance,
        },
    )


def build_multibagger_labels(
    session: Session,
    *,
    backtest_run_id: UUID,
    policy: MultibaggerOutcomePolicy,
    research_profile: ProductionResearchProfile,
    outcome_data_cutoff: datetime,
) -> MultibaggerLabelBuildResult:
    """Build immutable labels only after J observations have been frozen."""

    data_cutoff = _utc(outcome_data_cutoff, "outcome_data_cutoff")
    backtest = BacktestRepository(session).get_run(backtest_run_id)
    if backtest is None:
        raise ValueError("source backtest run is unavailable")
    if backtest.status not in {"snapshots_built", "outcomes_built", "completed"}:
        raise ValueError("source backtest observations must be frozen before label construction")
    if (
        backtest.backtest_policy_code != policy.source_backtest_policy_code
        or backtest.backtest_policy_checksum_sha256 != policy.source_backtest_policy_checksum_sha256
    ):
        raise ValueError("source backtest policy binding mismatch")
    if backtest.research_profile_checksum_sha256 != research_profile.checksum_sha256:
        raise ValueError("source backtest research profile binding mismatch")
    dataset_ids = resolve_profile_datasets(session, research_profile)
    market_id = _required_dataset(dataset_ids, "market")
    actions_id = _required_dataset(dataset_ids, "corporate_actions")
    benchmark_id = _required_dataset(dataset_ids, "benchmark")
    securities = tuple(sorted({item.security_id for item in backtest.observations}, key=str))
    source_state = _outcome_source_state_checksum(
        session,
        security_ids=securities,
        market_provider_dataset_id=market_id,
        corporate_action_provider_dataset_id=actions_id,
        benchmark_provider_dataset_id=benchmark_id,
        benchmark_code=research_profile.benchmark_code,
        cutoff=data_cutoff,
    )
    contracts: list[object] = [
        {
            "code": item.code,
            "threshold_multiple": format(item.threshold_multiple, "f"),
            "horizon_calendar_years": item.horizon_calendar_years,
        }
        for item in policy.contracts
    ]
    algorithms: dict[str, object] = {
        "adjusted_market_price": ADJUSTED_MARKET_PRICE_VERSION,
        "maximum_drawdown": policy.maximum_drawdown_algorithm,
        "outcome_window_completeness": policy.outcome_window_completeness_algorithm,
        "positive_maturation": policy.positive_maturation_semantics,
        "negative_maturation": policy.negative_maturation_semantics,
        "label_fingerprint": policy.label_fingerprint_version,
    }
    identity = {
        "label_policy_checksum": policy.checksum_sha256,
        "source_backtest_run_id": str(backtest.id),
        "source_backtest_run_key": backtest.run_key_sha256,
        "outcome_data_cutoff": data_cutoff.isoformat(),
        "market_provider_dataset_id": str(market_id),
        "corporate_action_provider_dataset_id": str(actions_id),
        "benchmark_provider_dataset_id": str(benchmark_id),
        "benchmark_code": research_profile.benchmark_code,
        "source_state_checksum": source_state,
        "contracts": contracts,
        "algorithm_versions": algorithms,
    }
    run_key = canonical_json_sha256(identity)
    repository = HistoricalEvaluationRepository(session)
    label_run, created_run = repository.create_label_run(
        run_key_sha256=run_key,
        label_policy_code=policy.code,
        label_policy_checksum_sha256=policy.checksum_sha256,
        source_backtest_run_id=backtest.id,
        source_backtest_run_key_sha256=backtest.run_key_sha256,
        outcome_data_cutoff=data_cutoff,
        market_provider_dataset_id=market_id,
        corporate_action_provider_dataset_id=actions_id,
        source_state_checksum_sha256=source_state,
        ordered_contracts_json=contracts,
        algorithm_versions_json=algorithms,
        inputs_json=identity,
    )
    if label_run.status == "completed":
        return MultibaggerLabelBuildResult(
            run_id=label_run.id,
            run_key_sha256=label_run.run_key_sha256,
            created_run=False,
            labels_created=0,
            labels_reused=len(label_run.labels),
            summary=label_run.summary_json,
        )
    market_reader = PointInTimeMarketReader(session)
    adjustments = MarketAdjustmentPrimitives(
        market_reader, PointInTimeCorporateActionReader(session)
    )
    trading_calendar_bars = tuple(
        market_reader.benchmark_series_as_of(
            provider_dataset_id=benchmark_id,
            benchmark_code=research_profile.benchmark_code,
            interval=research_profile.market_interval,
            as_of=data_cutoff,
        )
    )
    created = reused = 0
    measurements: list[MultibaggerLabelMeasurement] = []
    for observation in sorted(
        backtest.observations,
        key=lambda item: (item.knowledge_cutoff, str(item.security_id), str(item.id)),
    ):
        knowledge_cutoff = _stored_utc(observation.knowledge_cutoff)
        if data_cutoff <= knowledge_cutoff:
            raise ValueError("outcome data cutoff must be after every observation cutoff")
        series = adjustments.adjusted_market_series_as_of(
            market_provider_dataset_id=market_id,
            corporate_action_provider_dataset_id=actions_id,
            security_id=observation.security_id,
            interval=research_profile.market_interval,
            as_of=data_cutoff,
        )
        entry = market_reader.latest_market_bar_as_of(
            provider_dataset_id=market_id,
            security_id=observation.security_id,
            interval=research_profile.market_interval,
            as_of=knowledge_cutoff,
            on_or_before=knowledge_cutoff.date(),
        )
        listing_valid_to = _terminal_nse_listing_end(session, observation.security_id)
        for contract in policy.contracts:
            measurement = compute_multibagger_label(
                series=series,
                entry_raw_bar=entry,
                contract=contract,
                outcome_data_cutoff=data_cutoff,
                listing_valid_to=listing_valid_to,
                trading_calendar_bars=trading_calendar_bars,
            )
            fingerprint = canonical_json_sha256(
                {
                    "label_run_key": run_key,
                    "backtest_observation_id": str(observation.id),
                    "measurement": _measurement_projection(measurement),
                }
            )
            _, was_created = repository.add_label(
                label_run,
                _label_write(
                    measurement,
                    backtest_observation_id=observation.id,
                    label_fingerprint_sha256=fingerprint,
                    provenance_json={
                        **measurement.provenance,
                        "source_backtest_run_id": str(backtest.id),
                        "source_backtest_observation_id": str(observation.id),
                        "source_research_state_sha256": observation.research_state_sha256,
                        "market_provider_dataset_id": str(market_id),
                        "corporate_action_provider_dataset_id": str(actions_id),
                        "benchmark_provider_dataset_id": str(benchmark_id),
                        "benchmark_code": research_profile.benchmark_code,
                        "source_state_checksum_sha256": source_state,
                        "research_score_or_rank_used": False,
                    },
                ),
            )
            created += int(was_created)
            reused += int(not was_created)
            measurements.append(measurement)
    summary = _summary(measurements, observations=len(backtest.observations))
    repository.complete_label_run(
        label_run,
        summary_json=summary,
        completed_at=data_cutoff,
    )
    session.commit()
    return MultibaggerLabelBuildResult(
        run_id=label_run.id,
        run_key_sha256=run_key,
        created_run=created_run,
        labels_created=created,
        labels_reused=reused,
        summary=summary,
    )


def multibagger_label_summary(run: MultibaggerLabelRun) -> dict[str, object]:
    return {
        "run_id": run.id,
        "run_key_sha256": run.run_key_sha256,
        "source_backtest_run_id": run.source_backtest_run_id,
        "outcome_data_cutoff": run.outcome_data_cutoff,
        "status": run.status,
        "summary": run.summary_json,
    }


def _summary(
    measurements: list[MultibaggerLabelMeasurement], *, observations: int
) -> dict[str, object]:
    contracts: dict[str, dict[str, object]] = {}
    for code in sorted({item.contract_code for item in measurements}):
        rows = [item for item in measurements if item.contract_code == code]
        counts = {
            state: sum(item.classification == state for item in rows)
            for state in ("positive", "negative", "unmatured", "unavailable")
        }
        classified = counts["positive"] + counts["negative"]
        unavailable_reasons: dict[str, int] = {}
        for item in rows:
            if item.unavailable_reason:
                unavailable_reasons[item.unavailable_reason] = (
                    unavailable_reasons.get(item.unavailable_reason, 0) + 1
                )
        entries = [item.entry_trading_date for item in rows if item.entry_trading_date]
        maturities = [item.label_matured_at for item in rows if item.label_matured_at]
        contracts[code] = {
            **counts,
            "coverage_percentage": (
                format(Decimal(classified) * Decimal("100") / Decimal(len(rows)), "f")
                if rows
                else None
            ),
            "unavailable_reasons": dict(sorted(unavailable_reasons.items())),
            "earliest_entry_date": min(entries).isoformat() if entries else None,
            "latest_entry_date": max(entries).isoformat() if entries else None,
            "earliest_maturity_at": min(maturities).isoformat() if maturities else None,
            "latest_maturity_at": max(maturities).isoformat() if maturities else None,
        }
    return {
        "observations_considered": observations,
        "labels": len(measurements),
        "contracts": contracts,
        "prediction_metrics_computed": False,
    }


def _measurement_projection(value: MultibaggerLabelMeasurement) -> dict[str, object]:
    return {
        "contract_code": value.contract_code,
        "classification": value.classification,
        "unavailable_reason": value.unavailable_reason,
        "threshold_multiple": format(value.threshold_multiple, "f"),
        "horizon_calendar_years": value.horizon_calendar_years,
        "entry_trading_date": _date_iso(value.entry_trading_date),
        "adjusted_entry_close": _decimal_text(value.adjusted_entry_close),
        "horizon_end_date": _date_iso(value.horizon_end_date),
        "first_threshold_hit_trading_date": _date_iso(value.first_threshold_hit_trading_date),
        "trading_observations_available": value.trading_observations_available,
        "peak_adjusted_close": _decimal_text(value.peak_adjusted_close),
        "peak_price_multiple": _decimal_text(value.peak_price_multiple),
        "maximum_forward_price_return": _decimal_text(value.maximum_forward_price_return),
        "endpoint_adjusted_close": _decimal_text(value.endpoint_adjusted_close),
        "endpoint_return": _decimal_text(value.endpoint_return),
        "calendar_days_to_threshold": value.calendar_days_to_threshold,
        "trading_observations_to_threshold": value.trading_observations_to_threshold,
        "maximum_drawdown": _decimal_text(value.maximum_drawdown),
        "listing_valid_to": _date_iso(value.listing_valid_to),
        "label_matured_at": (
            value.label_matured_at.isoformat() if value.label_matured_at else None
        ),
        "outcome_data_cutoff": value.outcome_data_cutoff.isoformat(),
        "provenance": value.provenance,
    }


def _label_write(
    value: MultibaggerLabelMeasurement,
    *,
    backtest_observation_id: UUID,
    provenance_json: dict[str, object],
    label_fingerprint_sha256: str,
) -> MultibaggerLabelWrite:
    return MultibaggerLabelWrite(
        backtest_observation_id=backtest_observation_id,
        contract_code=value.contract_code,
        classification=value.classification,
        unavailable_reason=value.unavailable_reason,
        threshold_multiple=value.threshold_multiple,
        horizon_calendar_years=value.horizon_calendar_years,
        entry_trading_date=value.entry_trading_date,
        adjusted_entry_close=value.adjusted_entry_close,
        horizon_end_date=value.horizon_end_date,
        first_threshold_hit_trading_date=value.first_threshold_hit_trading_date,
        trading_observations_available=value.trading_observations_available,
        peak_adjusted_close=value.peak_adjusted_close,
        peak_price_multiple=value.peak_price_multiple,
        maximum_forward_price_return=value.maximum_forward_price_return,
        endpoint_adjusted_close=value.endpoint_adjusted_close,
        endpoint_return=value.endpoint_return,
        calendar_days_to_threshold=value.calendar_days_to_threshold,
        trading_observations_to_threshold=value.trading_observations_to_threshold,
        maximum_drawdown=value.maximum_drawdown,
        listing_valid_to=value.listing_valid_to,
        label_matured_at=value.label_matured_at,
        outcome_data_cutoff=value.outcome_data_cutoff,
        provenance_json=provenance_json,
        label_fingerprint_sha256=label_fingerprint_sha256,
    )


def _provenance(
    series: AdjustedMarketSeries,
    entry: AdjustedMarketBar,
    future: list[AdjustedMarketBar],
) -> dict[str, object]:
    bars = [entry, *future]
    actions = {
        adjustment.action.id: adjustment.action
        for bar in bars
        for adjustment in bar.applied_adjustments
    }
    return {
        "entry_price_bar_id": str(entry.raw_bar.id),
        "future_price_bar_ids": [str(item.raw_bar.id) for item in future],
        "market_sources": [
            {
                "price_bar_id": str(bar.raw_bar.id),
                "source_record_id": str(bar.raw_bar.source_record.id),
                "source_content_sha256": bar.raw_bar.source_record.content_sha256,
            }
            for bar in bars
        ],
        "corporate_action_ids_used": [str(identifier) for identifier in sorted(actions, key=str)],
        "corporate_action_sources": [
            {
                "corporate_action_id": str(identifier),
                "source_record_id": str(actions[identifier].source_record.id),
                "source_content_sha256": actions[identifier].source_record.content_sha256,
            }
            for identifier in sorted(actions, key=str)
        ],
        "adjusted_market_price_version": series.algorithm_version,
        "outcome_window_completeness_algorithm": OUTCOME_WINDOW_COMPLETENESS_VERSION,
        "positive_maturation_semantics": (
            "raw_entry_hit_and_intervening_adjustment_evidence_v1"
        ),
        "cash_dividends_included": False,
    }


def _maximum_drawdown(entry: Decimal, future: tuple[Decimal, ...]) -> Decimal:
    peak = entry
    maximum = Decimal("0")
    for value in future:
        if value > peak:
            peak = value
        drawdown = value / peak - Decimal("1")
        if drawdown < maximum:
            maximum = drawdown
    return maximum


def _positive_label_maturity(
    series: AdjustedMarketSeries,
    entry: AdjustedMarketBar,
    hit: AdjustedMarketBar,
) -> tuple[datetime, list[str]]:
    adjustments = tuple(
        adjustment
        for adjustment in series.applicable_adjustments
        if entry.raw_bar.trading_date < adjustment.event_date <= hit.raw_bar.trading_date
    )
    matured_at = max(
        entry.raw_bar.available_at,
        hit.raw_bar.available_at,
        *(adjustment.available_at for adjustment in adjustments),
    )
    return matured_at, [str(item.action.id) for item in adjustments]


def _negative_label_maturity(
    *,
    series: AdjustedMarketSeries,
    entry: AdjustedMarketBar,
    future: tuple[AdjustedMarketBar, ...],
    horizon_end: date,
    calendar_bars: tuple[PointInTimeBenchmarkBar, ...],
) -> datetime:
    adjustments = tuple(
        adjustment
        for adjustment in series.applicable_adjustments
        if entry.raw_bar.trading_date < adjustment.event_date <= horizon_end
    )
    return max(
        datetime.combine(horizon_end, time.max, tzinfo=UTC),
        entry.raw_bar.available_at,
        *(bar.raw_bar.available_at for bar in future),
        *(adjustment.available_at for adjustment in adjustments),
        *(bar.available_at for bar in calendar_bars),
    )


def _outcome_window_coverage(
    *,
    entry_date: date,
    horizon_end: date,
    security_bars: tuple[AdjustedMarketBar, ...],
    trading_calendar_bars: tuple[PointInTimeBenchmarkBar, ...],
) -> _OutcomeWindowCoverage:
    calendar_dates = [bar.trading_date for bar in trading_calendar_bars]
    duplicate_calendar_dates = len(calendar_dates) != len(set(calendar_dates))
    coverage_bar = next(
        (bar for bar in trading_calendar_bars if bar.trading_date >= horizon_end),
        None,
    )
    expected_dates = tuple(
        item
        for item in calendar_dates
        if entry_date < item <= horizon_end
    )
    security_dates = {bar.raw_bar.trading_date for bar in security_bars}
    missing_dates = tuple(item for item in expected_dates if item not in security_dates)
    complete = (
        not duplicate_calendar_dates
        and coverage_bar is not None
        and bool(expected_dates)
        and not missing_dates
    )
    provenance: dict[str, object] = {
        "outcome_window_completeness_algorithm": OUTCOME_WINDOW_COMPLETENESS_VERSION,
        "expected_trading_dates_count": len(expected_dates),
        "expected_trading_dates_checksum_sha256": canonical_json_sha256(
            [item.isoformat() for item in expected_dates]
        ),
        "missing_trading_dates": [item.isoformat() for item in missing_dates],
        "duplicate_calendar_dates": duplicate_calendar_dates,
        "calendar_coverage_bar_id": str(coverage_bar.id) if coverage_bar else None,
        "calendar_coverage_source_record_id": (
            str(coverage_bar.source_record.id) if coverage_bar else None
        ),
        "calendar_coverage_source_content_sha256": (
            coverage_bar.source_record.content_sha256 if coverage_bar else None
        ),
    }
    return _OutcomeWindowCoverage(
        complete=complete,
        expected_dates=expected_dates,
        missing_dates=missing_dates,
        coverage_bar=coverage_bar,
        provenance=provenance,
    )


def _add_calendar_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _unavailable(
    contract: MultibaggerContract,
    cutoff: datetime,
    reason: str,
    *,
    entry: AdjustedMarketBar | None = None,
    horizon_end: date | None = None,
    listing_valid_to: date | None = None,
    provenance: dict[str, object] | None = None,
) -> MultibaggerLabelMeasurement:
    return MultibaggerLabelMeasurement(
        contract_code=contract.code,
        classification="unavailable",
        unavailable_reason=reason,
        threshold_multiple=contract.threshold_multiple,
        horizon_calendar_years=contract.horizon_calendar_years,
        entry_trading_date=entry.raw_bar.trading_date if entry else None,
        adjusted_entry_close=entry.adjusted_close if entry else None,
        horizon_end_date=horizon_end,
        first_threshold_hit_trading_date=None,
        trading_observations_available=0,
        peak_adjusted_close=None,
        peak_price_multiple=None,
        maximum_forward_price_return=None,
        endpoint_adjusted_close=None,
        endpoint_return=None,
        calendar_days_to_threshold=None,
        trading_observations_to_threshold=None,
        maximum_drawdown=None,
        listing_valid_to=listing_valid_to,
        label_matured_at=None,
        outcome_data_cutoff=cutoff,
        provenance=provenance or {},
    )


def _terminal_nse_listing_end(session: Session, security_id: UUID) -> date | None:
    rows = list(
        session.scalars(
            select(ExchangeListing).where(
                ExchangeListing.security_id == security_id,
                ExchangeListing.exchange == "NSE",
            )
        )
    )
    if any(row.valid_to is None for row in rows):
        return None
    valid_to = [row.valid_to for row in rows if row.valid_to is not None]
    return max(valid_to) if valid_to else None


def _outcome_source_state_checksum(
    session: Session,
    *,
    security_ids: tuple[UUID, ...],
    market_provider_dataset_id: UUID,
    corporate_action_provider_dataset_id: UUID,
    benchmark_provider_dataset_id: UUID,
    benchmark_code: str,
    cutoff: datetime,
) -> str:
    prices = session.execute(
        select(PriceBar, SourceRecord)
        .join(SourceRecord, PriceBar.source_record_id == SourceRecord.id)
        .where(
            PriceBar.security_id.in_(security_ids),
            PriceBar.available_at <= cutoff,
            SourceRecord.provider_dataset_id == market_provider_dataset_id,
            SourceRecord.validation_status == "accepted",
        )
    )
    actions = session.execute(
        select(CorporateAction, SourceRecord)
        .join(SourceRecord, CorporateAction.source_record_id == SourceRecord.id)
        .where(
            CorporateAction.security_id.in_(security_ids),
            CorporateAction.available_at <= cutoff,
            CorporateAction.provider_dataset_id == corporate_action_provider_dataset_id,
            SourceRecord.validation_status == "accepted",
        )
    )
    benchmarks = session.execute(
        select(BenchmarkBar, BenchmarkSeries, SourceRecord)
        .join(BenchmarkSeries, BenchmarkBar.benchmark_series_id == BenchmarkSeries.id)
        .join(SourceRecord, BenchmarkBar.source_record_id == SourceRecord.id)
        .where(
            BenchmarkSeries.provider_dataset_id == benchmark_provider_dataset_id,
            BenchmarkSeries.code == benchmark_code,
            BenchmarkBar.available_at <= cutoff,
            SourceRecord.validation_status == "accepted",
        )
    )
    projection = {
        "prices": sorted(
            (
                str(price.id),
                str(price.security_id),
                price.trading_date.isoformat(),
                str(price.close_price),
                _stored_utc(price.available_at).isoformat(),
                _stored_utc(price.revision_at).isoformat() if price.revision_at else None,
                str(source.id),
                source.content_sha256,
            )
            for price, source in prices
        ),
        "actions": sorted(
            (
                str(action.id),
                str(action.security_id),
                action.action_type,
                action.ex_date.isoformat() if action.ex_date else None,
                action.effective_date.isoformat() if action.effective_date else None,
                action.ratio_numerator,
                action.ratio_denominator,
                _stored_utc(action.available_at).isoformat(),
                _stored_utc(action.revision_at).isoformat() if action.revision_at else None,
                str(source.id),
                source.content_sha256,
            )
            for action, source in actions
        ),
        "benchmark_calendar": sorted(
            (
                str(bar.id),
                str(series.id),
                series.code,
                bar.trading_date.isoformat(),
                _stored_utc(bar.available_at).isoformat(),
                _stored_utc(bar.revision_at).isoformat() if bar.revision_at else None,
                str(source.id),
                source.content_sha256,
            )
            for bar, series, source in benchmarks
        ),
    }
    return canonical_json_sha256(projection)


def _required_dataset(values: dict[str, UUID | None], domain: str) -> UUID:
    value = values.get(domain)
    if value is None:
        raise ValueError(f"multibagger labels require provider dataset domain: {domain}")
    return value


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _date_iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
