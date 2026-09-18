"""Phase 3E-B leverage and capital-efficiency feature tests."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.capital_features import CapitalEfficiencyFeatures
from inflector_data.financial_snapshots import (
    InstantFinancialSnapshot,
    InstantFinancialSnapshotReader,
    InstantSnapshotComponent,
)
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import (
    FinancialPeriodView,
    PointInTimeFinancialFact,
    PointInTimeFinancialReader,
)
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_data.ttm import TrailingTwelveMonthNormalizer, TrailingTwelveMonthValue
from inflector_database.models import Company, DataProvider, ProviderDataset

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2028, 3, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
CAPITAL = ProviderMetadata("capital_main", "csv", "financials", "synthetic-development-only")
PROVIDER_A = ProviderMetadata(
    "snapshot_provider_a", "csv", "financials", "synthetic-development-only"
)
PROVIDER_B = ProviderMetadata(
    "snapshot_provider_b", "csv", "financials", "synthetic-development-only"
)
SCOPE = ProviderMetadata(
    "snapshot_scope", "csv", "financials", "synthetic-development-only"
)


def _at(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _features(
    session,
    tmp_path: Path,
    *financials: tuple[str, ProviderMetadata],
) -> CapitalEfficiencyFeatures:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    for filename, metadata in financials:
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / filename, metadata, RETRIEVED_AT)
        )
    financial_reader = PointInTimeFinancialReader(session)
    return CapitalEfficiencyFeatures(
        InstantFinancialSnapshotReader(financial_reader),
        TrailingTwelveMonthNormalizer(FiscalQuarterNormalizer(financial_reader)),
    )


def _main_features(session, tmp_path: Path) -> CapitalEfficiencyFeatures:
    return _features(session, tmp_path, ("financial_capital_synthetic.csv", CAPITAL))


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


class _Context(TypedDict):
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str


def _context(session, provider_code: str = "capital_main") -> _Context:
    return {
        "provider_dataset_id": _dataset_id(session, provider_code),
        "company_id": _company_id(session),
        "filing_scope": "standalone",
    }


def test_real_capital_features_use_exact_pit_inputs_and_lineage(session, tmp_path: Path) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)
    as_of = _at(2027, 5, 3)

    net_debt = features.net_debt_as_of(as_of=as_of, **context)
    debt_equity = features.debt_to_equity_as_of(as_of=as_of, **context)
    coverage = features.interest_coverage_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=as_of, **context
    )
    roe = features.roe_as_of(fiscal_year=2026, fiscal_quarter=4, as_of=as_of, **context)
    roce = features.roce_as_of(fiscal_year=2026, fiscal_quarter=4, as_of=as_of, **context)

    assert net_debt is not None and net_debt.value == Decimal("1500000000")
    assert net_debt.available_at == _at(2027, 5, 2)
    assert debt_equity is not None
    assert debt_equity.value == Decimal("2200000000") / Decimal("6000000000")
    assert coverage is not None and coverage.value == Decimal("7")
    assert coverage.available_at == _at(2027, 5, 1)

    assert roe is not None
    assert roe.ttm_pat.value == Decimal("1000000000")
    assert roe.beginning_snapshot.fiscal_period.period_end == date(2026, 3, 31)
    assert roe.ending_snapshot.fiscal_period.period_end == date(2027, 3, 31)
    assert roe.beginning_equity == Decimal("5000000000")
    assert roe.ending_equity == Decimal("6000000000")
    assert roe.average_equity == Decimal("5500000000")
    assert roe.value == Decimal("1000000000") / Decimal("5500000000")
    assert roe.available_at == _at(2027, 5, 2)

    assert roce is not None
    assert roce.beginning_capital_employed == Decimal("6500000000")
    assert roce.ending_capital_employed == Decimal("7500000000")
    assert roce.average_capital_employed == Decimal("7000000000")
    assert roce.value == Decimal("0.2")
    assert roce.available_at == _at(2027, 5, 2)
    assert all(
        component.fact.source_record.raw_object_key
        for component in roce.ending_snapshot.components
    )


def test_net_debt_restatement_changes_only_at_availability(session, tmp_path: Path) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)

    original = features.net_debt_as_of(as_of=_at(2027, 5, 31), **context)
    restated = features.net_debt_as_of(as_of=_at(2027, 6, 1), **context)
    historical = features.net_debt_as_of(as_of=_at(2027, 5, 31), **context)

    assert original is not None and original.value == Decimal("1500000000")
    assert restated is not None and restated.value == Decimal("1400000000")
    assert historical is not None and historical.value == original.value
    assert restated.snapshot.fiscal_period.id == original.snapshot.fiscal_period.id


def test_roe_boundary_restatement_changes_only_at_availability(session, tmp_path: Path) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)

    original = features.roe_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 14), **context
    )
    restated = features.roe_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 15), **context
    )
    historical = features.roe_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 14), **context
    )

    assert original is not None and original.average_equity == Decimal("5500000000")
    assert restated is not None and restated.average_equity == Decimal("5750000000")
    assert restated.value == Decimal("1000000000") / Decimal("5750000000")
    assert restated.available_at == _at(2027, 6, 15)
    assert historical is not None and historical.value == original.value


def test_net_cash_and_non_positive_equity_are_not_repaired(session, tmp_path: Path) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)

    net_cash = features.net_debt_as_of(as_of=_at(2027, 8, 2), **context)
    zero_equity = features.debt_to_equity_as_of(as_of=_at(2027, 11, 2), **context)
    negative_equity = features.debt_to_equity_as_of(as_of=_at(2028, 2, 2), **context)

    assert net_cash is not None and net_cash.value == Decimal("-300000000")
    assert zero_equity is not None and zero_equity.equity == Decimal("0")
    assert zero_equity.value is None and zero_equity.warnings == ("non_positive_equity",)
    assert negative_equity is not None and negative_equity.equity == Decimal("-100000000")
    assert negative_equity.value is None
    assert negative_equity.warnings == ("non_positive_equity",)


def test_net_debt_uses_latest_complete_common_snapshot(session, tmp_path: Path) -> None:
    features = _features(
        session,
        tmp_path,
        (
            "financial_snapshots_synthetic.csv",
            ProviderMetadata(
                "snapshot_main", "csv", "financials", "synthetic-development-only"
            ),
        ),
    )

    result = features.net_debt_as_of(
        provider_dataset_id=_dataset_id(session, "snapshot_main"),
        company_id=_company_id(session),
        filing_scope="standalone",
        as_of=_at(2026, 11, 2),
    )

    assert result is not None
    assert result.snapshot.fiscal_period.period_end == date(2026, 6, 30)
    assert result.value == Decimal("850000000")


def test_capital_features_normalize_aware_offsets_and_reject_naive_time(
    session, tmp_path: Path
) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)
    ist = datetime(2027, 5, 3, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    result = features.net_debt_as_of(as_of=ist, **context)

    assert result is not None and result.as_of == _at(2027, 5, 3)
    with pytest.raises(ValueError, match="timezone-aware"):
        features.net_debt_as_of(as_of=datetime(2027, 5, 3), **context)


def test_capital_features_preserve_provider_and_scope_isolation(session, tmp_path: Path) -> None:
    features = _features(
        session,
        tmp_path,
        ("financial_snapshots_provider_a_synthetic.csv", PROVIDER_A),
        ("financial_snapshots_provider_b_synthetic.csv", PROVIDER_B),
        ("financial_snapshots_scope_synthetic.csv", SCOPE),
    )
    company_id = _company_id(session)

    for provider_code in ("snapshot_provider_a", "snapshot_provider_b"):
        assert (
            features.net_debt_as_of(
                provider_dataset_id=_dataset_id(session, provider_code),
                company_id=company_id,
                filing_scope="standalone",
                as_of=_at(2027, 8, 2),
            )
            is None
        )
    for filing_scope in ("standalone", "consolidated"):
        assert (
            features.net_debt_as_of(
                provider_dataset_id=_dataset_id(session, "snapshot_scope"),
                company_id=company_id,
                filing_scope=filing_scope,
                as_of=_at(2027, 11, 2),
            )
            is None
        )


@dataclass
class _SnapshotStub:
    latest: InstantFinancialSnapshot | None = None
    boundaries: dict[date, InstantFinancialSnapshot] | None = None

    def latest_common_snapshot_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_codes: Sequence[str],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        return self.latest

    def common_snapshot_for_period_end_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        period_end: date,
        metric_codes: Sequence[str],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        return None if self.boundaries is None else self.boundaries.get(period_end)


@dataclass
class _TTMStub:
    values: dict[str, TrailingTwelveMonthValue]

    def ttm_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        fiscal_year: int,
        fiscal_quarter: int,
        as_of: datetime,
    ) -> TrailingTwelveMonthValue | None:
        return self.values.get(metric_code)


def _ttm_value(
    metric_code: str,
    value: Decimal,
    *,
    provider_dataset_id: UUID,
    company_id: UUID,
    available_at: datetime = datetime(2027, 5, 1, 12, tzinfo=UTC),
    period_start: date = date(2026, 4, 1),
    period_end: date = date(2027, 3, 31),
) -> TrailingTwelveMonthValue:
    return TrailingTwelveMonthValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_code=metric_code,
        ending_fiscal_year=2026,
        ending_fiscal_quarter=4,
        period_start=period_start,
        period_end=period_end,
        value=value,
        unit="INR",
        construction_kind="four_quarters",
        operation="sum_four_quarters",
        algorithm_version="ttm_v1",
        as_of=_at(2027, 7, 1),
        available_at=available_at,
        lineage=(),
    )


def _snapshot(
    period_end: date,
    values: dict[str, Decimal],
    *,
    provider_dataset_id: UUID,
    company_id: UUID,
    available_at: datetime,
) -> InstantFinancialSnapshot:
    period = FinancialPeriodView(
        id=uuid4(),
        period_kind="annual",
        period_start=period_end - timedelta(days=364),
        period_end=period_end,
        fiscal_year=period_end.year,
        fiscal_quarter=None,
        is_ytd=False,
    )
    components = tuple(
        InstantSnapshotComponent(
            metric_code=metric_code,
            value=value,
            unit="INR",
            fact=cast(PointInTimeFinancialFact, object()),
        )
        for metric_code, value in sorted(values.items())
    )
    return InstantFinancialSnapshot(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_period=period,
        components=components,
        as_of=_at(2027, 7, 1),
        available_at=available_at,
        algorithm_version="instant_snapshot_v1",
    )


def _stub_features(
    snapshots: _SnapshotStub,
    ttms: _TTMStub,
) -> CapitalEfficiencyFeatures:
    return CapitalEfficiencyFeatures(
        cast(InstantFinancialSnapshotReader, snapshots),
        cast(TrailingTwelveMonthNormalizer, ttms),
    )


@pytest.mark.parametrize(
    ("ebit", "finance_cost", "expected", "warning"),
    [
        (Decimal("-100"), Decimal("20"), Decimal("-5"), ()),
        (Decimal("100"), Decimal("0"), None, ("non_positive_finance_cost",)),
        (Decimal("100"), Decimal("-20"), None, ("non_positive_finance_cost",)),
    ],
)
def test_interest_coverage_preserves_signs_and_refuses_non_positive_cost(
    ebit: Decimal,
    finance_cost: Decimal,
    expected: Decimal | None,
    warning: tuple[str, ...],
) -> None:
    dataset_id, company_id = uuid4(), uuid4()
    ttms = _TTMStub(
        {
            "ebit": _ttm_value("ebit", ebit, provider_dataset_id=dataset_id, company_id=company_id),
            "finance_cost": _ttm_value(
                "finance_cost", finance_cost, provider_dataset_id=dataset_id, company_id=company_id
            ),
        }
    )
    features = _stub_features(_SnapshotStub(), ttms)

    result = features.interest_coverage_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 7, 1),
    )

    assert result is not None and result.value == expected and result.warnings == warning


def test_interest_coverage_refuses_missing_or_mismatched_ttm_inputs() -> None:
    dataset_id, company_id = uuid4(), uuid4()
    ebit = _ttm_value("ebit", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id)
    mismatched_cost = _ttm_value(
        "finance_cost",
        Decimal("20"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        period_end=date(2027, 3, 30),
    )
    context = {
        "provider_dataset_id": dataset_id,
        "company_id": company_id,
        "filing_scope": "standalone",
        "fiscal_year": 2026,
        "fiscal_quarter": 4,
        "as_of": _at(2027, 7, 1),
    }

    assert (
        _stub_features(_SnapshotStub(), _TTMStub({"ebit": ebit})).interest_coverage_as_of(
            **context
        )
        is None
    )
    assert (
        _stub_features(
            _SnapshotStub(), _TTMStub({"ebit": ebit, "finance_cost": mismatched_cost})
        ).interest_coverage_as_of(**context)
        is None
    )


def test_interest_coverage_availability_is_latest_ttm_input() -> None:
    dataset_id, company_id = uuid4(), uuid4()
    ebit = _ttm_value(
        "ebit",
        Decimal("100"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 5, 1),
    )
    finance_cost = _ttm_value(
        "finance_cost",
        Decimal("20"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 6, 1),
    )

    result = _stub_features(
        _SnapshotStub(), _TTMStub({"ebit": ebit, "finance_cost": finance_cost})
    ).interest_coverage_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 7, 1),
    )

    assert result is not None and result.value == Decimal("5")
    assert result.available_at == _at(2027, 6, 1)


@pytest.mark.parametrize(
    ("beginning_equity", "ending_equity"),
    [(Decimal("0"), Decimal("600")), (Decimal("500"), Decimal("-1"))],
)
def test_roe_refuses_non_positive_equity(
    beginning_equity: Decimal, ending_equity: Decimal
) -> None:
    dataset_id, company_id = uuid4(), uuid4()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    snapshots = _SnapshotStub(
        boundaries={
            beginning_date: _snapshot(
                beginning_date,
                {"total_equity": beginning_equity},
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2026, 5, 1),
            ),
            ending_date: _snapshot(
                ending_date,
                {"total_equity": ending_equity},
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2027, 5, 1),
            ),
        }
    )
    pat = _ttm_value("pat", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id)

    result = _stub_features(snapshots, _TTMStub({"pat": pat})).roe_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 7, 1),
    )

    assert result is not None and result.value is None
    assert result.average_equity is None and result.warnings == ("non_positive_equity",)


def test_roe_accepts_negative_pat_and_requires_both_exact_boundaries() -> None:
    dataset_id, company_id = uuid4(), uuid4()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    beginning = _snapshot(
        beginning_date,
        {"total_equity": Decimal("500")},
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2026, 5, 1),
    )
    ending = _snapshot(
        ending_date,
        {"total_equity": Decimal("600")},
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 6, 1),
    )
    pat = _ttm_value(
        "pat",
        Decimal("-55"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 5, 15),
    )
    context = {
        "provider_dataset_id": dataset_id,
        "company_id": company_id,
        "filing_scope": "standalone",
        "fiscal_year": 2026,
        "fiscal_quarter": 4,
        "as_of": _at(2027, 7, 1),
    }

    valid = _stub_features(
        _SnapshotStub(boundaries={beginning_date: beginning, ending_date: ending}),
        _TTMStub({"pat": pat}),
    ).roe_as_of(**context)
    missing = _stub_features(
        _SnapshotStub(boundaries={ending_date: ending}), _TTMStub({"pat": pat})
    ).roe_as_of(**context)

    assert valid is not None and valid.value == Decimal("-0.1")
    assert valid.available_at == _at(2027, 6, 1)
    assert missing is None


def test_roe_returns_none_for_missing_or_ambiguous_exact_boundary() -> None:
    dataset_id, company_id = uuid4(), uuid4()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    beginning = _snapshot(
        beginning_date,
        {"total_equity": Decimal("500")},
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2026, 5, 1),
    )
    ending = _snapshot(
        ending_date,
        {"total_equity": Decimal("600")},
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 5, 1),
    )
    pat = _ttm_value("pat", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id)
    context = {
        "provider_dataset_id": dataset_id,
        "company_id": company_id,
        "filing_scope": "standalone",
        "fiscal_year": 2026,
        "fiscal_quarter": 4,
        "as_of": _at(2027, 7, 1),
    }

    missing_beginning = _stub_features(
        _SnapshotStub(boundaries={ending_date: ending}), _TTMStub({"pat": pat})
    ).roe_as_of(**context)
    missing_or_ambiguous_ending = _stub_features(
        _SnapshotStub(boundaries={beginning_date: beginning}), _TTMStub({"pat": pat})
    ).roe_as_of(**context)

    assert missing_beginning is None
    assert missing_or_ambiguous_ending is None


@pytest.mark.parametrize(
    ("beginning_values", "ending_values"),
    [
        (
            {
                "total_equity": Decimal("10"),
                "total_debt": Decimal("5"),
                "cash_and_equivalents": Decimal("15"),
            },
            {
                "total_equity": Decimal("20"),
                "total_debt": Decimal("5"),
                "cash_and_equivalents": Decimal("5"),
            },
        ),
        (
            {
                "total_equity": Decimal("20"),
                "total_debt": Decimal("5"),
                "cash_and_equivalents": Decimal("5"),
            },
            {
                "total_equity": Decimal("10"),
                "total_debt": Decimal("5"),
                "cash_and_equivalents": Decimal("16"),
            },
        ),
    ],
)
def test_roce_refuses_non_positive_boundary_capital(
    beginning_values: dict[str, Decimal], ending_values: dict[str, Decimal]
) -> None:
    dataset_id, company_id = uuid4(), uuid4()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    snapshots = _SnapshotStub(
        boundaries={
            beginning_date: _snapshot(
                beginning_date,
                beginning_values,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2026, 5, 1),
            ),
            ending_date: _snapshot(
                ending_date,
                ending_values,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2027, 5, 1),
            ),
        }
    )
    ebit = _ttm_value("ebit", Decimal("10"), provider_dataset_id=dataset_id, company_id=company_id)

    result = _stub_features(snapshots, _TTMStub({"ebit": ebit})).roce_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 7, 1),
    )

    assert result is not None and result.value is None
    assert result.average_capital_employed is None
    assert result.warnings == ("non_positive_capital_employed",)


def test_roce_accepts_negative_ebit_and_uses_latest_required_availability() -> None:
    dataset_id, company_id = uuid4(), uuid4()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    values = {
        "total_equity": Decimal("20"),
        "total_debt": Decimal("5"),
        "cash_and_equivalents": Decimal("5"),
    }
    snapshots = _SnapshotStub(
        boundaries={
            beginning_date: _snapshot(
                beginning_date,
                values,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2026, 5, 1),
            ),
            ending_date: _snapshot(
                ending_date,
                values,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2027, 6, 1),
            ),
        }
    )
    ebit = _ttm_value(
        "ebit",
        Decimal("-10"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 5, 15),
    )

    result = _stub_features(snapshots, _TTMStub({"ebit": ebit})).roce_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 7, 1),
    )

    assert result is not None and result.value == Decimal("-0.5")
    assert result.beginning_capital_employed == Decimal("20")
    assert result.ending_capital_employed == Decimal("20")
    assert result.available_at == _at(2027, 6, 1)


def test_roce_returns_none_without_both_exact_boundary_snapshots() -> None:
    dataset_id, company_id = uuid4(), uuid4()
    ending_date = date(2027, 3, 31)
    ending = _snapshot(
        ending_date,
        {
            "total_equity": Decimal("20"),
            "total_debt": Decimal("5"),
            "cash_and_equivalents": Decimal("5"),
        },
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 5, 1),
    )
    ebit = _ttm_value("ebit", Decimal("10"), provider_dataset_id=dataset_id, company_id=company_id)

    result = _stub_features(
        _SnapshotStub(boundaries={ending_date: ending}), _TTMStub({"ebit": ebit})
    ).roce_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 7, 1),
    )

    assert result is None
