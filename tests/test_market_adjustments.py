"""Phase 3G-B corporate-action PIT, adjusted-price, and simple-return tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_core.providers import (
    BenchmarkBarRecord,
    CorporateActionRecord,
    IngestionEnvelope,
    MarketBarRecord,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.corporate_action_pit import (
    CorporateActionIntegrityError,
    PointInTimeCorporateActionReader,
)
from inflector_data.market_adjustments import (
    ADJUSTED_MARKET_PRICE_VERSION,
    CORPORATE_ACTION_PRICE_FACTOR_VERSION,
    SIMPLE_BENCHMARK_RETURN_VERSION,
    SIMPLE_PRICE_RETURN_VERSION,
    MarketAdjustmentPrimitives,
)
from inflector_data.market_pit import PointInTimeMarketReader
from inflector_data.providers import (
    CSVBenchmarkDataProvider,
    CSVCorporateActionProvider,
    CSVMarketDataProvider,
    CSVUniverseProvider,
    MockBenchmarkDataProvider,
    MockCorporateActionProvider,
    MockMarketDataProvider,
)
from inflector_data.service import IngestionService
from inflector_database.models import (
    CorporateAction,
    DataProvider,
    ProviderDataset,
    Security,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 8, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("adjustment_universe", "csv", "universe", "synthetic-development-only")
MARKET_A = ProviderMetadata(
    "adjustment_market_a", "csv", "market_daily", "synthetic-development-only"
)
MARKET_B = ProviderMetadata(
    "adjustment_market_b", "mock", "market_daily", "synthetic-development-only"
)
ACTIONS_A = ProviderMetadata(
    "adjustment_actions_a", "csv", "corporate_actions", "synthetic-development-only"
)
ACTIONS_B = ProviderMetadata(
    "adjustment_actions_b", "mock", "corporate_actions", "synthetic-development-only"
)
ACTIONS_NONE = ProviderMetadata(
    "adjustment_actions_none", "mock", "corporate_actions", "synthetic-development-only"
)
BENCHMARKS = ProviderMetadata(
    "adjustment_benchmarks", "csv", "benchmark_daily", "synthetic-development-only"
)
ISIN = "INF0AUR01018"


def _dataset_id(session: Session, metadata: ProviderMetadata) -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(
            DataProvider.code == metadata.provider_code,
            ProviderDataset.code == metadata.dataset_code,
        )
    )
    assert value is not None
    return value


def _setup(session: Session, tmp_path: Path) -> tuple[IngestionService, UUID]:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    security_id = session.scalar(select(Security.id).where(Security.isin == ISIN))
    assert result.records_accepted == 3 and security_id is not None
    return service, security_id


def _action_record(**overrides: object) -> CorporateActionRecord:
    return replace(
        CorporateActionRecord(
            security_isin=ISIN,
            action_type="split",
            announcement_date=date(2026, 1, 1),
            ex_date=None,
            record_date=None,
            effective_date=date(2026, 1, 10),
            ratio_numerator=2,
            ratio_denominator=1,
            cash_amount=None,
            cash_currency=None,
            cash_unit=None,
            subscription_price=None,
            subscription_currency=None,
            exchange=None,
            old_symbol=None,
            new_symbol=None,
            successor_isin=None,
        ),
        **overrides,
    )


def _action_provider(
    entries: Sequence[tuple[str, CorporateActionRecord, datetime, datetime | None]],
    metadata: ProviderMetadata = ACTIONS_A,
) -> MockCorporateActionProvider:
    rows = [
        f"{external_id}:{record!r}:{available_at!r}:{revision_at!r}"
        for external_id, record, available_at, revision_at in entries
    ]
    payload = "\n".join(rows).encode()
    envelopes = tuple(
        IngestionEnvelope(
            provider=metadata,
            external_record_id=external_id,
            source_uri="fixture://adjustment-actions",
            raw_payload_reference=f"row-{index}",
            content_sha256=sha256(rows[index - 1].encode()).hexdigest(),
            retrieved_at=RETRIEVED_AT,
            record=record,
            available_at=available_at,
            revision_at=revision_at,
        )
        for index, (external_id, record, available_at, revision_at) in enumerate(entries, start=1)
    )
    return MockCorporateActionProvider(
        ProviderBatch(
            provider=metadata,
            source_uri="fixture://adjustment-actions",
            raw_payload=payload,
            retrieved_at=RETRIEVED_AT,
            records=envelopes,
        )
    )


def _market_record(trading_date: date, close: Decimal) -> MarketBarRecord:
    return MarketBarRecord(
        security_isin=ISIN,
        trading_date=trading_date,
        interval="1d",
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        volume=1000,
        market_cap=Decimal("10000"),
        delivery_quantity=400,
        delivery_percentage=Decimal("0.4"),
    )


def _market_provider(
    entries: Sequence[tuple[str, MarketBarRecord, datetime, datetime | None]],
    metadata: ProviderMetadata = MARKET_A,
) -> MockMarketDataProvider:
    rows = [
        f"{external_id}:{record!r}:{available_at!r}:{revision_at!r}"
        for external_id, record, available_at, revision_at in entries
    ]
    payload = "\n".join(rows).encode()
    envelopes = tuple(
        IngestionEnvelope(
            provider=metadata,
            external_record_id=external_id,
            source_uri="fixture://adjustment-market",
            raw_payload_reference=f"row-{index}",
            content_sha256=sha256(rows[index - 1].encode()).hexdigest(),
            retrieved_at=RETRIEVED_AT,
            record=record,
            available_at=available_at,
            revision_at=revision_at,
        )
        for index, (external_id, record, available_at, revision_at) in enumerate(entries, start=1)
    )
    return MockMarketDataProvider(
        ProviderBatch(
            provider=metadata,
            source_uri="fixture://adjustment-market",
            raw_payload=payload,
            retrieved_at=RETRIEVED_AT,
            records=envelopes,
        )
    )


def _benchmark_provider(
    closes: Sequence[tuple[str, date, Decimal, datetime]],
) -> MockBenchmarkDataProvider:
    rows = [
        f"{external_id}:{trading_date}:{close}:{available_at}"
        for external_id, trading_date, close, available_at in closes
    ]
    payload = "\n".join(rows).encode()
    envelopes = tuple(
        IngestionEnvelope(
            provider=BENCHMARKS,
            external_record_id=external_id,
            source_uri="fixture://adjustment-benchmark",
            raw_payload_reference=f"row-{index}",
            content_sha256=sha256(rows[index - 1].encode()).hexdigest(),
            retrieved_at=RETRIEVED_AT,
            record=BenchmarkBarRecord(
                benchmark_code="NIFTY50",
                benchmark_display_name="Nifty 50",
                currency="INR",
                trading_date=trading_date,
                interval="1d",
                open_value=close,
                high_value=close,
                low_value=close,
                close_value=close,
            ),
            available_at=available_at,
        )
        for index, (external_id, trading_date, close, available_at) in enumerate(closes, start=1)
    )
    return MockBenchmarkDataProvider(
        ProviderBatch(
            provider=BENCHMARKS,
            source_uri="fixture://adjustment-benchmark",
            raw_payload=payload,
            retrieved_at=RETRIEVED_AT,
            records=envelopes,
        )
    )


def _primitives(session: Session) -> MarketAdjustmentPrimitives:
    return MarketAdjustmentPrimitives(
        PointInTimeMarketReader(session), PointInTimeCorporateActionReader(session)
    )


def test_action_pit_cutoff_provenance_filter_order_and_provider_isolation(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    service.ingest_corporate_actions(
        CSVCorporateActionProvider(
            FIXTURES / "corporate_actions_synthetic.csv", ACTIONS_A, RETRIEVED_AT
        )
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "P2-SPLIT",
                    _action_record(ratio_numerator=3),
                    datetime(2026, 1, 2, 12, tzinfo=UTC),
                    None,
                )
            ],
            ACTIONS_B,
        )
    )
    p1 = _dataset_id(session, ACTIONS_A)
    p2 = _dataset_id(session, ACTIONS_B)
    stored_action = session.scalar(
        select(CorporateAction).where(CorporateAction.provider_dataset_id == p1)
    )
    assert stored_action is not None
    stored_action.status = "retired"
    stored_action.ingested_at = datetime(2026, 3, 1, tzinfo=UTC)
    session.flush()
    reader = PointInTimeCorporateActionReader(session)
    exact = datetime(2026, 1, 2, 12, tzinfo=UTC)

    assert (
        reader.corporate_actions_as_of(
            provider_dataset_id=p1,
            security_id=security_id,
            as_of=exact - timedelta(microseconds=1),
        )
        == []
    )
    selected = reader.corporate_actions_as_of(
        provider_dataset_id=p1, security_id=security_id, as_of=exact
    )
    other = reader.corporate_actions_as_of(
        provider_dataset_id=p2, security_id=security_id, as_of=exact
    )
    assert len(selected) == len(other) == 1
    split = selected[0]
    assert (split.action_type, split.ratio_numerator, split.ratio_denominator) == (
        "split",
        2,
        1,
    )
    assert other[0].ratio_numerator == 3
    assert split.source_record.external_record_id == "ACT-SPLIT"
    assert split.source_record.source_uri.endswith("corporate_actions_synthetic.csv")
    assert split.source_record.raw_object_key
    assert split.source_record.raw_payload_reference == "row-1"
    assert len(split.source_record.content_sha256) == 64
    assert split.available_at == exact

    all_actions = reader.corporate_actions_as_of(
        provider_dataset_id=p1,
        security_id=security_id,
        as_of=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert [action.action_type for action in all_actions] == [
        "split",
        "bonus",
        "cash_dividend",
        "rights",
        "symbol_change",
        "security_replacement",
    ]
    assert [action.event_anchor for action in all_actions] == sorted(
        action.event_anchor for action in all_actions if action.event_anchor is not None
    )
    assert [
        action.action_type
        for action in reader.corporate_actions_as_of(
            provider_dataset_id=p1,
            security_id=security_id,
            as_of=datetime(2026, 7, 1, tzinfo=UTC),
            action_types=("cash_dividend", "rights"),
        )
    ] == ["cash_dividend", "rights"]


def test_action_external_id_anchor_correction_and_economic_event_collapse(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)
    t2 = datetime(2026, 1, 12, tzinfo=UTC)
    service.ingest_corporate_actions(
        _action_provider([("ANCHOR-CORR", _action_record(), t1, None)])
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "ANCHOR-CORR",
                    _action_record(effective_date=date(2026, 1, 11), ratio_numerator=3),
                    t2,
                    datetime(2026, 2, 1, tzinfo=UTC),
                )
            ]
        )
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "EVENT-ALTERNATE",
                    _action_record(effective_date=date(2026, 1, 11), ratio_numerator=4),
                    t2 + timedelta(hours=1),
                    None,
                )
            ]
        )
    )
    dataset_id = _dataset_id(session, ACTIONS_A)
    reader = PointInTimeCorporateActionReader(session)

    before = reader.corporate_actions_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        as_of=t2 - timedelta(microseconds=1),
    )
    at_correction = reader.corporate_actions_as_of(
        provider_dataset_id=dataset_id, security_id=security_id, as_of=t2
    )
    after_duplicate = reader.corporate_actions_as_of(
        provider_dataset_id=dataset_id,
        security_id=security_id,
        as_of=t2 + timedelta(hours=1),
    )
    assert [(action.event_anchor, action.ratio_numerator) for action in before] == [
        (date(2026, 1, 10), 2)
    ]
    assert [(action.event_anchor, action.ratio_numerator) for action in at_correction] == [
        (date(2026, 1, 11), 3)
    ]
    assert [(action.event_anchor, action.ratio_numerator) for action in after_duplicate] == [
        (date(2026, 1, 11), 4)
    ]
    assert after_duplicate[0].revision_at is None
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 3


def test_action_source_acceptance_integrity_and_time_rules(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    available = datetime(2026, 1, 2, tzinfo=UTC)
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "INTEGRITY-SPLIT",
                    _action_record(),
                    available,
                    datetime(2026, 2, 1, tzinfo=UTC),
                )
            ]
        )
    )
    service.ingest_corporate_actions(
        _action_provider([("OTHER-PROVIDER", _action_record(), available, None)], ACTIONS_B)
    )
    p1 = _dataset_id(session, ACTIONS_A)
    p2 = _dataset_id(session, ACTIONS_B)
    reader = PointInTimeCorporateActionReader(session)
    equivalent = datetime(2026, 1, 2, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    selected = reader.corporate_actions_as_of(
        provider_dataset_id=p1, security_id=security_id, as_of=equivalent
    )
    assert len(selected) == 1 and selected[0].revision_at == datetime(2026, 2, 1, tzinfo=UTC)

    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "INTEGRITY-SPLIT")
    )
    assert source is not None
    source.validation_status = "parsed"
    session.flush()
    assert (
        reader.corporate_actions_as_of(
            provider_dataset_id=p1, security_id=security_id, as_of=available
        )
        == []
    )
    source.validation_status = "accepted"
    source.provider_dataset_id = p2
    session.flush()
    with pytest.raises(CorporateActionIntegrityError, match="source dataset"):
        reader.corporate_actions_as_of(
            provider_dataset_id=p1, security_id=security_id, as_of=available
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        reader.corporate_actions_as_of(
            provider_dataset_id=p1,
            security_id=security_id,
            as_of=datetime(2026, 1, 2),
        )


def test_real_ingestion_split_bonus_compound_adjustment_and_neutral_returns(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    market_result = service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_adjustment_split_bonus_synthetic.csv",
            MARKET_A,
            RETRIEVED_AT,
        )
    )
    action_result = service.ingest_corporate_actions(
        CSVCorporateActionProvider(
            FIXTURES / "corporate_actions_synthetic.csv", ACTIONS_A, RETRIEVED_AT
        )
    )
    assert (market_result.records_accepted, action_result.records_accepted) == (4, 6)
    market_dataset = _dataset_id(session, MARKET_A)
    action_dataset = _dataset_id(session, ACTIONS_A)
    cutoff = datetime(2026, 2, 10, 18, tzinfo=UTC)
    primitives = _primitives(session)

    series = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    equivalent_cutoff = datetime(
        2026,
        2,
        10,
        23,
        30,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    assert (
        primitives.adjusted_market_series_as_of(
            market_provider_dataset_id=market_dataset,
            corporate_action_provider_dataset_id=action_dataset,
            security_id=security_id,
            interval="1d",
            as_of=equivalent_cutoff,
        )
        == series
    )
    assert series.as_of == cutoff
    assert series.algorithm_version == ADJUSTED_MARKET_PRICE_VERSION
    assert series.adjustment_basis_date == date(2026, 2, 10)
    assert [adjustment.action_type for adjustment in series.applicable_adjustments] == [
        "split",
        "bonus",
    ]
    split, bonus = series.applicable_adjustments
    assert (
        split.share_factor,
        split.price_factor,
        split.algorithm_version,
    ) == (Decimal("2"), Decimal("0.5"), CORPORATE_ACTION_PRICE_FACTOR_VERSION)
    assert bonus.share_factor == Decimal("1.5")
    assert bonus.price_factor == Decimal("2") / Decimal("3")
    compound_factor = split.price_factor * bonus.price_factor
    assert [bar.cumulative_price_factor for bar in series.bars] == [
        compound_factor,
        bonus.price_factor,
        bonus.price_factor,
        Decimal("1"),
    ]
    assert series.bars[0].adjusted_close == Decimal("200.000000") * compound_factor
    assert series.bars[1].adjusted_close == Decimal("100.000000") * bonus.price_factor
    assert series.bars[2].adjusted_close == Decimal("100.000000")
    assert series.bars[3].adjusted_close == Decimal("100.000000")
    assert series.bars[0].adjusted_open == series.bars[0].adjusted_close
    assert series.bars[0].raw_bar.volume == 100000
    assert series.bars[0].raw_bar.market_cap == Decimal("1000")
    assert [item.action_type for item in series.bars[0].applied_adjustments] == [
        "split",
        "bonus",
    ]
    assert [item.action_type for item in series.bars[1].applied_adjustments] == ["bonus"]
    assert series.bars[3].applied_adjustments == ()
    assert series.bars[0].available_at == datetime(2026, 2, 2, 12, tzinfo=UTC)
    assert series.bars[0].raw_bar.source_record.validation_status == "accepted"
    assert split.action.source_record.validation_status == "accepted"

    returns = primitives.simple_price_returns_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    assert len(returns) == 3
    assert returns[0].algorithm_version == SIMPLE_PRICE_RETURN_VERSION
    assert (returns[0].previous_trading_date, returns[0].trading_date) == (
        date(2026, 1, 9),
        date(2026, 1, 10),
    )
    assert returns[2].value == Decimal("0")
    assert returns[2].available_at == max(
        returns[2].previous_bar.available_at, returns[2].current_bar.available_at
    )

    split_only_returns = primitives.simple_price_returns_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 1, 10, 18, tzinfo=UTC),
    )
    assert len(split_only_returns) == 1
    assert split_only_returns[0].value == Decimal("0")


def test_subset_basis_future_effective_and_start_date_factor_scope(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    service.ingest_market_data(
        CSVMarketDataProvider(
            FIXTURES / "market_adjustment_split_bonus_synthetic.csv",
            MARKET_A,
            RETRIEVED_AT,
        )
    )
    service.ingest_corporate_actions(
        CSVCorporateActionProvider(
            FIXTURES / "corporate_actions_synthetic.csv", ACTIONS_A, RETRIEVED_AT
        )
    )
    market_dataset = _dataset_id(session, MARKET_A)
    action_dataset = _dataset_id(session, ACTIONS_A)
    primitives = _primitives(session)
    cutoff = datetime(2026, 2, 10, 18, tzinfo=UTC)

    historical = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
        end_date=date(2026, 1, 9),
    )
    assert historical.adjustment_basis_date == date(2026, 1, 9)
    assert historical.applicable_adjustments == ()
    assert historical.bars[0].adjusted_close == historical.bars[0].raw_bar.close_price
    assert {action.action_type for action in historical.selected_actions} == {"split", "bonus"}

    bonus_subset = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
        start_date=date(2026, 2, 9),
        end_date=date(2026, 2, 10),
    )
    assert [item.action_type for item in bonus_subset.applicable_adjustments] == ["bonus"]
    assert [bar.adjusted_close for bar in bonus_subset.bars] == [
        Decimal("100.000000"),
        Decimal("100.000000"),
    ]


def test_two_to_one_split_and_one_to_one_bonus_compound_to_exact_quarter(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    service.ingest_market_data(
        _market_provider(
            [
                (
                    "COMPOUND-PRE",
                    _market_record(date(2026, 1, 9), Decimal("400")),
                    datetime(2026, 1, 9, 18, tzinfo=UTC),
                    None,
                ),
                (
                    "COMPOUND-SPLIT",
                    _market_record(date(2026, 1, 10), Decimal("200")),
                    datetime(2026, 1, 10, 18, tzinfo=UTC),
                    None,
                ),
                (
                    "COMPOUND-BONUS",
                    _market_record(date(2026, 1, 11), Decimal("100")),
                    datetime(2026, 1, 11, 18, tzinfo=UTC),
                    None,
                ),
            ]
        )
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                ("COMPOUND-SPLIT", _action_record(), datetime(2026, 1, 2, tzinfo=UTC), None),
                (
                    "COMPOUND-BONUS",
                    _action_record(
                        action_type="bonus",
                        effective_date=date(2026, 1, 11),
                        ratio_numerator=1,
                        ratio_denominator=1,
                    ),
                    datetime(2026, 1, 3, tzinfo=UTC),
                    None,
                ),
            ]
        )
    )
    series = _primitives(session).adjusted_market_series_as_of(
        market_provider_dataset_id=_dataset_id(session, MARKET_A),
        corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 1, 11, 18, tzinfo=UTC),
    )
    assert [item.action_type for item in series.applicable_adjustments] == ["split", "bonus"]
    assert series.bars[0].cumulative_price_factor == Decimal("0.25")
    assert series.bars[0].adjusted_close == Decimal("100.00000000")


def test_late_known_action_and_action_price_revision_composition(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    jan9 = _market_record(date(2026, 1, 9), Decimal("200"))
    jan10 = _market_record(date(2026, 1, 10), Decimal("100"))
    service.ingest_market_data(
        _market_provider(
            [
                ("PRICE-CORR", jan9, datetime(2026, 1, 9, 18, tzinfo=UTC), None),
                ("PRICE-JAN10", jan10, datetime(2026, 1, 10, 18, tzinfo=UTC), None),
            ]
        )
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "SPLIT-CORR",
                    _action_record(),
                    datetime(2026, 1, 12, tzinfo=UTC),
                    None,
                )
            ]
        )
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "SPLIT-CORR",
                    _action_record(ratio_numerator=3),
                    datetime(2026, 1, 13, tzinfo=UTC),
                    datetime(2026, 2, 1, tzinfo=UTC),
                )
            ]
        )
    )
    service.ingest_market_data(
        _market_provider(
            [
                (
                    "PRICE-CORR",
                    _market_record(date(2026, 1, 9), Decimal("300")),
                    datetime(2026, 1, 14, tzinfo=UTC),
                    datetime(2026, 1, 14, tzinfo=UTC),
                )
            ]
        )
    )
    market_dataset = _dataset_id(session, MARKET_A)
    action_dataset = _dataset_id(session, ACTIONS_A)
    primitives = _primitives(session)

    before_known = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 1, 11, tzinfo=UTC),
    )
    original = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 1, 12, tzinfo=UTC),
    )
    corrected_action = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 1, 13, tzinfo=UTC),
    )
    corrected_both = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 1, 14, tzinfo=UTC),
    )
    assert before_known.bars[0].adjusted_close == Decimal("200.000000")
    assert original.bars[0].adjusted_close == Decimal("100.000000")
    assert corrected_action.bars[0].adjusted_close == Decimal("200.000000") * (
        Decimal("1") / Decimal("3")
    )
    assert corrected_action.applicable_adjustments[0].price_factor == Decimal("1") / Decimal("3")
    assert corrected_both.bars[0].raw_bar.close_price == Decimal("300.000000")
    corrected_factor = corrected_both.applicable_adjustments[0].price_factor
    assert corrected_both.bars[0].adjusted_close == Decimal("300.000000") * corrected_factor
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 2


def test_unsupported_actions_do_not_adjust_price_or_create_total_return(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    service.ingest_market_data(
        _market_provider(
            [
                (
                    "DIV-PRE",
                    _market_record(date(2026, 3, 9), Decimal("100")),
                    datetime(2026, 3, 9, 18, tzinfo=UTC),
                    None,
                ),
                (
                    "DIV-EX",
                    _market_record(date(2026, 3, 10), Decimal("95")),
                    datetime(2026, 3, 10, 18, tzinfo=UTC),
                    None,
                ),
            ]
        )
    )
    service.ingest_corporate_actions(
        CSVCorporateActionProvider(
            FIXTURES / "corporate_actions_synthetic.csv", ACTIONS_A, RETRIEVED_AT
        )
    )
    returns = _primitives(session).simple_price_returns_as_of(
        market_provider_dataset_id=_dataset_id(session, MARKET_A),
        corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
        security_id=security_id,
        interval="1d",
        as_of=datetime(2026, 7, 1, tzinfo=UTC),
        start_date=date(2026, 3, 9),
        end_date=date(2026, 3, 10),
    )
    assert len(returns) == 1
    assert returns[0].value == Decimal("-0.05")
    assert returns[0].previous_bar.applied_adjustments == ()
    selected_types = {
        action.action_type
        for action in _primitives(session)
        .adjusted_market_series_as_of(
            market_provider_dataset_id=_dataset_id(session, MARKET_A),
            corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
            security_id=security_id,
            interval="1d",
            as_of=datetime(2026, 7, 1, tzinfo=UTC),
            start_date=date(2026, 3, 9),
            end_date=date(2026, 3, 10),
        )
        .selected_actions
    }
    assert {"cash_dividend", "rights", "symbol_change", "security_replacement"} <= selected_types


def test_no_action_empty_one_bar_zero_denominator_and_missing_date_adjacency(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    service.ingest_market_data(
        _market_provider(
            [
                (
                    "ZERO",
                    _market_record(date(2026, 1, 5), Decimal("0")),
                    datetime(2026, 1, 5, 18, tzinfo=UTC),
                    None,
                ),
                (
                    "TEN",
                    _market_record(date(2026, 1, 7), Decimal("10")),
                    datetime(2026, 1, 7, 18, tzinfo=UTC),
                    None,
                ),
                (
                    "ZERO-CURRENT",
                    _market_record(date(2026, 1, 8), Decimal("0")),
                    datetime(2026, 1, 8, 18, tzinfo=UTC),
                    None,
                ),
            ]
        )
    )
    service.ingest_corporate_actions(_action_provider([], ACTIONS_NONE))
    market_dataset = _dataset_id(session, MARKET_A)
    action_dataset = _dataset_id(session, ACTIONS_NONE)
    primitives = _primitives(session)
    cutoff = datetime(2026, 1, 8, 18, tzinfo=UTC)

    series = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    assert series.selected_actions == () and series.applicable_adjustments == ()
    assert all(bar.cumulative_price_factor == Decimal("1") for bar in series.bars)
    assert [bar.adjusted_close for bar in series.bars] == [
        Decimal("0.000000"),
        Decimal("10.000000"),
        Decimal("0.000000"),
    ]
    returns = primitives.simple_price_returns_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    assert len(returns) == 2
    assert (returns[0].value, returns[0].warnings) == (
        None,
        ("non_positive_previous_close",),
    )
    assert returns[0].previous_trading_date == date(2026, 1, 5)
    assert returns[0].trading_date == date(2026, 1, 7)
    assert returns[1].value == Decimal("-1")
    one_bar = primitives.simple_price_returns_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
        start_date=date(2026, 1, 8),
    )
    assert one_bar == ()
    empty = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=market_dataset,
        corporate_action_provider_dataset_id=action_dataset,
        security_id=UUID(int=0),
        interval="1d",
        as_of=cutoff,
    )
    assert empty.bars == () and empty.adjustment_basis_date is None

    service.ingest_market_data(_market_provider([], MARKET_B))
    service.ingest_corporate_actions(
        _action_provider(
            [("ACTION-WITHOUT-PRICE", _action_record(), datetime(2026, 1, 2, tzinfo=UTC), None)]
        )
    )
    actions_without_prices = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=_dataset_id(session, MARKET_B),
        corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    assert actions_without_prices.bars == ()
    assert actions_without_prices.adjustment_basis_date is None
    assert len(actions_without_prices.selected_actions) == 1


def test_explicit_price_and_action_providers_change_only_their_own_evidence(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    entries = [
        (
            "P-A",
            _market_record(date(2026, 1, 9), Decimal("200")),
            datetime(2026, 1, 9, 18, tzinfo=UTC),
            None,
        ),
        (
            "P-B",
            _market_record(date(2026, 1, 10), Decimal("100")),
            datetime(2026, 1, 10, 18, tzinfo=UTC),
            None,
        ),
    ]
    service.ingest_market_data(_market_provider(entries, MARKET_A))
    service.ingest_market_data(
        _market_provider(
            [
                (
                    entries[0][0],
                    _market_record(date(2026, 1, 9), Decimal("300")),
                    entries[0][2],
                    None,
                ),
                (
                    entries[1][0],
                    _market_record(date(2026, 1, 10), Decimal("100")),
                    entries[1][2],
                    None,
                ),
            ],
            MARKET_B,
        )
    )
    service.ingest_corporate_actions(
        _action_provider([("A-SPLIT", _action_record(), datetime(2026, 1, 2, tzinfo=UTC), None)])
    )
    service.ingest_corporate_actions(
        _action_provider(
            [
                (
                    "B-SPLIT",
                    _action_record(ratio_numerator=3),
                    datetime(2026, 1, 2, tzinfo=UTC),
                    None,
                )
            ],
            ACTIONS_B,
        )
    )
    primitives = _primitives(session)
    cutoff = datetime(2026, 1, 10, 18, tzinfo=UTC)

    a_a = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=_dataset_id(session, MARKET_A),
        corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    a_b = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=_dataset_id(session, MARKET_A),
        corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_B),
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    b_a = primitives.adjusted_market_series_as_of(
        market_provider_dataset_id=_dataset_id(session, MARKET_B),
        corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
        security_id=security_id,
        interval="1d",
        as_of=cutoff,
    )
    assert a_a.bars[0].adjusted_close == Decimal("100.000000")
    assert a_b.bars[0].adjusted_close == Decimal("200.000000") * (Decimal("1") / Decimal("3"))
    assert b_a.bars[0].raw_bar.close_price == Decimal("300.000000")
    assert b_a.bars[0].adjusted_close == Decimal("150.000000")


def test_benchmark_simple_returns_correction_and_denominator_behavior(
    session: Session, tmp_path: Path
) -> None:
    service, _ = _setup(session, tmp_path)
    service.ingest_benchmark_data(
        CSVBenchmarkDataProvider(
            FIXTURES / "benchmark_pit_original_synthetic.csv", BENCHMARKS, RETRIEVED_AT
        )
    )
    primitives = _primitives(session)
    dataset_id = _dataset_id(session, BENCHMARKS)
    before = primitives.simple_benchmark_returns_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 1, 7, 18, 30, tzinfo=UTC),
    )
    service.ingest_benchmark_data(
        CSVBenchmarkDataProvider(
            FIXTURES / "benchmark_pit_correction_synthetic.csv", BENCHMARKS, RETRIEVED_AT
        )
    )
    after = primitives.simple_benchmark_returns_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 1, 8, 18, 30, tzinfo=UTC),
    )
    historical = primitives.simple_benchmark_returns_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 1, 7, 18, 30, tzinfo=UTC),
    )
    assert before[0].value == Decimal("25200") / Decimal("25000") - Decimal("1")
    assert after[0].value == Decimal("25200") / Decimal("25100") - Decimal("1")
    assert historical == before
    assert after[0].algorithm_version == SIMPLE_BENCHMARK_RETURN_VERSION
    assert after[0].available_at == max(
        after[0].previous_bar.available_at, after[0].current_bar.available_at
    )

    service.ingest_benchmark_data(
        _benchmark_provider(
            [
                (
                    "ZERO-BENCH",
                    date(2026, 4, 1),
                    Decimal("0"),
                    datetime(2026, 4, 1, 18, tzinfo=UTC),
                ),
                (
                    "TEN-BENCH",
                    date(2026, 4, 3),
                    Decimal("10"),
                    datetime(2026, 4, 3, 18, tzinfo=UTC),
                ),
            ]
        )
    )
    zero_returns = _primitives(session).simple_benchmark_returns_as_of(
        provider_dataset_id=dataset_id,
        benchmark_code="NIFTY50",
        interval="1d",
        as_of=datetime(2026, 4, 3, 18, tzinfo=UTC),
        start_date=date(2026, 4, 1),
    )
    assert zero_returns[-1].value is None
    assert zero_returns[-1].warnings == ("non_positive_previous_close",)


def test_invalid_adjustment_evidence_and_naive_cutoff_fail_closed(
    session: Session, tmp_path: Path
) -> None:
    service, security_id = _setup(session, tmp_path)
    service.ingest_market_data(
        _market_provider(
            [
                (
                    "PRE",
                    _market_record(date(2026, 1, 9), Decimal("200")),
                    datetime(2026, 1, 9, 18, tzinfo=UTC),
                    None,
                ),
                (
                    "POST",
                    _market_record(date(2026, 1, 10), Decimal("100")),
                    datetime(2026, 1, 10, 18, tzinfo=UTC),
                    None,
                ),
            ]
        )
    )
    service.ingest_corporate_actions(
        _action_provider([("BAD-LATER", _action_record(), datetime(2026, 1, 2, tzinfo=UTC), None)])
    )
    action = session.scalar(select(CorporateAction).where(CorporateAction.action_type == "split"))
    assert action is not None
    action.ratio_numerator = 0
    session.flush()
    primitives = _primitives(session)
    with pytest.raises(ValueError, match="ratio components"):
        primitives.adjusted_market_series_as_of(
            market_provider_dataset_id=_dataset_id(session, MARKET_A),
            corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
            security_id=security_id,
            interval="1d",
            as_of=datetime(2026, 1, 10, 18, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        primitives.adjusted_market_series_as_of(
            market_provider_dataset_id=_dataset_id(session, MARKET_A),
            corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS_A),
            security_id=security_id,
            interval="1d",
            as_of=datetime(2026, 1, 10, 18),
        )
