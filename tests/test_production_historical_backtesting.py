"""Production J strict knowledge-time historical evaluation contracts."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session

from inflector_data.backtest_outcomes import compute_forward_outcome
from inflector_data.backtest_policy import (
    canonical_json_sha256,
    load_backtest_policy,
    load_historical_availability_manifest,
)
from inflector_data.corporate_action_pit import PointInTimeCorporateAction
from inflector_data.financial_endpoint import (
    FinancialEndpointResolver,
    load_financial_endpoint_policy,
)
from inflector_data.historical_dataset import (
    HistoricalUniverseMember,
    ObservedPitUniverse,
    _default_research_runner,
    calendar_month_end_cutoffs,
    normalize_symbols,
    symbol_set_checksum,
)
from inflector_data.market_adjustments import (
    AdjustedMarketSeries,
    MarketAdjustmentPrimitives,
)
from inflector_data.market_pit import (
    BenchmarkSeriesView,
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)
from inflector_data.pit import PointInTimeFinancialReader, SourceRecordView
from inflector_data.production_research import ProductionSecurityContext
from inflector_database.backtest_repository import (
    BacktestObservationWrite,
    BacktestRepository,
)
from inflector_database.models import (
    Company,
    DataProvider,
    ExchangeListing,
    FinancialFact,
    FinancialFiling,
    FinancialMetricDefinition,
    FiscalPeriod,
    IngestionRun,
    ModelVersion,
    PriceBar,
    ProviderDataset,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
POLICY = ROOT / "config/backtest/production_backtest_v1.json"
MANIFEST = ROOT / "config/backtest/production_historical_availability_v1.json"
ENDPOINT_POLICY = ROOT / "config/financial/production_financial_endpoint_policy_v1.json"
T1 = datetime(2026, 6, 30, 18, tzinfo=UTC)
T2 = datetime(2026, 8, 15, 18, tzinfo=UTC)
T3 = datetime(2026, 10, 3, 18, tzinfo=UTC)


def _dataset(session: Session, code: str) -> tuple[ProviderDataset, IngestionRun]:
    provider = session.scalar(select(DataProvider).where(DataProvider.code == "nse_official"))
    if provider is None:
        provider = DataProvider(
            code="nse_official",
            provider_type="https",
            licence_name="fictional-reviewed",
            enabled=True,
        )
        session.add(provider)
        session.flush()
    dataset = ProviderDataset(
        provider_id=provider.id,
        code=code,
        licence_class="fictional-reviewed",
        redistributable=False,
    )
    session.add(dataset)
    session.flush()
    run = IngestionRun(
        provider_dataset_id=dataset.id,
        status="completed",
        started_at=T1,
        finished_at=T1,
        records_received=0,
        records_accepted=0,
        records_quarantined=0,
        records_duplicated=0,
    )
    session.add(run)
    session.flush()
    return dataset, run


def _identity(
    session: Session, *, created_at: datetime = T1
) -> tuple[Company, Security, ExchangeListing]:
    company = Company(
        legal_name=f"Fictional {uuid4()} Limited",
        display_name="Fictional Limited",
        sector="Industrials",
        industry="Equipment",
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(company)
    session.flush()
    security = Security(
        company_id=company.id,
        isin=f"IN{uuid4().hex[:10].upper()}",
        security_type="equity",
        status="active",
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(security)
    session.flush()
    listing = ExchangeListing(
        security_id=security.id,
        exchange="NSE",
        symbol="AAA",
        valid_from=date(2026, 1, 1),
        status="active",
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(listing)
    session.flush()
    return company, security, listing


def _source(
    session: Session,
    *,
    dataset: ProviderDataset,
    run: IngestionRun,
    suffix: str,
    available_at: datetime,
) -> SourceRecord:
    digest = (suffix.encode().hex() + "0" * 64)[:64]
    value = SourceRecord(
        ingestion_run_id=run.id,
        provider_dataset_id=dataset.id,
        external_record_id=f"source-{suffix}",
        source_uri=f"https://nsearchives.nseindia.com/{suffix}",
        raw_object_key=f"fixture/{suffix}",
        raw_content_sha256=digest,
        content_sha256=digest,
        retrieved_at=available_at,
        available_at=available_at,
        parse_status="parsed",
        validation_status="accepted",
    )
    session.add(value)
    session.flush()
    return value


def _financial_fact(
    session: Session,
    *,
    dataset: ProviderDataset,
    run: IngestionRun,
    company: Company,
    metric: FinancialMetricDefinition,
    period: FiscalPeriod,
    available_at: datetime,
    value: Decimal,
    suffix: str,
    restatement: bool = False,
) -> FinancialFact:
    filing = FinancialFiling(
        company_id=company.id,
        provider_dataset_id=dataset.id,
        external_filing_id=f"filing-{suffix}",
        filing_type="integrated_financial_results_xbrl",
        filing_scope="consolidated",
        is_restatement=restatement,
        available_at=available_at,
        revision_at=available_at if restatement else None,
    )
    source = _source(
        session,
        dataset=dataset,
        run=run,
        suffix=suffix,
        available_at=available_at,
    )
    session.add(filing)
    session.flush()
    fact = FinancialFact(
        filing_id=filing.id,
        fiscal_period_id=period.id,
        metric_definition_id=metric.id,
        source_record_id=source.id,
        reported_value=value,
        reported_unit="INR",
        reported_scale="ones",
        reported_currency="INR",
        normalized_value=value,
        normalized_unit="INR",
        available_at=available_at,
        revision_at=available_at if restatement else None,
    )
    session.add(fact)
    session.flush()
    return fact


def test_policy_manifest_checksums_and_strict_classifications(tmp_path: Path) -> None:
    policy = load_backtest_policy(POLICY, repository_root=ROOT)
    manifest = load_historical_availability_manifest(MANIFEST)
    assert policy.mode == "strict_knowledge_time"
    assert policy.forward_return_horizons == (21, 63, 126, 252)
    assert policy.entry_observation_rule == (
        "latest_pit_visible_market_bar_on_or_before_cutoff"
    )
    assert policy.forward_observation_rule == (
        "nth_subsequent_security_trading_observation_strictly_after_cutoff_date"
    )
    assert policy.cash_dividend_treatment == "excluded_price_return_only"
    assert policy.benchmark_code == "NIFTY 50"
    assert manifest.checksum_sha256 == policy.historical_availability_manifest_checksum_sha256
    assert {rule.classification for rule in manifest.classifications.values()} <= {
        "A",
        "B",
        "C",
        "D",
    }
    assert manifest.classifications["prices"].classification == "B"
    assert manifest.classifications["analyst_attention"].classification == "D"
    assert manifest.classifications["market_cap"].classification == "C"

    reordered = dict(reversed(tuple(json.loads(POLICY.read_text()).items())))
    copy = tmp_path / "policy.json"
    copy.write_text(json.dumps(reordered), encoding="utf-8")
    assert (
        load_backtest_policy(copy, repository_root=ROOT).checksum_sha256 == policy.checksum_sha256
    )
    changed = dict(reordered)
    changed["maximum_symbols"] = 24
    assert canonical_json_sha256(changed) != policy.checksum_sha256


def test_cutoff_and_symbol_identity_are_deterministic() -> None:
    cutoffs = calendar_month_end_cutoffs(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 31, 23, 59, 59, 999999, tzinfo=UTC),
    )
    assert [item.date() for item in cutoffs] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
    ]
    symbols = normalize_symbols([" aaa ", "BBB", "AAA", "CCC"], maximum=3)
    assert symbols == ("AAA", "BBB", "CCC")
    assert symbol_set_checksum(symbols) == symbol_set_checksum(symbols)
    assert symbol_set_checksum(("BBB", "AAA", "CCC")) != symbol_set_checksum(symbols)


def test_financial_future_fact_restatement_and_latest_partial_are_pit_safe(
    session: Session,
) -> None:
    dataset, run = _dataset(session, "nse_integrated_financials_xbrl")
    company, _, _ = _identity(session)
    metric = FinancialMetricDefinition(
        code="operating_revenue",
        statement_kind="income",
        unit_category="monetary",
        semantic_type="duration",
    )
    q1 = FiscalPeriod(
        company_id=company.id,
        period_kind="quarter",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        fiscal_year=2026,
        fiscal_quarter=1,
        is_ytd=False,
    )
    q2 = FiscalPeriod(
        company_id=company.id,
        period_kind="quarter",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 9, 30),
        fiscal_year=2026,
        fiscal_quarter=2,
        is_ytd=False,
    )
    session.add_all((metric, q1, q2))
    session.flush()
    _financial_fact(
        session,
        dataset=dataset,
        run=run,
        company=company,
        metric=metric,
        period=q1,
        available_at=T1,
        value=Decimal("100"),
        suffix="q1-original",
    )
    _financial_fact(
        session,
        dataset=dataset,
        run=run,
        company=company,
        metric=metric,
        period=q1,
        available_at=T3,
        value=Decimal("110"),
        suffix="q1-restatement",
        restatement=True,
    )
    _financial_fact(
        session,
        dataset=dataset,
        run=run,
        company=company,
        metric=metric,
        period=q2,
        available_at=T2,
        value=Decimal("120"),
        suffix="q2-partial-only",
    )
    reader = PointInTimeFinancialReader(session)
    before = reader.financial_fact_as_of(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        fiscal_period_id=q1.id,
        filing_scope="consolidated",
        metric_code="operating_revenue",
        as_of=T2,
    )
    after = reader.financial_fact_as_of(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        fiscal_period_id=q1.id,
        filing_scope="consolidated",
        metric_code="operating_revenue",
        as_of=T3,
    )
    assert before is not None and before.normalized_value == Decimal("100")
    assert after is not None and after.normalized_value == Decimal("110")
    resolver = FinancialEndpointResolver(session, load_financial_endpoint_policy(ENDPOINT_POLICY))
    before_q2 = resolver.resolve(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        filing_scope_priority=("consolidated", "standalone"),
        knowledge_cutoff=T1 + timedelta(days=1),
    )
    after_q2 = resolver.resolve(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        filing_scope_priority=("consolidated", "standalone"),
        knowledge_cutoff=T3 - timedelta(days=1),
    )
    assert before_q2.fiscal_quarter == 1
    assert after_q2.fiscal_quarter == 2


def test_future_market_bar_is_invisible_to_research_but_available_to_later_outcome(
    session: Session,
) -> None:
    dataset, run = _dataset(session, "nse_cash_market_udiff_daily")
    _, security, _ = _identity(session)
    for index, (trading_date, available) in enumerate(
        ((date(2026, 6, 30), T1), (date(2026, 7, 1), T2)), start=1
    ):
        source = _source(
            session,
            dataset=dataset,
            run=run,
            suffix=f"bar-{index}",
            available_at=available,
        )
        session.add(
            PriceBar(
                security_id=security.id,
                source_record_id=source.id,
                trading_date=trading_date,
                interval="1d",
                open_price=100,
                high_price=100,
                low_price=100,
                close_price=100,
                volume=1000,
                available_at=available,
            )
        )
    session.flush()
    reader = PointInTimeMarketReader(session)
    assert (
        len(
            reader.market_series_as_of(
                provider_dataset_id=dataset.id,
                security_id=security.id,
                interval="1d",
                as_of=T1,
            )
        )
        == 1
    )
    assert (
        len(
            reader.market_series_as_of(
                provider_dataset_id=dataset.id,
                security_id=security.id,
                interval="1d",
                as_of=T2,
            )
        )
        == 2
    )


def test_observed_universe_never_backprojects_persisted_identity(session: Session) -> None:
    dataset, run = _dataset(session, "nse_cash_market_udiff_daily")
    _, security, _ = _identity(session, created_at=T2)
    source = _source(session, dataset=dataset, run=run, suffix="universe-bar", available_at=T2)
    session.add(
        PriceBar(
            security_id=security.id,
            source_record_id=source.id,
            trading_date=date(2026, 6, 30),
            interval="1d",
            open_price=100,
            high_price=100,
            low_price=100,
            close_price=100,
            volume=1,
            available_at=T2,
        )
    )
    session.flush()
    universe = ObservedPitUniverse(session, dataset.id)
    assert universe.resolve(symbol="AAA", cutoff=T1) is None
    assert universe.resolve(symbol="AAA", cutoff=T3) is not None


def test_observed_universe_uses_historical_listing_interval_not_current_status(
    session: Session,
) -> None:
    dataset, run = _dataset(session, "nse_cash_market_udiff_daily")

    def add_candidate(
        *, symbol: str, valid_to: date, created_at: datetime, suffix: str
    ) -> HistoricalUniverseMember:
        company, security, listing = _identity(session, created_at=created_at)
        listing.symbol = symbol
        listing.valid_to = valid_to
        listing.status = "inactive"
        source = _source(
            session,
            dataset=dataset,
            run=run,
            suffix=suffix,
            available_at=created_at,
        )
        session.add(
            PriceBar(
                security_id=security.id,
                source_record_id=source.id,
                trading_date=date(2026, 6, 29),
                interval="1d",
                open_price=100,
                high_price=100,
                low_price=100,
                close_price=100,
                volume=1,
                available_at=created_at,
            )
        )
        session.flush()
        return HistoricalUniverseMember(
            symbol=symbol,
            company_id=company.id,
            security_id=security.id,
            listing_id=listing.id,
        )

    historically_valid = add_candidate(
        symbol="ENDED",
        valid_to=date(2026, 7, 15),
        created_at=T1 - timedelta(days=1),
        suffix="ended-listing",
    )
    add_candidate(
        symbol="EXPIRED",
        valid_to=date(2026, 6, 1),
        created_at=T1 - timedelta(days=1),
        suffix="expired-listing",
    )
    add_candidate(
        symbol="LATE",
        valid_to=date(2026, 7, 15),
        created_at=T2,
        suffix="late-identity",
    )
    universe = ObservedPitUniverse(session, dataset.id)
    resolved = universe.resolve(symbol="ENDED", cutoff=T1)
    assert resolved == historically_valid
    assert universe.resolve(symbol="EXPIRED", cutoff=T1) is None
    assert universe.resolve(symbol="LATE", cutoff=T1) is None


def test_historical_runner_passes_resolved_ended_listing_to_existing_auto_path(
    session: Session, monkeypatch
) -> None:
    company, security, listing = _identity(session, created_at=T1 - timedelta(days=1))
    listing.valid_to = date(2026, 7, 15)
    listing.status = "inactive"
    session.flush()
    captured: dict[str, object] = {}

    def fake_auto(*_args, **kwargs):
        captured.update(kwargs)
        return {"status": "completed"}

    monkeypatch.setattr("inflector_data.historical_dataset._run_symbol_auto", fake_auto)
    result = _default_research_runner(
        session,
        argparse.Namespace(),
        "AAA",
        ROOT,
        HistoricalUniverseMember(
            symbol="AAA",
            company_id=company.id,
            security_id=security.id,
            listing_id=listing.id,
        ),
    )
    assert result == {"status": "completed"}
    identity = cast(ProductionSecurityContext, captured["identity_override"])
    assert identity.security.id == security.id
    assert identity.listing.id == listing.id
    assert identity.listing.valid_to == date(2026, 7, 15)


def test_outcome_entry_is_exact_latest_pit_visible_bar_not_later_download(
    session: Session,
) -> None:
    dataset, run = _dataset(session, "nse_cash_market_udiff_daily")
    historical_cutoff = datetime(2024, 7, 31, 23, 59, 59, tzinfo=UTC)
    _, security, _ = _identity(
        session, created_at=datetime(2024, 1, 1, tzinfo=UTC)
    )
    rows = [
        (date(2024, 7, 30), datetime(2024, 7, 30, 18, tzinfo=UTC), "100", "entry"),
        (date(2024, 7, 31), T3, "999", "downloaded-years-later"),
    ]
    rows.extend(
        (date(2024, 8, day), T3, "110", f"future-{day}") for day in range(1, 22)
    )
    for trading_date, available_at, close, suffix in rows:
        source = _source(
            session,
            dataset=dataset,
            run=run,
            suffix=suffix,
            available_at=available_at,
        )
        session.add(
            PriceBar(
                security_id=security.id,
                source_record_id=source.id,
                trading_date=trading_date,
                interval="1d",
                open_price=Decimal(close),
                high_price=Decimal(close),
                low_price=Decimal(close),
                close_price=Decimal(close),
                volume=1,
                available_at=available_at,
            )
        )
    session.flush()
    reader = PointInTimeMarketReader(session)
    entry = reader.latest_market_bar_as_of(
        provider_dataset_id=dataset.id,
        security_id=security.id,
        interval="1d",
        as_of=historical_cutoff,
        on_or_before=historical_cutoff.date(),
    )
    assert entry is not None and entry.trading_date == date(2024, 7, 30)
    outcome_bars = reader.market_series_as_of(
        provider_dataset_id=dataset.id,
        security_id=security.id,
        interval="1d",
        as_of=T3,
    )
    adjusted = tuple(
        MarketAdjustmentPrimitives._adjusted_bar(
            raw_bar=item,
            applicable_adjustments=(),
            basis_date=outcome_bars[-1].trading_date,
            as_of=T3,
        )
        for item in outcome_bars
    )
    series = AdjustedMarketSeries(
        market_provider_dataset_id=dataset.id,
        corporate_action_provider_dataset_id=uuid4(),
        security_id=security.id,
        interval="1d",
        adjustment_basis_date=outcome_bars[-1].trading_date,
        bars=adjusted,
        selected_actions=(),
        applicable_adjustments=(),
        as_of=T3,
    )
    outcome = compute_forward_outcome(
        series=series,
        benchmark_bars=(),
        entry_raw_bar=entry,
        knowledge_cutoff=historical_cutoff,
        horizon=21,
        listing_valid_to=None,
    )
    assert outcome.entry_trading_date == date(2024, 7, 30)
    assert outcome.entry_adjusted_close == Decimal("100")
    assert outcome.exit_trading_date == date(2024, 8, 21)
    assert outcome.security_return == Decimal("0.1")
    assert outcome.provenance["entry_price_bar_id"] == str(entry.id)


def test_split_adjusted_price_return_and_missing_horizons() -> None:
    dataset_id, security_id, action_id = uuid4(), uuid4(), uuid4()
    source = SourceRecordView(
        id=uuid4(),
        external_record_id="bar",
        source_uri="fixture://bar",
        raw_object_key="bar",
        raw_payload_reference=None,
        content_sha256="0" * 64,
        validation_status="accepted",
    )

    def raw(identifier: UUID, when: date, close: str) -> PointInTimeMarketBar:
        return PointInTimeMarketBar(
            id=identifier,
            provider_dataset_id=dataset_id,
            security_id=security_id,
            trading_date=when,
            interval="1d",
            open_price=Decimal(close),
            high_price=Decimal(close),
            low_price=Decimal(close),
            close_price=Decimal(close),
            volume=1,
            market_cap=None,
            delivery_quantity=None,
            delivery_percentage=None,
            available_at=T3,
            revision_at=None,
            ingested_at=T3,
            source_record=source,
        )

    action = PointInTimeCorporateAction(
        id=action_id,
        provider_dataset_id=uuid4(),
        security_id=security_id,
        action_type="split",
        announcement_date=None,
        ex_date=date(2026, 7, 1),
        record_date=None,
        effective_date=date(2026, 7, 1),
        ratio_numerator=2,
        ratio_denominator=1,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
        subscription_price=None,
        subscription_currency=None,
        exchange="NSE",
        old_symbol=None,
        new_symbol=None,
        successor_isin=None,
        available_at=T3,
        revision_at=None,
        ingested_at=T3,
        source_record=source,
    )
    adjustment = MarketAdjustmentPrimitives._price_adjustment(action)
    raw_bars = [raw(uuid4(), date(2026, 6, 30), "100")]
    raw_bars.extend(raw(uuid4(), date(2026, 7, day), "50") for day in range(1, 31))
    adjusted = tuple(
        MarketAdjustmentPrimitives._adjusted_bar(
            raw_bar=item,
            applicable_adjustments=(adjustment,),
            basis_date=date(2026, 7, 30),
            as_of=T3,
        )
        for item in raw_bars
    )
    series = AdjustedMarketSeries(
        market_provider_dataset_id=dataset_id,
        corporate_action_provider_dataset_id=action.provider_dataset_id,
        security_id=security_id,
        interval="1d",
        adjustment_basis_date=date(2026, 7, 30),
        bars=adjusted,
        selected_actions=(action,),
        applicable_adjustments=(adjustment,),
        as_of=T3,
    )
    benchmark_identity = BenchmarkSeriesView(
        id=uuid4(),
        provider_dataset_id=uuid4(),
        code="NIFTY 50",
        display_name="Nifty 50",
        currency="INR",
    )
    benchmarks = tuple(
        PointInTimeBenchmarkBar(
            id=uuid4(),
            benchmark_series=benchmark_identity,
            trading_date=item.raw_bar.trading_date,
            interval="1d",
            open_value=Decimal("100"),
            high_value=Decimal("100"),
            low_value=Decimal("100"),
            close_value=Decimal("100"),
            available_at=T3,
            revision_at=None,
            ingested_at=T3,
            source_record=source,
        )
        for item in adjusted
    )
    available = compute_forward_outcome(
        series=series,
        benchmark_bars=benchmarks,
        entry_raw_bar=raw_bars[0],
        knowledge_cutoff=T1,
        horizon=21,
        listing_valid_to=None,
    )
    missing = compute_forward_outcome(
        series=series,
        benchmark_bars=benchmarks,
        entry_raw_bar=raw_bars[0],
        knowledge_cutoff=T1,
        horizon=63,
        listing_valid_to=None,
    )
    delisted = compute_forward_outcome(
        series=series,
        benchmark_bars=benchmarks,
        entry_raw_bar=raw_bars[0],
        knowledge_cutoff=T1,
        horizon=63,
        listing_valid_to=date(2026, 7, 31),
    )
    assert available.status == "available"
    assert available.security_return == Decimal("0")
    assert missing.status == "insufficient_future_history"
    assert missing.security_return is None
    assert delisted.status == "outcome_unavailable_due_to_delisting"


def test_backtest_run_identity_and_repository_are_idempotent(session: Session) -> None:
    model = ModelVersion(
        model_family="fictional_v5",
        semantic_version="5",
        git_sha="a" * 40,
        status="active",
    )
    session.add(model)
    session.flush()
    configuration = ScoringConfiguration(
        model_version_id=model.id,
        configuration_name="nse_current_research_v4",
        configuration_version="4",
        status="active",
        configuration_json={},
        checksum_sha256="b" * 64,
    )
    session.add(configuration)
    session.flush()
    repository = BacktestRepository(session)
    first, created = _create_test_run(
        repository,
        configuration=configuration,
        run_key_sha256="c" * 64,
        cutoff_end=T3,
    )
    second, created_again = _create_test_run(
        repository,
        configuration=configuration,
        run_key_sha256="c" * 64,
        cutoff_end=T3,
    )
    assert created is True and created_again is False and second.id == first.id
    third, _ = _create_test_run(
        repository,
        configuration=configuration,
        run_key_sha256="5" * 64,
        cutoff_end=T3 + timedelta(days=1),
    )
    assert third.id != first.id


def _create_test_run(
    repository: BacktestRepository,
    *,
    configuration: ScoringConfiguration,
    run_key_sha256: str,
    cutoff_end: datetime,
):
    return repository.create_run(
        run_key_sha256=run_key_sha256,
        backtest_policy_code="production_backtest_v1",
        backtest_policy_checksum_sha256="d" * 64,
        availability_manifest_code="production_historical_availability_v1",
        availability_manifest_checksum_sha256="e" * 64,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256="f" * 64,
        financial_primitive_policy_checksum_sha256="1" * 64,
        financial_endpoint_policy_checksum_sha256="2" * 64,
        source_state_checksum_sha256="3" * 64,
        model_family="fictional_v5",
        cutoff_start=T1,
        cutoff_end=cutoff_end,
        cutoff_cadence="calendar_month_end_utc",
        universe_policy="observed_pit_universe",
        benchmark_code="NIFTY 50",
        return_horizons=(21, 63, 126, 252),
        ordered_symbols=("AAA",),
        symbol_set_checksum_sha256="4" * 64,
        inputs_json={"stable": True},
    )


def test_partial_v5_snapshot_is_retained_without_synthetic_score(session: Session) -> None:
    company, security, _ = _identity(session)
    model = ModelVersion(
        model_family="partial_v5",
        semantic_version="5",
        git_sha="a" * 40,
        status="active",
    )
    session.add(model)
    session.flush()
    configuration = ScoringConfiguration(
        model_version_id=model.id,
        configuration_name="nse_current_research_v4",
        configuration_version="4",
        status="active",
        configuration_json={},
        checksum_sha256="b" * 64,
    )
    session.add(configuration)
    session.flush()
    snapshot = ScoreSnapshot(
        company_id=company.id,
        model_version_id=model.id,
        scoring_configuration_id=configuration.id,
        configuration_checksum_sha256=configuration.checksum_sha256,
        as_of_date=T1.date(),
        knowledge_cutoff=T1,
        ending_fiscal_year=2026,
        ending_fiscal_quarter=1,
        selected_filing_scope="consolidated",
        selected_security_id=security.id,
        snapshot_status="partial_component_set",
        eligibility_eligible=True,
        eligibility_inputs_json={},
        eligibility_reasons_json=[],
        eligibility_warnings_json=[],
        financial_core_coverage=Decimal("0.5"),
        confidence=Decimal("0.4"),
        confidence_inputs_json={},
        confidence_details_json={},
        top_level_component_weight_coverage=Decimal("0.2"),
        available_component_codes_json=["market_structure"],
        missing_component_codes_json=["valuation"],
        context_resolution_json={},
        input_manifest_json={},
        fingerprint_payload_json={"algorithm_version": "score_snapshot_v5"},
        snapshot_fingerprint_sha256="c" * 64,
        final_score=None,
        algorithm_version="score_snapshot_v5",
    )
    session.add(snapshot)
    session.flush()
    repository = BacktestRepository(session)
    run, _ = repository.create_run(
        run_key_sha256="d" * 64,
        backtest_policy_code="production_backtest_v1",
        backtest_policy_checksum_sha256="e" * 64,
        availability_manifest_code="production_historical_availability_v1",
        availability_manifest_checksum_sha256="f" * 64,
        scoring_configuration_id=configuration.id,
        scoring_configuration_checksum_sha256=configuration.checksum_sha256,
        research_profile_code="nse_current_research_v4",
        research_profile_checksum_sha256="1" * 64,
        financial_primitive_policy_checksum_sha256="2" * 64,
        financial_endpoint_policy_checksum_sha256="3" * 64,
        source_state_checksum_sha256="4" * 64,
        model_family="partial_v5",
        cutoff_start=T1,
        cutoff_end=T1,
        cutoff_cadence="calendar_month_end_utc",
        universe_policy="observed_pit_universe",
        benchmark_code="NIFTY 50",
        return_horizons=(21, 63, 126, 252),
        ordered_symbols=("AAA",),
        symbol_set_checksum_sha256="5" * 64,
        inputs_json={},
    )
    observation, created = repository.add_observation(
        run,
        BacktestObservationWrite(
            company_id=company.id,
            security_id=security.id,
            score_snapshot_id=snapshot.id,
            symbol="AAA",
            knowledge_cutoff=T1,
            observation_status="snapshot_frozen",
            selected_fiscal_year=2026,
            selected_fiscal_quarter=1,
            selected_filing_scope="consolidated",
            selected_period_end=date(2026, 6, 30),
            snapshot_status="partial_component_set",
            snapshot_fingerprint_sha256=snapshot.snapshot_fingerprint_sha256,
            research_state_projection_version="backtest_research_state_projection_v1",
            research_state_sha256="6" * 64,
            research_state_changed=True,
            detail_json={},
        ),
    )
    assert created is True
    assert observation.score_snapshot_id == snapshot.id
    assert snapshot.snapshot_status == "partial_component_set"
    assert snapshot.final_score is None


def test_research_modules_have_no_outcome_dependency() -> None:
    for relative in (
        "packages/data/inflector_data/production_research.py",
        "packages/data/inflector_data/historical_dataset.py",
        "packages/data/inflector_data/score_orchestration.py",
    ):
        text_value = (ROOT / relative).read_text(encoding="utf-8")
        assert "backtest_outcomes" not in text_value
        assert "BacktestOutcome" not in text_value


def test_migration_0018_creates_only_backtest_evaluation_tables(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert {"backtest_runs", "backtest_observations", "backtest_outcomes"} <= tables
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == (
            "20261003_0021"
        )
    engine.dispose()
    command.downgrade(config, "20261002_0017")
    engine = create_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    tables = set(inspect(engine).get_table_names())
    assert not {"backtest_runs", "backtest_observations", "backtest_outcomes"} & tables
    assert "price_bars" in tables and "score_snapshots" in tables
    engine.dispose()
