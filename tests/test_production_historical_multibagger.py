"""Production Q historical-universe and factual outcome-label contracts."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from inflector_data.backtest_cli import _execute, _json_default, _parser
from inflector_data.corporate_action_pit import PointInTimeCorporateAction
from inflector_data.historical_evaluation_policy import (
    MultibaggerContract,
    canonical_json_sha256,
    load_historical_universe_policy,
    load_multibagger_outcome_policy,
)
from inflector_data.historical_universe import (
    HistoricalUniverseBuildResult,
    HistoricalUniverseDataUnavailableError,
    HistoricalUniverseEvidence,
    build_historical_universe,
    shard_historical_universe,
)
from inflector_data.market_adjustments import AdjustedMarketSeries, MarketAdjustmentPrimitives
from inflector_data.market_pit import (
    BenchmarkSeriesView,
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)
from inflector_data.multibagger_labels import (
    _terminal_nse_listing_end,
    build_multibagger_labels,
    compute_multibagger_label,
)
from inflector_data.pit import SourceRecordView
from inflector_data.research_profile import load_research_profile
from inflector_database.backtest_repository import (
    BacktestObservationWrite,
    BacktestRepository,
)
from inflector_database.historical_evaluation_repository import (
    HistoricalEvaluationIntegrityError,
    HistoricalEvaluationRepository,
    MultibaggerLabelWrite,
)
from inflector_database.models import (
    Company,
    DataProvider,
    ExchangeListing,
    HistoricalUniverseMemberRecord,
    IngestionRun,
    PriceBar,
    ProviderDataset,
    Security,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
UNIVERSE_POLICY = ROOT / "config/backtest/production_historical_universe_v1.json"
LABEL_POLICY = ROOT / "config/backtest/production_multibagger_outcomes_v1.json"
RESEARCH_PROFILE = ROOT / "config/research/production_research_v4.json"
ENTRY_DATE = date(2020, 1, 2)
ENTRY_AT = datetime(2020, 1, 2, 18, tzinfo=UTC)


def _dataset(session: Session) -> tuple[ProviderDataset, IngestionRun]:
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
        code="nse_cm_mii_security_daily",
        licence_class="fictional-reviewed",
        redistributable=False,
    )
    session.add(dataset)
    session.flush()
    return dataset, _ingestion(session, dataset)


def _ingestion(session: Session, dataset: ProviderDataset) -> IngestionRun:
    run = IngestionRun(
        provider_dataset_id=dataset.id,
        status="completed",
        started_at=ENTRY_AT,
        finished_at=ENTRY_AT,
        records_received=0,
        records_accepted=0,
        records_quarantined=0,
        records_duplicated=0,
    )
    session.add(run)
    session.flush()
    return run


def _profile_datasets(session: Session) -> tuple[ProviderDataset, IngestionRun]:
    nse = DataProvider(
        code="nse_official",
        provider_type="https",
        licence_name="fictional-reviewed",
        enabled=True,
    )
    gdelt = DataProvider(
        code="gdelt",
        provider_type="https",
        licence_name="fictional-reviewed",
        enabled=True,
    )
    session.add_all([nse, gdelt])
    session.flush()
    codes = (
        "nse_integrated_financials_xbrl",
        "nse_cash_market_udiff_daily",
        "nse_cash_market_delivery_daily",
        "nse_indices_daily",
        "nse_corporate_actions",
        "nse_corporate_announcements",
    )
    datasets = {
        code: ProviderDataset(
            provider_id=nse.id,
            code=code,
            licence_class="fictional-reviewed",
            redistributable=False,
        )
        for code in codes
    }
    datasets["gdelt_doc_company_news_mentions"] = ProviderDataset(
        provider_id=gdelt.id,
        code="gdelt_doc_company_news_mentions",
        licence_class="fictional-reviewed",
        redistributable=False,
    )
    session.add_all(datasets.values())
    session.flush()
    market = datasets["nse_cash_market_udiff_daily"]
    run = IngestionRun(
        provider_dataset_id=market.id,
        status="completed",
        started_at=ENTRY_AT,
        finished_at=ENTRY_AT,
        records_received=3,
        records_accepted=3,
        records_quarantined=0,
        records_duplicated=0,
    )
    session.add(run)
    session.flush()
    return market, run


def _source(
    session: Session,
    dataset: ProviderDataset,
    run: IngestionRun,
    *,
    suffix: str,
    retrieved_at: datetime = datetime(2026, 10, 4, 12, tzinfo=UTC),
    semantic_row: dict[str, str] | None = None,
) -> SourceRecord:
    token = canonical_json_sha256(suffix).upper()
    symbol = ("F" + token)[:10]
    isin = ("INE" + token.ljust(9, "0"))[:12]
    row = semantic_row or {
        "TradDt": ENTRY_DATE.isoformat(),
        "Sgmt": "CM",
        "FinInstrmTp": "STK",
        "ISIN": isin,
        "TckrSymb": symbol,
        "SctySrs": "EQ",
        "ClsPric": "100",
    }
    digest = canonical_json_sha256(row)
    row_date = date.fromisoformat(row["TradDt"])
    isin = row["ISIN"]
    record = SourceRecord(
        ingestion_run_id=run.id,
        provider_dataset_id=dataset.id,
        external_record_id=f"nse-cm:{row_date.isoformat()}:{isin}:EQ",
        source_uri=f"fixture://nse/{suffix}",
        raw_object_key=f"q/{suffix}",
        raw_content_sha256=digest,
        content_sha256=digest,
        retrieved_at=retrieved_at,
        available_at=retrieved_at,
        parse_status="parsed",
        validation_status="accepted",
    )
    session.add(record)
    session.flush()
    return record


def _mii_source(
    session: Session,
    dataset: ProviderDataset,
    run: IngestionRun,
    *,
    suffix: str,
    snapshot_date: date,
    semantic_row: dict[str, str],
    raw_content_sha256: str | None = None,
) -> SourceRecord:
    digest = canonical_json_sha256(semantic_row)
    filename = f"NSE_CM_security_{snapshot_date.strftime('%d%m%Y')}.csv.gz"
    raw_hash = raw_content_sha256 or canonical_json_sha256(
        {"ingestion_run_id": str(run.id), "filename": filename}
    )
    record = SourceRecord(
        ingestion_run_id=run.id,
        provider_dataset_id=dataset.id,
        external_record_id=(
            f"nse-cm-mii-security:{snapshot_date.isoformat()}:"
            f"{semantic_row['ISIN']}:EQ"
        ),
        source_uri=f"fixture://nse/{filename}",
        raw_object_key=f"q/security-master/{run.id}/{filename}",
        raw_payload_reference=f"{filename}#row-{suffix}",
        raw_content_sha256=raw_hash,
        content_sha256=digest,
        retrieved_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
        available_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
        parse_status="parsed",
        validation_status="accepted",
    )
    session.add(record)
    run.records_received += 1
    run.records_accepted += 1
    session.flush()
    return record


def _semantic_row(
    *,
    trading_date: date,
    symbol: str,
    isin: str,
    series: str = "EQ",
) -> dict[str, str]:
    del trading_date
    return {
        "Sgmt": "CM",
        "FinInstrmTp": "STK",
        "ISIN": isin,
        "TckrSymb": symbol,
        "SctySrs": series,
        "FinInstrmNm": f"Fictional {symbol}",
        "CallAuctnInd": "1",
    }


def _universe_evidence(source: SourceRecord, row: dict[str, str]) -> HistoricalUniverseEvidence:
    return HistoricalUniverseEvidence(source_record_id=source.id, semantic_row=row)


def _identity(
    session: Session,
    *,
    symbol: str,
    isin: str,
    valid_from: date,
    valid_to: date | None = None,
    security_status: str = "active",
    listing_status: str = "active",
) -> tuple[Company, Security, ExchangeListing]:
    company = Company(
        legal_name=f"Fictional {uuid4()} Limited",
        display_name=symbol,
        sector="Industrials",
        industry="Equipment",
        created_at=ENTRY_AT,
        updated_at=ENTRY_AT,
    )
    session.add(company)
    session.flush()
    security = Security(
        company_id=company.id,
        isin=isin,
        security_type="equity",
        status=security_status,
        created_at=ENTRY_AT,
        updated_at=ENTRY_AT,
    )
    session.add(security)
    session.flush()
    listing = ExchangeListing(
        security_id=security.id,
        exchange="NSE",
        symbol=symbol,
        valid_from=valid_from,
        valid_to=valid_to,
        status=listing_status,
        created_at=ENTRY_AT,
        updated_at=ENTRY_AT,
    )
    session.add(listing)
    session.flush()
    return company, security, listing


def _source_view(dataset_id: UUID) -> SourceRecordView:
    return SourceRecordView(
        id=uuid4(),
        external_record_id="q-bar",
        source_uri="fixture://q/bar",
        raw_object_key="q/bar",
        raw_payload_reference=None,
        content_sha256="1" * 64,
        validation_status="accepted",
        provider_dataset_id=dataset_id,
    )


def _raw_bar(
    *,
    identifier: UUID,
    dataset_id: UUID,
    security_id: UUID,
    when: date,
    close: str,
    available_at: datetime | None = None,
) -> PointInTimeMarketBar:
    observed = available_at or datetime.combine(when, datetime.min.time(), tzinfo=UTC)
    value = Decimal(close)
    return PointInTimeMarketBar(
        id=identifier,
        provider_dataset_id=dataset_id,
        security_id=security_id,
        trading_date=when,
        interval="1d",
        open_price=value,
        high_price=value,
        low_price=value,
        close_price=value,
        volume=1,
        market_cap=None,
        delivery_quantity=None,
        delivery_percentage=None,
        available_at=observed,
        revision_at=None,
        ingested_at=observed,
        source_record=_source_view(dataset_id),
    )


def _series(
    values: list[tuple[date, str]],
    *,
    actions: tuple[PointInTimeCorporateAction, ...] = (),
) -> tuple[AdjustedMarketSeries, PointInTimeMarketBar]:
    dataset_id, actions_id, security_id = uuid4(), uuid4(), uuid4()
    raws = [
        _raw_bar(
            identifier=uuid4(),
            dataset_id=dataset_id,
            security_id=security_id,
            when=when,
            close=close,
        )
        for when, close in values
    ]
    adjustments = tuple(
        MarketAdjustmentPrimitives._price_adjustment(action)
        for action in actions
        if action.action_type in {"split", "bonus"}
    )
    basis = raws[-1].trading_date
    adjusted = tuple(
        MarketAdjustmentPrimitives._adjusted_bar(
            raw_bar=bar,
            applicable_adjustments=adjustments,
            basis_date=basis,
            as_of=datetime(2030, 1, 1, tzinfo=UTC),
        )
        for bar in raws
    )
    return (
        AdjustedMarketSeries(
            market_provider_dataset_id=dataset_id,
            corporate_action_provider_dataset_id=actions_id,
            security_id=security_id,
            interval="1d",
            adjustment_basis_date=basis,
            bars=adjusted,
            selected_actions=actions,
            applicable_adjustments=adjustments,
            as_of=datetime(2030, 1, 1, tzinfo=UTC),
        ),
        raws[0],
    )


def _calendar(dates: list[date]) -> tuple[PointInTimeBenchmarkBar, ...]:
    dataset_id = uuid4()
    series = BenchmarkSeriesView(
        id=uuid4(),
        provider_dataset_id=dataset_id,
        code="NIFTY 50",
        display_name="NIFTY 50",
        currency="INR",
    )
    return tuple(
        PointInTimeBenchmarkBar(
            id=uuid4(),
            benchmark_series=series,
            trading_date=when,
            interval="1d",
            open_value=Decimal("1"),
            high_value=Decimal("1"),
            low_value=Decimal("1"),
            close_value=Decimal("1"),
            available_at=datetime.combine(when, datetime.min.time(), tzinfo=UTC),
            revision_at=None,
            ingested_at=datetime.combine(when, datetime.min.time(), tzinfo=UTC),
            source_record=_source_view(dataset_id),
        )
        for when in dates
    )


def _contract(multiple: str, years: int, code: str = "TEST") -> MultibaggerContract:
    return MultibaggerContract(code, Decimal(multiple), years)


def _action(
    action_type: str,
    *,
    effective_date: date = date(2021, 1, 2),
    available_at: datetime = datetime(2021, 1, 2, 18, tzinfo=UTC),
) -> PointInTimeCorporateAction:
    dataset_id, security_id = uuid4(), uuid4()
    return PointInTimeCorporateAction(
        id=uuid4(),
        provider_dataset_id=dataset_id,
        security_id=security_id,
        action_type=action_type,
        announcement_date=None,
        ex_date=effective_date,
        record_date=None,
        effective_date=effective_date,
        ratio_numerator=2 if action_type == "split" else 1,
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
        available_at=available_at,
        revision_at=None,
        ingested_at=available_at,
        source_record=_source_view(dataset_id),
    )


def test_q_policies_are_strict_and_bind_production_j() -> None:
    universe = load_historical_universe_policy(UNIVERSE_POLICY)
    labels = load_multibagger_outcome_policy(LABEL_POLICY, repository_root=ROOT)
    assert universe.code == "production_historical_universe_v1"
    assert universe.current_status_filtering == "forbidden"
    assert universe.source_dataset_code == "nse_cm_mii_security_daily"
    assert universe.complete_member_set_semantics == (
        "exact_accepted_eligible_source_record_set_equality_v1"
    )
    assert universe.semantic_row_verification_version == (
        "nse_cm_mii_security_semantic_row_sha256_v1"
    )
    assert universe.maximum_symbols_per_backtest_shard == 25
    assert [
        (item.code, item.threshold_multiple, item.horizon_calendar_years)
        for item in labels.contracts
    ] == [
        ("MB_2X_2Y", Decimal("2"), 2),
        ("MB_3X_3Y", Decimal("3"), 3),
        ("MB_5X_5Y", Decimal("5"), 5),
    ]
    assert labels.source_backtest_policy_checksum_sha256 == (
        "e1f1f886f686045f7f2774be355674a726b155db1a32c9080999c32becd35984"
    )
    assert labels.outcome_window_completeness_algorithm == (
        "nifty50_session_dates_full_security_bar_coverage_v1"
    )


def test_historical_universe_retains_inactive_delisted_and_late_retrieved_member(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    _, security, _ = _identity(
        session,
        symbol="DEAD",
        isin="INE000000001",
        valid_from=date(2018, 1, 1),
        valid_to=date(2021, 6, 30),
        security_status="inactive",
        listing_status="delisted",
    )
    row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="DEAD", isin=security.isin
    )
    source = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="dead-2020",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row,
    )
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    result = build_historical_universe(
        session,
        policy=policy,
        cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
        source_provider_dataset_id=dataset.id,
        source_ingestion_run_id=ingestion.id,
        evidence=(
            _universe_evidence(source, row),
        ),
        completed_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
    )
    assert result.eligible == 1
    member = session.scalar(select(HistoricalUniverseMemberRecord))
    assert member is not None and member.security_id == security.id
    assert member.provenance_json["research_visibility_claimed"] is False
    assert cast(str, member.provenance_json["source_retrieved_at"]).startswith("2026-")
    assert member.provenance_json["source_raw_object_key"] == source.raw_object_key
    assert member.provenance_json["semantic_row_verification_version"] == (
        "nse_cm_mii_security_semantic_row_sha256_v1"
    )


@pytest.mark.parametrize(
    ("field", "fabricated"),
    [
        ("TckrSymb", "FAKE"),
        ("ISIN", "INE999999998"),
        ("TradDt", "2020-01-30"),
        ("SctySrs", "BE"),
    ],
)
def test_historical_universe_rejects_fabricated_fields_on_valid_source_record(
    session: Session, field: str, fabricated: str
) -> None:
    dataset, ingestion = _dataset(session)
    row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="BOUND", isin="INE000000090"
    )
    source = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="bound",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row,
    )
    tampered = {**row, field: fabricated}
    with pytest.raises(ValueError, match="content hash mismatch"):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(_universe_evidence(source, tampered),),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


def test_historical_universe_rejects_source_content_hash_mismatch(session: Session) -> None:
    dataset, ingestion = _dataset(session)
    row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="HASH", isin="INE000000091"
    )
    source = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="hash",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row,
    )
    source.content_sha256 = "f" * 64
    with pytest.raises(ValueError, match="content hash mismatch"):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(_universe_evidence(source, row),),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


def test_historical_universe_rejects_fabricated_source_date(session: Session) -> None:
    dataset, ingestion = _dataset(session)
    row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="DATE", isin="INE000000094"
    )
    source = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="date",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row,
    )
    source.external_record_id = "nse-cm-mii-security:2020-01-30:INE000000094:EQ"
    with pytest.raises(ValueError, match="artifact filename is not authoritative"):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(_universe_evidence(source, row),),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


def test_historical_universe_requires_latest_supported_cutoff_month_source_date(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    stale_row = _semantic_row(
        trading_date=date(2020, 1, 30), symbol="STALE", isin="INE000000092"
    )
    final_row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="FINAL", isin="INE000000093"
    )
    final_ingestion = _ingestion(session, dataset)
    stale = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="stale",
        snapshot_date=date(2020, 1, 30),
        semantic_row=stale_row,
    )
    final = _mii_source(
        session,
        dataset,
        final_ingestion,
        suffix="final",
        snapshot_date=date(2020, 1, 31),
        semantic_row=final_row,
    )
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    with pytest.raises(ValueError, match="not the latest supported cohort date"):
        build_historical_universe(
            session,
            policy=policy,
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(_universe_evidence(stale, stale_row),),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="in the cutoff month"):
        build_historical_universe(
            session,
            policy=policy,
            cutoff=datetime(2020, 2, 29, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=final_ingestion.id,
            evidence=(_universe_evidence(final, final_row),),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
    result = build_historical_universe(
        session,
        policy=policy,
        cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
        source_provider_dataset_id=dataset.id,
        source_ingestion_run_id=final_ingestion.id,
        evidence=(_universe_evidence(final, final_row),),
        completed_at=datetime(2026, 10, 4, tzinfo=UTC),
    )
    assert result.unresolved_identity == 1


def test_historical_identity_is_isin_first_and_continues_across_symbol_rename(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    _, security, old = _identity(
        session,
        symbol="OLD",
        isin="INE000000002",
        valid_from=date(2018, 1, 1),
        valid_to=date(2020, 6, 30),
    )
    new = ExchangeListing(
        security_id=security.id,
        exchange="NSE",
        symbol="NEW",
        valid_from=date(2020, 7, 1),
        status="active",
        created_at=ENTRY_AT,
        updated_at=ENTRY_AT,
    )
    session.add(new)
    first_row = _semantic_row(
        trading_date=date(2020, 6, 30), symbol="OLD", isin=security.isin
    )
    second_row = _semantic_row(
        trading_date=date(2020, 7, 31), symbol="NEW", isin=security.isin
    )
    second_ingestion = _ingestion(session, dataset)
    first = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="rename-old",
        snapshot_date=date(2020, 6, 30),
        semantic_row=first_row,
    )
    second = _mii_source(
        session,
        dataset,
        second_ingestion,
        suffix="rename-new",
        snapshot_date=date(2020, 7, 31),
        semantic_row=second_row,
    )
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    for cutoff, source_run, source, row, listing in (
        (date(2020, 6, 30), ingestion, first, first_row, old),
        (date(2020, 7, 31), second_ingestion, second, second_row, new),
    ):
        build_historical_universe(
            session,
            policy=policy,
            cutoff=datetime.combine(cutoff, datetime.max.time(), tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=source_run.id,
            evidence=(
                _universe_evidence(source, row),
            ),
            completed_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
        )
    rows = list(session.scalars(select(HistoricalUniverseMemberRecord)))
    assert {item.security_id for item in rows} == {security.id}
    assert {item.exchange_listing_id for item in rows} == {old.id, new.id}


def test_unresolved_and_ambiguous_historical_members_remain_explicit(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    _, ambiguous_security, _ = _identity(
        session,
        symbol="AMB",
        isin="INE000000003",
        valid_from=date(2018, 1, 1),
    )
    session.add(
        ExchangeListing(
            security_id=ambiguous_security.id,
            exchange="NSE",
            symbol="AMB2",
            valid_from=date(2018, 1, 2),
            status="active",
            created_at=ENTRY_AT,
            updated_at=ENTRY_AT,
        )
    )
    row_a = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="AMB", isin="INE000000003"
    )
    row_b = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="MISSING", isin="INE000000004"
    )
    source_a = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="amb",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row_a,
    )
    source_b = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="missing",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row_b,
    )
    result = build_historical_universe(
        session,
        policy=load_historical_universe_policy(UNIVERSE_POLICY),
        cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
        source_provider_dataset_id=dataset.id,
        source_ingestion_run_id=ingestion.id,
        evidence=(
            _universe_evidence(source_a, row_a),
            _universe_evidence(source_b, row_b),
        ),
        completed_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
    )
    assert result.ambiguous_identity == 1
    assert result.unresolved_identity == 1
    assert {
        item.membership_status for item in session.scalars(select(HistoricalUniverseMemberRecord))
    } == {
        "ambiguous_identity",
        "unresolved_identity",
    }


def test_current_survivor_set_cannot_substitute_and_cohort_is_idempotent(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    _identity(
        session,
        symbol="TODAY",
        isin="INE000000005",
        valid_from=date(2025, 1, 1),
    )
    row = _semantic_row(
        trading_date=date(2020, 1, 31),
        symbol="HISTORICAL",
        isin="INE000000099",
    )
    source = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="historical-absent",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row,
    )
    policy = load_historical_universe_policy(UNIVERSE_POLICY)

    def build() -> HistoricalUniverseBuildResult:
        return build_historical_universe(
            session,
            policy=policy,
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(
                _universe_evidence(source, row),
            ),
            completed_at=datetime(2026, 10, 4, 12, tzinfo=UTC),
        )

    first = build()
    second = build()
    assert first.run_key_sha256 == second.run_key_sha256
    assert first.unresolved_identity == 1
    assert second.created_run is False


def test_complete_authoritative_snapshot_builds_every_member_and_recomposes(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    rows: list[dict[str, str]] = []
    sources: list[SourceRecord] = []
    for index, symbol in enumerate(("ALPHA", "BETA", "DEAD"), start=1):
        isin = f"INE0000002{index:02d}"
        _identity(
            session,
            symbol=symbol,
            isin=isin,
            valid_from=date(2018, 1, 1),
            valid_to=date(2021, 6, 30) if symbol == "DEAD" else None,
            security_status="inactive" if symbol == "DEAD" else "active",
            listing_status="delisted" if symbol == "DEAD" else "active",
        )
        row = _semantic_row(
            trading_date=date(2020, 1, 31), symbol=symbol, isin=isin
        )
        rows.append(row)
        sources.append(
            _mii_source(
                session,
                dataset,
                ingestion,
                suffix=symbol.lower(),
                snapshot_date=date(2020, 1, 31),
                semantic_row=row,
            )
        )
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    result = build_historical_universe(
        session,
        policy=policy,
        cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
        source_provider_dataset_id=dataset.id,
        source_ingestion_run_id=ingestion.id,
        evidence=tuple(
            _universe_evidence(source, row)
            for source, row in zip(sources, rows, strict=True)
        ),
        completed_at=datetime(2026, 10, 4, tzinfo=UTC),
    )
    run = HistoricalEvaluationRepository(session).get_universe_run(result.run_id)
    assert run is not None
    assert result.total_members == ingestion.records_accepted == 3
    assert run.summary_json["eligible_source_row_count"] == 3
    assert {item.historical_symbol for item in run.members} == {"ALPHA", "BETA", "DEAD"}
    assert any(item.historical_symbol == "DEAD" for item in run.members)
    shards = shard_historical_universe(tuple(run.members), policy=policy)
    assert {symbol for shard in shards for symbol in shard.symbols} == {
        item.historical_symbol for item in run.members
    }


def test_authoritative_snapshot_rejects_missing_member(session: Session) -> None:
    dataset, ingestion = _dataset(session)
    rows = [
        _semantic_row(
            trading_date=date(2020, 1, 31),
            symbol=f"MISS{index}",
            isin=f"INE0000003{index:02d}",
        )
        for index in range(3)
    ]
    sources = [
        _mii_source(
            session,
            dataset,
            ingestion,
            suffix=f"missing-{index}",
            snapshot_date=date(2020, 1, 31),
            semantic_row=row,
        )
        for index, row in enumerate(rows)
    ]
    with pytest.raises(ValueError, match=r"exactly match.*missing=1, extra=0"):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=tuple(
                _universe_evidence(source, row)
                for source, row in zip(sources[:2], rows[:2], strict=True)
            ),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


def test_authoritative_snapshot_rejects_extra_or_mixed_snapshot_member(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    other_ingestion = _ingestion(session, dataset)
    first_row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="BOUND1", isin="INE000000401"
    )
    second_row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="BOUND2", isin="INE000000402"
    )
    other_row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="OTHER", isin="INE000000403"
    )
    first = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="bound-1",
        snapshot_date=date(2020, 1, 31),
        semantic_row=first_row,
    )
    second = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="bound-2",
        snapshot_date=date(2020, 1, 31),
        semantic_row=second_row,
    )
    other = _mii_source(
        session,
        dataset,
        other_ingestion,
        suffix="other",
        snapshot_date=date(2020, 1, 31),
        semantic_row=other_row,
    )
    common = {
        "session": session,
        "policy": load_historical_universe_policy(UNIVERSE_POLICY),
        "cutoff": datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
        "source_provider_dataset_id": dataset.id,
        "source_ingestion_run_id": ingestion.id,
        "completed_at": datetime(2026, 10, 4, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match=r"missing=0, extra=1"):
        build_historical_universe(
            **common,
            evidence=(
                _universe_evidence(first, first_row),
                _universe_evidence(second, second_row),
                _universe_evidence(other, other_row),
            ),
        )
    with pytest.raises(ValueError, match=r"missing=1, extra=1"):
        build_historical_universe(
            **common,
            evidence=(
                _universe_evidence(first, first_row),
                _universe_evidence(other, other_row),
            ),
        )


def test_authoritative_snapshot_rejects_duplicate_source_row(session: Session) -> None:
    dataset, ingestion = _dataset(session)
    row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="DUPE", isin="INE000000501"
    )
    source = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="dupe",
        snapshot_date=date(2020, 1, 31),
        semantic_row=row,
    )
    item = _universe_evidence(source, row)
    with pytest.raises(ValueError, match="duplicate historical universe source row"):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(item, item),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


def test_missing_historical_security_master_is_data_blocked(session: Session) -> None:
    dataset, ingestion = _dataset(session)
    with pytest.raises(
        HistoricalUniverseDataUnavailableError,
        match="historical NSE CM MII security-master snapshot is unavailable",
    ):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


def test_historical_universe_rejects_unbound_source_and_mixed_dates(
    session: Session,
) -> None:
    dataset, ingestion = _dataset(session)
    first_row = _semantic_row(
        trading_date=date(2020, 1, 30), symbol="AAA", isin="INE000000101"
    )
    second_row = _semantic_row(
        trading_date=date(2020, 1, 31), symbol="BBB", isin="INE000000102"
    )
    first = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="bound-a",
        snapshot_date=date(2020, 1, 30),
        semantic_row=first_row,
    )
    second = _mii_source(
        session,
        dataset,
        ingestion,
        suffix="bound-b",
        snapshot_date=date(2020, 1, 31),
        semantic_row=second_row,
    )
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    mixed = (
        _universe_evidence(first, first_row),
        _universe_evidence(second, second_row),
    )
    with pytest.raises(ValueError, match="mixes multiple archived source artifacts"):
        build_historical_universe(
            session,
            policy=policy,
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=mixed,
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
    dataset.code = "unreviewed_dataset"
    with pytest.raises(ValueError, match="authoritative source binding mismatch"):
        build_historical_universe(
            session,
            policy=policy,
            cutoff=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            source_provider_dataset_id=dataset.id,
            source_ingestion_run_id=ingestion.id,
            evidence=(mixed[-1],),
            completed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("count", "sizes"),
    [(1, [1]), (25, [25]), (26, [25, 1]), (50, [25, 25]), (51, [25, 25, 1])],
)
def test_historical_universe_sharding_is_deterministic_and_recomposes(
    count: int, sizes: list[int]
) -> None:
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    members = tuple(
        HistoricalUniverseMemberRecord(
            id=UUID(int=index + 1),
            historical_universe_run_id=uuid4(),
            historical_symbol=f"Q{index:04d}",
            historical_isin=f"IN{index:010d}",
            exchange="NSE",
            series="EQ",
            membership_date=date(2020, 1, 31),
            company_id=uuid4(),
            security_id=UUID(int=index + 1000),
            exchange_listing_id=uuid4(),
            membership_status="eligible",
            reason_code=None,
            source_record_id=uuid4(),
            member_fingerprint_sha256=f"{index:064x}",
            provenance_json={},
        )
        for index in range(count)
    )
    first = shard_historical_universe(tuple(reversed(members)), policy=policy)
    second = shard_historical_universe(members, policy=policy)
    assert [len(item.symbols) for item in first] == sizes
    assert [(item.shard_id, item.symbols) for item in first] == [
        (item.shard_id, item.symbols) for item in second
    ]
    assert [symbol for shard in first for symbol in shard.symbols] == [
        item.historical_symbol for item in members
    ]
    assert len({symbol for shard in first for symbol in shard.symbols}) == count


def test_sharding_rejects_duplicate_symbol_and_member_change_changes_checksum() -> None:
    policy = load_historical_universe_policy(UNIVERSE_POLICY)
    members = tuple(
        HistoricalUniverseMemberRecord(
            id=uuid4(),
            historical_universe_run_id=uuid4(),
            historical_symbol="DUP",
            historical_isin=f"IN{index:010d}",
            exchange="NSE",
            series="EQ",
            membership_date=date(2020, 1, 31),
            company_id=uuid4(),
            security_id=UUID(int=index + 1),
            exchange_listing_id=uuid4(),
            membership_status="eligible",
            reason_code=None,
            source_record_id=uuid4(),
            member_fingerprint_sha256=f"{index:064x}",
            provenance_json={},
        )
        for index in range(2)
    )
    with pytest.raises(ValueError, match="symbols must be unique"):
        shard_historical_universe(members, policy=policy)
    one = (members[0],)
    changed = (members[0],)
    first = shard_historical_universe(one, policy=policy)[0]
    members[0].member_fingerprint_sha256 = "f" * 64
    second = shard_historical_universe(changed, policy=policy)[0]
    assert first.checksum_sha256 != second.checksum_sha256


@pytest.mark.parametrize(
    ("multiple", "years", "hit_date", "hit_close"),
    [
        ("2", 2, date(2021, 12, 31), "200"),
        ("3", 3, date(2022, 12, 30), "300"),
        ("5", 5, date(2024, 12, 31), "500"),
    ],
)
def test_multibagger_contract_thresholds_mature_positive_early(
    multiple: str, years: int, hit_date: date, hit_close: str
) -> None:
    series, entry = _series([(ENTRY_DATE, "100"), (hit_date, hit_close)])
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract(multiple, years),
        outcome_data_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    assert result.classification == "positive"
    assert result.first_threshold_hit_trading_date == hit_date
    assert result.label_matured_at == series.bars[-1].available_at
    assert result.peak_price_multiple == Decimal(multiple)


def test_threshold_hit_is_sticky_and_decimal_boundary_is_exact() -> None:
    series, entry = _series(
        [(ENTRY_DATE, "100"), (date(2021, 1, 4), "300"), (date(2022, 1, 2), "150")]
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("3", 3),
        outcome_data_cutoff=datetime(2024, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    assert result.classification == "positive"
    assert result.first_threshold_hit_trading_date == date(2021, 1, 4)
    assert result.maximum_forward_price_return == Decimal("2")


def test_just_below_threshold_is_negative_only_after_complete_horizon() -> None:
    series, entry = _series(
        [(ENTRY_DATE, "100"), (date(2021, 6, 1), "199.999"), (date(2022, 1, 2), "150")]
    )
    mature = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=_calendar([date(2021, 6, 1), date(2022, 1, 2)]),
    )
    immature = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("3", 3),
        outcome_data_cutoff=datetime(2022, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    assert mature.classification == "negative"
    assert mature.label_matured_at is not None
    assert immature.classification == "unmatured"
    assert immature.label_matured_at is None


def test_negative_requires_complete_authoritative_trading_window() -> None:
    series, entry = _series(
        [
            (ENTRY_DATE, "100"),
            (date(2022, 1, 3), "120"),
        ]
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 4, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=_calendar(
            [date(2020, 1, 3), date(2020, 1, 6), date(2022, 1, 3)]
        ),
    )
    assert result.classification == "unavailable"
    assert result.unavailable_reason == "incomplete_outcome_window"
    assert result.provenance["missing_trading_dates"] == ["2020-01-03", "2020-01-06"]


def test_complete_supported_window_with_threshold_hit_remains_positive() -> None:
    series, entry = _series(
        [
            (ENTRY_DATE, "100"),
            (date(2020, 1, 3), "200"),
            (date(2022, 1, 2), "150"),
        ]
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=_calendar([date(2020, 1, 3), date(2022, 1, 2)]),
    )
    assert result.classification == "positive"
    assert result.first_threshold_hit_trading_date == date(2020, 1, 3)


def test_incomplete_window_does_not_block_an_observed_positive_hit() -> None:
    series, entry = _series(
        [(ENTRY_DATE, "100"), (date(2020, 1, 6), "200")]
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 4, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=_calendar(
            [date(2020, 1, 3), date(2020, 1, 6), date(2022, 1, 3)]
        ),
    )
    assert result.classification == "positive"


def test_symbol_rename_continues_same_security_but_true_terminal_end_remains(
    session: Session,
) -> None:
    _, security, _ = _identity(
        session,
        symbol="OLD",
        isin="INE000000094",
        valid_from=date(2018, 1, 1),
        valid_to=date(2020, 6, 30),
    )
    session.add(
        ExchangeListing(
            security_id=security.id,
            exchange="NSE",
            symbol="NEW",
            valid_from=date(2020, 7, 1),
            valid_to=None,
            status="active",
            created_at=ENTRY_AT,
            updated_at=ENTRY_AT,
        )
    )
    session.flush()
    assert _terminal_nse_listing_end(session, security.id) is None
    series, entry = _series([(ENTRY_DATE, "100"), (date(2020, 8, 3), "200")])
    continued = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 4, tzinfo=UTC),
        listing_valid_to=_terminal_nse_listing_end(session, security.id),
        trading_calendar_bars=(),
    )
    assert continued.classification == "positive"

    _, delisted, _ = _identity(
        session,
        symbol="DELIST",
        isin="INE000000095",
        valid_from=date(2018, 1, 1),
        valid_to=date(2021, 5, 1),
    )
    assert _terminal_nse_listing_end(session, delisted.id) == date(2021, 5, 1)


def test_unavailable_states_are_never_imputed_negative() -> None:
    series, entry = _series([(ENTRY_DATE, "100"), (date(2021, 1, 2), "150")])
    missing_entry = compute_multibagger_label(
        series=series,
        entry_raw_bar=None,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2023, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    missing_history = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2023, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    delisted = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2023, 1, 3, tzinfo=UTC),
        listing_valid_to=date(2021, 5, 1),
        trading_calendar_bars=(),
    )
    assert (missing_entry.classification, missing_entry.unavailable_reason) == (
        "unavailable",
        "entry_price_unavailable",
    )
    assert missing_history.unavailable_reason == "incomplete_outcome_window"
    assert delisted.unavailable_reason == "unavailable_due_to_delisting"


@pytest.mark.parametrize("action_type", ["split", "bonus"])
def test_split_and_bonus_adjusted_continuation(action_type: str) -> None:
    action = _action(action_type)
    series, entry = _series([(ENTRY_DATE, "100"), (date(2021, 1, 3), "100")], actions=(action,))
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 1, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    assert result.adjusted_entry_close == Decimal("50")
    assert result.classification == "positive"


def test_post_hit_split_does_not_delay_positive_label_maturity() -> None:
    later_action = _action(
        "split",
        effective_date=date(2021, 1, 2),
        available_at=datetime(2021, 1, 2, 18, tzinfo=UTC),
    )
    hit_date = date(2020, 6, 1)
    series, entry = _series(
        [(ENTRY_DATE, "100"), (hit_date, "200"), (date(2021, 1, 3), "100")],
        actions=(later_action,),
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    assert result.classification == "positive"
    assert result.label_matured_at == datetime(2020, 6, 1, tzinfo=UTC)
    assert result.provenance["positive_maturity_corporate_action_ids"] == []


def test_intervening_adjustment_evidence_is_required_for_positive_maturity() -> None:
    action_available_at = datetime(2021, 1, 3, 18, tzinfo=UTC)
    intervening_action = _action(
        "split",
        effective_date=date(2020, 6, 1),
        available_at=action_available_at,
    )
    series, entry = _series(
        [(ENTRY_DATE, "100"), (date(2021, 1, 3), "100")],
        actions=(intervening_action,),
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    assert result.classification == "positive"
    assert result.label_matured_at == action_available_at
    assert result.provenance["positive_maturity_corporate_action_ids"] == [
        str(intervening_action.id)
    ]


def test_unsupported_action_and_out_of_window_prices_fail_safely() -> None:
    rights = _action("rights")
    series, entry = _series([(ENTRY_DATE, "100"), (date(2021, 1, 3), "150")], actions=(rights,))
    unsupported = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2023, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=(),
    )
    after_window, entry = _series(
        [(ENTRY_DATE, "100"), (date(2021, 1, 3), "150"), (date(2022, 1, 3), "500")]
    )
    negative = compute_multibagger_label(
        series=after_window,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2023, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=_calendar([date(2021, 1, 3), date(2022, 1, 3)]),
    )
    assert unsupported.unavailable_reason == "unsupported_corporate_action_state"
    assert negative.classification == "negative"
    assert negative.peak_price_multiple == Decimal("1.5")


def test_maximum_forward_return_and_drawdown_use_window_adjusted_closes() -> None:
    series, entry = _series(
        [
            (ENTRY_DATE, "100"),
            (date(2020, 6, 1), "150"),
            (date(2021, 1, 1), "75"),
            (date(2022, 1, 2), "90"),
        ]
    )
    result = compute_multibagger_label(
        series=series,
        entry_raw_bar=entry,
        contract=_contract("2", 2),
        outcome_data_cutoff=datetime(2022, 1, 3, tzinfo=UTC),
        listing_valid_to=None,
        trading_calendar_bars=_calendar(
            [date(2020, 6, 1), date(2021, 1, 1), date(2022, 1, 2)]
        ),
    )
    assert result.maximum_forward_price_return == Decimal("0.5")
    assert result.maximum_drawdown == Decimal("-0.5")


def test_label_persistence_is_append_only_idempotent_and_cutoff_bound(
    session: Session,
) -> None:
    repository = HistoricalEvaluationRepository(session)
    observation_id = uuid4()
    cutoff = datetime(2026, 10, 4, 12, tzinfo=UTC)

    def create_run(run_key: str, outcome_cutoff: datetime):
        return repository.create_label_run(
            run_key_sha256=run_key,
            label_policy_code="production_multibagger_outcomes_v1",
            label_policy_checksum_sha256="1" * 64,
            source_backtest_run_id=uuid4(),
            source_backtest_run_key_sha256="2" * 64,
            outcome_data_cutoff=outcome_cutoff,
            market_provider_dataset_id=uuid4(),
            corporate_action_provider_dataset_id=uuid4(),
            source_state_checksum_sha256="3" * 64,
            ordered_contracts_json=[{"code": "MB_2X_2Y"}],
            algorithm_versions_json={"label": "v1"},
            inputs_json={"outcome_data_cutoff": outcome_cutoff.isoformat()},
        )

    run, created = create_run("a" * 64, cutoff)
    assert created is True
    value = MultibaggerLabelWrite(
        backtest_observation_id=observation_id,
        contract_code="MB_2X_2Y",
        classification="positive",
        unavailable_reason=None,
        threshold_multiple=Decimal("2"),
        horizon_calendar_years=2,
        entry_trading_date=ENTRY_DATE,
        adjusted_entry_close=Decimal("100"),
        horizon_end_date=date(2022, 1, 2),
        first_threshold_hit_trading_date=date(2021, 1, 2),
        trading_observations_available=1,
        peak_adjusted_close=Decimal("200"),
        peak_price_multiple=Decimal("2"),
        maximum_forward_price_return=Decimal("1"),
        endpoint_adjusted_close=Decimal("200"),
        endpoint_return=Decimal("1"),
        calendar_days_to_threshold=366,
        trading_observations_to_threshold=1,
        maximum_drawdown=Decimal("0"),
        listing_valid_to=None,
        label_matured_at=datetime(2021, 1, 2, tzinfo=UTC),
        outcome_data_cutoff=cutoff,
        provenance_json={"score_or_rank_used": False},
        label_fingerprint_sha256="4" * 64,
    )
    label, label_created = repository.add_label(run, value)
    reused, reused_created = repository.add_label(run, value)
    assert label.id == reused.id
    assert label_created is True and reused_created is False
    with pytest.raises(HistoricalEvaluationIntegrityError, match="label conflicts"):
        repository.add_label(
            run,
            replace(
                value,
                classification="negative",
                label_fingerprint_sha256="5" * 64,
            ),
        )
    second_cutoff = datetime(2026, 10, 5, 12, tzinfo=UTC)
    second_identity = canonical_json_sha256({"cutoff": second_cutoff.isoformat()})
    second, second_created = create_run(second_identity, second_cutoff)
    assert second_created is True
    assert second.id != run.id


def test_label_build_consumes_frozen_j_observation_and_reruns_idempotently(
    session: Session,
) -> None:
    market, ingestion = _profile_datasets(session)
    company, security, _ = _identity(
        session,
        symbol="FROZEN",
        isin="INE000000007",
        valid_from=date(2018, 1, 1),
    )
    cutoff = datetime(2020, 1, 2, 23, 59, tzinfo=UTC)
    for index, (when, close, available_at) in enumerate(
        (
            (ENTRY_DATE, "100", datetime(2020, 1, 2, 18, tzinfo=UTC)),
            (date(2021, 1, 4), "200", datetime(2021, 1, 4, 18, tzinfo=UTC)),
            (date(2022, 1, 3), "180", datetime(2022, 1, 3, 18, tzinfo=UTC)),
        )
    ):
        source = _source(
            session,
            market,
            ingestion,
            suffix=f"frozen-price-{index}",
            retrieved_at=available_at,
        )
        session.add(
            PriceBar(
                security_id=security.id,
                source_record_id=source.id,
                trading_date=when,
                interval="1d",
                open_price=Decimal(close),
                high_price=Decimal(close),
                low_price=Decimal(close),
                close_price=Decimal(close),
                volume=1,
                available_at=available_at,
            )
        )
    profile = load_research_profile(RESEARCH_PROFILE)
    policy = load_multibagger_outcome_policy(LABEL_POLICY, repository_root=ROOT)
    backtests = BacktestRepository(session)
    backtest, _ = backtests.create_run(
        run_key_sha256="6" * 64,
        backtest_policy_code=policy.source_backtest_policy_code,
        backtest_policy_checksum_sha256=policy.source_backtest_policy_checksum_sha256,
        availability_manifest_code="production_historical_availability_v1",
        availability_manifest_checksum_sha256="5" * 64,
        scoring_configuration_id=uuid4(),
        scoring_configuration_checksum_sha256="4" * 64,
        research_profile_code=profile.research_profile_code,
        research_profile_checksum_sha256=profile.checksum_sha256,
        financial_primitive_policy_checksum_sha256="3" * 64,
        financial_endpoint_policy_checksum_sha256="2" * 64,
        source_state_checksum_sha256="1" * 64,
        model_family="fixture-model",
        cutoff_start=cutoff,
        cutoff_end=cutoff,
        cutoff_cadence="calendar_month_end_utc",
        universe_policy="observed_pit_identity_v1",
        benchmark_code="NIFTY 50",
        return_horizons=(21, 63, 126, 252),
        ordered_symbols=("FROZEN",),
        symbol_set_checksum_sha256="0" * 64,
        inputs_json={},
    )
    observation, _ = backtests.add_observation(
        backtest,
        BacktestObservationWrite(
            company_id=company.id,
            security_id=security.id,
            score_snapshot_id=None,
            symbol="FROZEN",
            knowledge_cutoff=cutoff,
            observation_status="partial_snapshot",
            selected_fiscal_year=None,
            selected_fiscal_quarter=None,
            selected_filing_scope=None,
            selected_period_end=None,
            snapshot_status="partial_component_set",
            snapshot_fingerprint_sha256=None,
            research_state_projection_version="historical_research_state_v1",
            research_state_sha256="f" * 64,
            research_state_changed=True,
            detail_json={},
        ),
    )
    backtests.set_status(backtest, "snapshots_built")
    session.commit()
    outcome_cutoff = datetime(2022, 1, 4, tzinfo=UTC)
    first = build_multibagger_labels(
        session,
        backtest_run_id=backtest.id,
        policy=policy,
        research_profile=profile,
        outcome_data_cutoff=outcome_cutoff,
    )
    second = build_multibagger_labels(
        session,
        backtest_run_id=backtest.id,
        policy=policy,
        research_profile=profile,
        outcome_data_cutoff=outcome_cutoff,
    )
    later = build_multibagger_labels(
        session,
        backtest_run_id=backtest.id,
        policy=policy,
        research_profile=profile,
        outcome_data_cutoff=datetime(2022, 1, 5, tzinfo=UTC),
    )
    assert first.run_id == second.run_id
    assert later.run_id != first.run_id
    assert first.labels_created == 3
    assert second.labels_reused == 3
    stored = HistoricalEvaluationRepository(session).get_label_run(first.run_id)
    assert stored is not None
    assert stored.algorithm_versions_json["outcome_window_completeness"] == (
        "nifty50_session_dates_full_security_bar_coverage_v1"
    )
    labels = {item.contract_code: item for item in stored.labels}
    assert labels["MB_2X_2Y"].classification == "positive"
    assert len(cast(list[object], labels["MB_2X_2Y"].provenance_json["market_sources"])) == 2
    assert labels["MB_3X_3Y"].classification == "unmatured"
    assert labels["MB_5X_5Y"].classification == "unmatured"
    assert observation.research_state_sha256 == "f" * 64


def test_current_fetched_price_is_not_backdated_into_research_entry(session: Session) -> None:
    dataset, ingestion = _dataset(session)
    _, security, _ = _identity(
        session,
        symbol="PIT",
        isin="INE000000006",
        valid_from=date(2018, 1, 1),
    )
    source = _source(session, dataset, ingestion, suffix="late-price")
    bar = PriceBar(
        security_id=security.id,
        source_record_id=source.id,
        trading_date=date(2020, 1, 31),
        interval="1d",
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("100"),
        volume=1,
        available_at=datetime(2026, 10, 4, tzinfo=UTC),
    )
    session.add(bar)
    session.flush()
    assert (
        PointInTimeMarketReader(session).latest_market_bar_as_of(
            provider_dataset_id=dataset.id,
            security_id=security.id,
            interval="1d",
            as_of=datetime(2020, 1, 31, 23, 59, tzinfo=UTC),
            on_or_before=date(2020, 1, 31),
        )
        is None
    )


def test_research_and_live_modules_do_not_depend_on_q_outcomes() -> None:
    protected = [
        ROOT / "packages/core/inflector_core",
        ROOT / "packages/data/inflector_data/production_research.py",
        ROOT / "packages/data/inflector_data/score_orchestration.py",
        ROOT / "packages/data/inflector_data/research_cli.py",
        ROOT / "packages/data/inflector_data/opportunity_discovery.py",
        ROOT / "packages/data/inflector_data/opportunity_change.py",
        ROOT / "packages/data/inflector_data/research_notifications.py",
        ROOT / "packages/data/inflector_data/research_notification_delivery.py",
    ]
    files = [
        path for root in protected for path in ([root] if root.is_file() else root.rglob("*.py"))
    ]
    forbidden = {"multibagger_labels", "MultibaggerOutcomeLabel", "MultibaggerLabelRun"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert not any(module.endswith("multibagger_labels") for module in modules), path
        assert not forbidden.intersection(names), path
    label_source = (ROOT / "packages/data/inflector_data/multibagger_labels.py").read_text(
        encoding="utf-8"
    )
    assert "ScoreSnapshot" not in label_source
    assert "OpportunityDiscovery" not in label_source


def test_q_cli_rejects_naive_datetime_at_service_boundary(session: Session) -> None:
    args = _parser().parse_args(
        [
            "build-historical-universe",
            "--database-url",
            "sqlite://",
            "--universe-policy",
            str(UNIVERSE_POLICY),
            "--source-dataset-id",
            str(uuid4()),
            "--source-ingestion-run-id",
            str(uuid4()),
            "--cutoff",
            "2020-01-31T23:59:59",
            "--completed-at",
            "2026-10-04T12:00:00+00:00",
            "--evidence-json",
            "fixture.json",
        ]
    )
    assert args.cutoff.tzinfo is None
    with pytest.raises(ValueError, match="cutoff must be timezone-aware"):
        build_historical_universe(
            session,
            policy=load_historical_universe_policy(UNIVERSE_POLICY),
            cutoff=args.cutoff,
            source_provider_dataset_id=args.source_dataset_id,
            source_ingestion_run_id=args.source_ingestion_run_id,
            evidence=(),
            completed_at=args.completed_at,
        )


def test_q_cli_rejects_invalid_contract_and_json_is_deterministic(session: Session) -> None:
    policy = load_multibagger_outcome_policy(LABEL_POLICY, repository_root=ROOT)
    run, _ = HistoricalEvaluationRepository(session).create_label_run(
        run_key_sha256="9" * 64,
        label_policy_code=policy.code,
        label_policy_checksum_sha256=policy.checksum_sha256,
        source_backtest_run_id=uuid4(),
        source_backtest_run_key_sha256="8" * 64,
        outcome_data_cutoff=datetime(2026, 10, 4, tzinfo=UTC),
        market_provider_dataset_id=uuid4(),
        corporate_action_provider_dataset_id=uuid4(),
        source_state_checksum_sha256="7" * 64,
        ordered_contracts_json=[],
        algorithm_versions_json={},
        inputs_json={},
    )
    HistoricalEvaluationRepository(session).complete_label_run(
        run,
        summary_json={"contracts": {}},
        completed_at=datetime(2026, 10, 4, tzinfo=UTC),
    )
    session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
    session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20261005_0024')"))
    args = _parser().parse_args(
        [
            "summarize-multibagger-labels",
            "--database-url",
            "sqlite://",
            "--multibagger-policy",
            str(LABEL_POLICY),
            "--run-id",
            str(run.id),
            "--contract",
            "NOT_A_CONTRACT",
        ]
    )
    with pytest.raises(ValueError, match="contract is unsupported"):
        _execute(args, session)
    args.contract = None
    payload = _execute(args, session)
    first = json.dumps(payload, default=_json_default, sort_keys=True)
    second = json.dumps(payload, default=_json_default, sort_keys=True)
    assert first == second
    assert '"status": "completed"' in first


def test_migration_0023_upgrade_and_downgrade_preserve_earlier_tables(tmp_path: Path) -> None:
    database = tmp_path / "q-migration.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    command.upgrade(config, "20261003_0022")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, "20261004_0023")
    after = set(inspect(engine).get_table_names())
    assert after - before == {
        "historical_universe_runs",
        "historical_universe_members",
        "multibagger_label_runs",
        "multibagger_outcome_labels",
    }
    checks = {
        item["name"] for item in inspect(engine).get_check_constraints("multibagger_outcome_labels")
    }
    assert {
        "ck_multibagger_outcome_label_classification",
        "ck_multibagger_outcome_label_state_fields",
        "ck_multibagger_outcome_label_measurement_bounds",
    }.issubset(checks)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "20261004_0023"
        )
    command.downgrade(config, "20261003_0022")
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()
