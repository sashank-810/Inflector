"""Phase 3E-A point-in-time instant financial snapshot tests."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_snapshots import (
    INSTANT_SNAPSHOT_VERSION,
    InstantFinancialSnapshotReader,
)
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import Company, DataProvider, FiscalPeriod, ProviderDataset

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2028, 1, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
MAIN = ProviderMetadata(
    "snapshot_main", "csv", "financials", "synthetic-development-only"
)
PROVIDER_A = ProviderMetadata(
    "snapshot_provider_a", "csv", "financials", "synthetic-development-only"
)
PROVIDER_B = ProviderMetadata(
    "snapshot_provider_b", "csv", "financials", "synthetic-development-only"
)
SCOPE = ProviderMetadata(
    "snapshot_scope", "csv", "financials", "synthetic-development-only"
)
METRICS = ("total_debt", "cash_and_equivalents")


def _at(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _reader(
    session,
    tmp_path: Path,
    *financials: tuple[str, ProviderMetadata],
) -> InstantFinancialSnapshotReader:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    for filename, metadata in financials:
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / filename, metadata, RETRIEVED_AT)
        )
    return InstantFinancialSnapshotReader(PointInTimeFinancialReader(session))


def _main_reader(session, tmp_path: Path) -> InstantFinancialSnapshotReader:
    return _reader(session, tmp_path, ("financial_snapshots_synthetic.csv", MAIN))


def _company_id(session) -> UUID:
    company_id = session.scalar(
        select(Company.id).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    assert company_id is not None
    return company_id


def _dataset_id(session, provider_code: str) -> UUID:
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(
            DataProvider.code == provider_code,
            ProviderDataset.code == "financials",
        )
    )
    assert dataset_id is not None
    return dataset_id


def _period_id(
    session,
    *,
    period_start: date,
    period_end: date,
    period_kind: str = "quarter",
) -> UUID:
    period_id = session.scalar(
        select(FiscalPeriod.id).where(
            FiscalPeriod.company_id == _company_id(session),
            FiscalPeriod.period_kind == period_kind,
            FiscalPeriod.period_start == period_start,
            FiscalPeriod.period_end == period_end,
        )
    )
    assert period_id is not None
    return period_id


def test_snapshot_requires_instant_monetary_inr_metrics(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)
    dataset_id = _dataset_id(session, "snapshot_main")
    company_id = _company_id(session)
    period_id = _period_id(
        session,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
    )
    arguments = {
        "provider_dataset_id": dataset_id,
        "company_id": company_id,
        "filing_scope": "standalone",
        "fiscal_period_id": period_id,
        "as_of": _at(2026, 8, 3),
    }

    eligible = reader.snapshot_for_period_as_of(metric_codes=("total_debt",), **arguments)

    assert eligible is not None
    assert eligible.components[0].value == Decimal("1100000000")
    assert eligible.components[0].unit == "INR"
    assert reader.snapshot_for_period_as_of(metric_codes=("revenue",), **arguments) is None
    assert reader.snapshot_for_period_as_of(metric_codes=("eps_basic",), **arguments) is None
    assert reader.snapshot_for_period_as_of(metric_codes=("total_assets",), **arguments) is None


def test_snapshot_for_period_preserves_fact_lineage(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)
    period_id = _period_id(
        session,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
    )
    snapshot = reader.snapshot_for_period_as_of(
        provider_dataset_id=_dataset_id(session, "snapshot_main"),
        company_id=_company_id(session),
        filing_scope="standalone",
        fiscal_period_id=period_id,
        metric_codes=("total_debt", "cash_and_equivalents"),
        as_of=_at(2026, 8, 3),
    )

    assert snapshot is not None
    assert snapshot.algorithm_version == INSTANT_SNAPSHOT_VERSION
    assert [component.metric_code for component in snapshot.components] == [
        "cash_and_equivalents",
        "total_debt",
    ]
    assert all(component.fact.fiscal_period.id == period_id for component in snapshot.components)
    sources = {component.fact.source_record.external_record_id for component in snapshot.components}
    assert sources == {"SNAP-P2-DEBT", "SNAP-P2-CASH"}
    assert all(component.fact.source_record.raw_object_key for component in snapshot.components)
    payload_references = {
        component.fact.source_record.raw_payload_reference for component in snapshot.components
    }
    assert payload_references == {
        "row-4",
        "row-5",
    }


def test_latest_snapshot_uses_latest_economic_period_not_latest_arrival(
    session, tmp_path: Path
) -> None:
    reader = _main_reader(session, tmp_path)
    snapshot = reader.latest_common_snapshot_as_of(
        provider_dataset_id=_dataset_id(session, "snapshot_main"),
        company_id=_company_id(session),
        filing_scope="standalone",
        metric_codes=METRICS,
        as_of=_at(2026, 9, 2),
    )

    assert snapshot is not None
    assert snapshot.fiscal_period.period_end == date(2026, 6, 30)
    assert {component.metric_code: component.value for component in snapshot.components} == {
        "cash_and_equivalents": Decimal("250000000"),
        "total_debt": Decimal("1100000000"),
    }


def test_latest_snapshot_never_mixes_metric_periods(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)
    dataset_id = _dataset_id(session, "snapshot_main")
    company_id = _company_id(session)
    cutoff = _at(2026, 11, 2)
    debt_only = reader.latest_common_snapshot_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_codes=("total_debt",),
        as_of=cutoff,
    )
    common = reader.latest_common_snapshot_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_codes=METRICS,
        as_of=cutoff,
    )

    assert debt_only is not None and debt_only.fiscal_period.period_end == date(2026, 9, 30)
    assert common is not None and common.fiscal_period.period_end == date(2026, 6, 30)
    assert {component.fact.fiscal_period.id for component in common.components} == {
        common.fiscal_period.id
    }


def test_latest_snapshot_falls_back_to_latest_complete_common_period(
    session, tmp_path: Path
) -> None:
    reader = _main_reader(session, tmp_path)
    snapshot = reader.latest_common_snapshot_as_of(
        provider_dataset_id=_dataset_id(session, "snapshot_main"),
        company_id=_company_id(session),
        filing_scope="standalone",
        metric_codes=("total_debt", "cash_and_equivalents", "inventory"),
        as_of=_at(2026, 11, 2),
    )

    assert snapshot is not None
    assert snapshot.fiscal_period.period_end == date(2026, 6, 30)
    assert tuple(component.metric_code for component in snapshot.components) == (
        "cash_and_equivalents",
        "inventory",
        "total_debt",
    )


def test_snapshot_returns_none_without_common_period(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)

    assert (
        reader.latest_common_snapshot_as_of(
            provider_dataset_id=_dataset_id(session, "snapshot_main"),
            company_id=_company_id(session),
            filing_scope="standalone",
            metric_codes=("total_debt", "trade_payables"),
            as_of=_at(2026, 11, 2),
        )
        is None
    )


def test_snapshot_restatement_changes_only_at_pit_availability(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)
    period_id = _period_id(
        session,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
    )
    arguments = {
        "provider_dataset_id": _dataset_id(session, "snapshot_main"),
        "company_id": _company_id(session),
        "filing_scope": "standalone",
        "fiscal_period_id": period_id,
        "metric_codes": ("total_debt",),
    }

    original = reader.snapshot_for_period_as_of(as_of=_at(2026, 8, 31), **arguments)
    restated = reader.snapshot_for_period_as_of(as_of=_at(2026, 9, 1), **arguments)
    historical = reader.snapshot_for_period_as_of(as_of=_at(2026, 8, 31), **arguments)

    assert original is not None and original.components[0].value == Decimal("1000000000")
    assert restated is not None and restated.components[0].value == Decimal("950000000")
    assert restated.components[0].fact.filing.is_restatement
    assert historical is not None
    assert historical.components[0].fact.id == original.components[0].fact.id


def test_snapshot_available_at_is_latest_component_availability(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)
    snapshot = reader.latest_common_snapshot_as_of(
        provider_dataset_id=_dataset_id(session, "snapshot_main"),
        company_id=_company_id(session),
        filing_scope="standalone",
        metric_codes=METRICS,
        as_of=datetime(
            2026,
            8,
            3,
            17,
            30,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        ),
    )

    assert snapshot is not None
    assert snapshot.available_at == _at(2026, 8, 2)
    assert snapshot.as_of == _at(2026, 8, 3)


def test_snapshot_provider_datasets_are_isolated(session, tmp_path: Path) -> None:
    reader = _reader(
        session,
        tmp_path,
        ("financial_snapshots_provider_a_synthetic.csv", PROVIDER_A),
        ("financial_snapshots_provider_b_synthetic.csv", PROVIDER_B),
    )
    period_id = _period_id(
        session,
        period_start=date(2027, 4, 1),
        period_end=date(2027, 6, 30),
    )
    common = {
        "company_id": _company_id(session),
        "filing_scope": "standalone",
        "fiscal_period_id": period_id,
        "metric_codes": METRICS,
        "as_of": _at(2027, 8, 2),
    }

    assert (
        reader.snapshot_for_period_as_of(
            provider_dataset_id=_dataset_id(session, "snapshot_provider_a"), **common
        )
        is None
    )
    assert (
        reader.snapshot_for_period_as_of(
            provider_dataset_id=_dataset_id(session, "snapshot_provider_b"), **common
        )
        is None
    )


def test_snapshot_filing_scopes_are_isolated(session, tmp_path: Path) -> None:
    reader = _reader(
        session,
        tmp_path,
        ("financial_snapshots_scope_synthetic.csv", SCOPE),
    )
    period_id = _period_id(
        session,
        period_start=date(2027, 7, 1),
        period_end=date(2027, 9, 30),
    )
    common = {
        "provider_dataset_id": _dataset_id(session, "snapshot_scope"),
        "company_id": _company_id(session),
        "fiscal_period_id": period_id,
        "metric_codes": METRICS,
        "as_of": _at(2027, 11, 2),
    }

    assert reader.snapshot_for_period_as_of(filing_scope="standalone", **common) is None
    assert reader.snapshot_for_period_as_of(filing_scope="consolidated", **common) is None


def test_snapshot_rejects_empty_and_duplicate_metric_requests(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)
    common = {
        "provider_dataset_id": _dataset_id(session, "snapshot_main"),
        "company_id": _company_id(session),
        "filing_scope": "standalone",
        "as_of": _at(2026, 8, 3),
    }

    with pytest.raises(ValueError, match="at least one"):
        reader.latest_common_snapshot_as_of(metric_codes=(), **common)
    with pytest.raises(ValueError, match="duplicates"):
        reader.latest_common_snapshot_as_of(
            metric_codes=("total_debt", "total_debt"), **common
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        reader.latest_common_snapshot_as_of(
            metric_codes=("total_debt",),
            **{**common, "as_of": datetime(2026, 8, 3)},
        )


def test_latest_snapshot_refuses_ambiguous_same_end_periods(session, tmp_path: Path) -> None:
    reader = _main_reader(session, tmp_path)

    assert (
        reader.latest_common_snapshot_as_of(
            provider_dataset_id=_dataset_id(session, "snapshot_main"),
            company_id=_company_id(session),
            filing_scope="standalone",
            metric_codes=("total_liabilities", "total_equity"),
            as_of=_at(2027, 5, 2),
        )
        is None
    )
