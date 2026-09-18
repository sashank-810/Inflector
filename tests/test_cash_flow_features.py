"""Phase 3E-C cash-flow quality and working-capital feature tests."""

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
from inflector_data.cash_flow_features import CashFlowQualityFeatures
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
CASH_FLOW = ProviderMetadata("cash_flow_main", "csv", "financials", "synthetic-development-only")
PROVIDER_A = ProviderMetadata(
    "cash_flow_provider_a", "csv", "financials", "synthetic-development-only"
)
PROVIDER_B = ProviderMetadata(
    "cash_flow_provider_b", "csv", "financials", "synthetic-development-only"
)
SCOPE = ProviderMetadata(
    "cash_flow_scope", "csv", "financials", "synthetic-development-only"
)


class _Context(TypedDict):
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str


class _FeatureRequest(_Context):
    fiscal_year: int
    fiscal_quarter: int
    as_of: datetime


def _at(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _features(
    session,
    tmp_path: Path,
    *financials: tuple[str, ProviderMetadata],
) -> CashFlowQualityFeatures:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    for filename, metadata in financials:
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / filename, metadata, RETRIEVED_AT)
        )
    financial_reader = PointInTimeFinancialReader(session)
    return CashFlowQualityFeatures(
        InstantFinancialSnapshotReader(financial_reader),
        TrailingTwelveMonthNormalizer(FiscalQuarterNormalizer(financial_reader)),
    )


def _main_features(session, tmp_path: Path) -> CashFlowQualityFeatures:
    return _features(session, tmp_path, ("financial_cash_flow_synthetic.csv", CASH_FLOW))


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


def _context(session, provider_code: str = "cash_flow_main") -> _Context:
    return {
        "provider_dataset_id": _dataset_id(session, provider_code),
        "company_id": _company_id(session),
        "filing_scope": "standalone",
    }


def test_real_cash_flow_and_working_capital_formulas_preserve_lineage(
    session, tmp_path: Path
) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)
    request = {"fiscal_year": 2026, "fiscal_quarter": 4, "as_of": _at(2027, 5, 3)}

    conversion = features.cfo_conversion_as_of(**context, **request)
    cfo_ebitda = features.cfo_to_ebitda_as_of(**context, **request)
    days = features.receivable_days_as_of(**context, **request)
    working_capital = features.trade_working_capital_change_as_of(**context, **request)

    assert conversion is not None
    assert conversion.cfo == Decimal("1200000000")
    assert conversion.pat == Decimal("1000000000")
    assert conversion.value == Decimal("1.2")
    assert conversion.cash_minus_pat == Decimal("200000000")
    assert conversion.available_at == _at(2027, 5, 1)
    assert cfo_ebitda is not None
    assert cfo_ebitda.value == Decimal("1200000000") / Decimal("1400000000")

    assert days is not None
    assert days.beginning_snapshot.fiscal_period.period_end == date(2026, 3, 31)
    assert days.ending_snapshot.fiscal_period.period_end == date(2027, 3, 31)
    assert days.beginning_receivables == Decimal("400000000")
    assert days.ending_receivables == Decimal("600000000")
    assert days.average_receivables == Decimal("500000000")
    assert days.value == Decimal("500000000") / Decimal("4600000000") * Decimal("365")
    assert days.available_at == _at(2027, 5, 2)

    assert working_capital is not None
    assert working_capital.beginning_trade_working_capital == Decimal("500000000")
    assert working_capital.ending_trade_working_capital == Decimal("700000000")
    assert working_capital.value == Decimal("200000000")
    assert working_capital.ttm_revenue.value == Decimal("4600000000")
    assert all(
        component.fact.source_record.raw_object_key
        for component in working_capital.ending_snapshot.components
    )


def test_cfo_restatement_changes_conversion_only_at_pit_boundary(
    session, tmp_path: Path
) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)

    original = features.cfo_conversion_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 5, 31), **context
    )
    restated = features.cfo_conversion_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 1), **context
    )
    historical = features.cfo_conversion_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 5, 31), **context
    )

    assert original is not None and original.cfo == Decimal("1200000000")
    assert original.value == Decimal("1.2")
    assert restated is not None and restated.cfo == Decimal("1300000000")
    assert restated.value == Decimal("1.3")
    assert restated.cash_minus_pat == Decimal("300000000")
    assert restated.available_at == _at(2027, 6, 1)
    assert historical is not None and historical.value == original.value


def test_receivables_restatement_changes_days_only_at_pit_boundary(
    session, tmp_path: Path
) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)

    original = features.receivable_days_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 14), **context
    )
    restated = features.receivable_days_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 15), **context
    )
    historical = features.receivable_days_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 6, 14), **context
    )

    assert original is not None and original.average_receivables == Decimal("500000000")
    assert restated is not None and restated.ending_receivables == Decimal("700000000")
    assert restated.average_receivables == Decimal("550000000")
    assert restated.value == Decimal("550000000") / Decimal("4600000000") * Decimal("365")
    assert restated.available_at == _at(2027, 6, 15)
    assert historical is not None and historical.value == original.value


def test_cash_flow_features_preserve_provider_and_scope_isolation(session, tmp_path: Path) -> None:
    features = _features(
        session,
        tmp_path,
        ("financial_cash_flow_provider_a_synthetic.csv", PROVIDER_A),
        ("financial_cash_flow_provider_b_synthetic.csv", PROVIDER_B),
        ("financial_cash_flow_scope_synthetic.csv", SCOPE),
    )
    company_id = _company_id(session)
    request = {"fiscal_year": 2026, "fiscal_quarter": 4, "as_of": _at(2027, 5, 3)}

    for provider_code in ("cash_flow_provider_a", "cash_flow_provider_b"):
        context = {
            "provider_dataset_id": _dataset_id(session, provider_code),
            "company_id": company_id,
            "filing_scope": "standalone",
        }
        assert features.cfo_conversion_as_of(**context, **request) is None
        assert features.receivable_days_as_of(**context, **request) is None
    for filing_scope in ("standalone", "consolidated"):
        context = {
            "provider_dataset_id": _dataset_id(session, "cash_flow_scope"),
            "company_id": company_id,
            "filing_scope": filing_scope,
        }
        assert features.cfo_conversion_as_of(**context, **request) is None
        assert features.receivable_days_as_of(**context, **request) is None


def test_cash_flow_features_normalize_offsets_and_reject_naive_time(
    session, tmp_path: Path
) -> None:
    features = _main_features(session, tmp_path)
    context = _context(session)
    ist = datetime(2027, 5, 3, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    result = features.cfo_conversion_as_of(
        fiscal_year=2026, fiscal_quarter=4, as_of=ist, **context
    )

    assert result is not None and result.as_of == _at(2027, 5, 3)
    with pytest.raises(ValueError, match="timezone-aware"):
        features.cfo_conversion_as_of(
            fiscal_year=2026,
            fiscal_quarter=4,
            as_of=datetime(2027, 5, 3),
            **context,
        )


@dataclass
class _SnapshotStub:
    boundaries: dict[date, InstantFinancialSnapshot] | None = None

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
    return InstantFinancialSnapshot(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        fiscal_period=period,
        components=tuple(
            InstantSnapshotComponent(
                metric_code=metric_code,
                value=value,
                unit="INR",
                fact=cast(PointInTimeFinancialFact, object()),
            )
            for metric_code, value in sorted(values.items())
        ),
        as_of=_at(2027, 7, 1),
        available_at=available_at,
        algorithm_version="instant_snapshot_v1",
    )


def _stub_features(
    snapshots: _SnapshotStub,
    ttms: _TTMStub,
) -> CashFlowQualityFeatures:
    return CashFlowQualityFeatures(
        cast(InstantFinancialSnapshotReader, snapshots),
        cast(TrailingTwelveMonthNormalizer, ttms),
    )


def _stub_context() -> tuple[UUID, UUID]:
    return uuid4(), uuid4()


def _feature_request(dataset_id: UUID, company_id: UUID) -> _FeatureRequest:
    return {
        "provider_dataset_id": dataset_id,
        "company_id": company_id,
        "filing_scope": "standalone",
        "fiscal_year": 2026,
        "fiscal_quarter": 4,
        "as_of": _at(2027, 7, 1),
    }


@pytest.mark.parametrize(
    ("cfo", "pat", "expected_ratio", "expected_difference", "warning"),
    [
        (Decimal("150"), Decimal("100"), Decimal("1.5"), Decimal("50"), ()),
        (Decimal("50"), Decimal("100"), Decimal("0.5"), Decimal("-50"), ()),
        (Decimal("-20"), Decimal("100"), Decimal("-0.2"), Decimal("-120"), ()),
        (Decimal("100"), Decimal("0"), None, Decimal("100"), ("non_positive_pat",)),
        (Decimal("100"), Decimal("-50"), None, Decimal("150"), ("non_positive_pat",)),
    ],
)
def test_cfo_conversion_preserves_loss_aware_arithmetic(
    cfo: Decimal,
    pat: Decimal,
    expected_ratio: Decimal | None,
    expected_difference: Decimal,
    warning: tuple[str, ...],
) -> None:
    dataset_id, company_id = _stub_context()
    ttms = _TTMStub(
        {
            "cash_flow_from_operations": _ttm_value(
                "cash_flow_from_operations",
                cfo,
                provider_dataset_id=dataset_id,
                company_id=company_id,
            ),
            "pat": _ttm_value(
                "pat", pat, provider_dataset_id=dataset_id, company_id=company_id
            ),
        }
    )

    result = _stub_features(_SnapshotStub(), ttms).cfo_conversion_as_of(
        **_feature_request(dataset_id, company_id)
    )

    assert result is not None
    assert result.value == expected_ratio and result.warnings == warning
    assert result.cash_minus_pat == expected_difference


def test_cfo_conversion_refuses_missing_or_mismatched_windows_and_tracks_availability() -> None:
    dataset_id, company_id = _stub_context()
    cfo = _ttm_value(
        "cash_flow_from_operations",
        Decimal("100"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 6, 1),
    )
    pat = _ttm_value(
        "pat",
        Decimal("50"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        available_at=_at(2027, 5, 1),
    )
    mismatched_pat = _ttm_value(
        "pat",
        Decimal("50"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        period_end=date(2027, 3, 30),
    )
    request = _feature_request(dataset_id, company_id)

    missing = _stub_features(
        _SnapshotStub(), _TTMStub({"cash_flow_from_operations": cfo})
    ).cfo_conversion_as_of(**request)
    mismatched = _stub_features(
        _SnapshotStub(),
        _TTMStub({"cash_flow_from_operations": cfo, "pat": mismatched_pat}),
    ).cfo_conversion_as_of(**request)
    valid = _stub_features(
        _SnapshotStub(), _TTMStub({"cash_flow_from_operations": cfo, "pat": pat})
    ).cfo_conversion_as_of(**request)

    assert missing is None and mismatched is None
    assert valid is not None and valid.available_at == _at(2027, 6, 1)


@pytest.mark.parametrize(
    ("cfo", "ebitda", "expected", "warning"),
    [
        (Decimal("80"), Decimal("100"), Decimal("0.8"), ()),
        (Decimal("-20"), Decimal("100"), Decimal("-0.2"), ()),
        (Decimal("80"), Decimal("0"), None, ("non_positive_ebitda",)),
        (Decimal("80"), Decimal("-100"), None, ("non_positive_ebitda",)),
    ],
)
def test_cfo_to_ebitda_preserves_signs_and_denominator_rules(
    cfo: Decimal,
    ebitda: Decimal,
    expected: Decimal | None,
    warning: tuple[str, ...],
) -> None:
    dataset_id, company_id = _stub_context()
    ttms = _TTMStub(
        {
            "cash_flow_from_operations": _ttm_value(
                "cash_flow_from_operations",
                cfo,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2027, 6, 1),
            ),
            "ebitda_reported": _ttm_value(
                "ebitda_reported",
                ebitda,
                provider_dataset_id=dataset_id,
                company_id=company_id,
            ),
        }
    )

    result = _stub_features(_SnapshotStub(), ttms).cfo_to_ebitda_as_of(
        **_feature_request(dataset_id, company_id)
    )

    assert result is not None and result.value == expected and result.warnings == warning
    assert result.available_at == _at(2027, 6, 1)


def test_cfo_to_ebitda_refuses_missing_or_mismatched_inputs() -> None:
    dataset_id, company_id = _stub_context()
    cfo = _ttm_value(
        "cash_flow_from_operations",
        Decimal("80"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
    )
    ebitda = _ttm_value(
        "ebitda_reported",
        Decimal("100"),
        provider_dataset_id=dataset_id,
        company_id=company_id,
        period_start=date(2026, 4, 2),
    )
    request = _feature_request(dataset_id, company_id)

    assert (
        _stub_features(
            _SnapshotStub(), _TTMStub({"cash_flow_from_operations": cfo})
        ).cfo_to_ebitda_as_of(**request)
        is None
    )
    assert (
        _stub_features(
            _SnapshotStub(),
            _TTMStub({"cash_flow_from_operations": cfo, "ebitda_reported": ebitda}),
        ).cfo_to_ebitda_as_of(**request)
        is None
    )


def _receivable_features(
    beginning: Decimal,
    ending: Decimal,
    revenue: Decimal,
    *,
    beginning_available: datetime = datetime(2026, 5, 1, 12, tzinfo=UTC),
    ending_available: datetime = datetime(2027, 5, 1, 12, tzinfo=UTC),
) -> tuple[CashFlowQualityFeatures, UUID, UUID]:
    dataset_id, company_id = _stub_context()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    snapshots = _SnapshotStub(
        boundaries={
            beginning_date: _snapshot(
                beginning_date,
                {"trade_receivables": beginning},
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=beginning_available,
            ),
            ending_date: _snapshot(
                ending_date,
                {"trade_receivables": ending},
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=ending_available,
            ),
        }
    )
    ttm = _ttm_value(
        "revenue", revenue, provider_dataset_id=dataset_id, company_id=company_id
    )
    return _stub_features(snapshots, _TTMStub({"revenue": ttm})), dataset_id, company_id


@pytest.mark.parametrize(
    ("beginning", "ending", "revenue", "expected", "average", "warning"),
    [
        (Decimal("0"), Decimal("0"), Decimal("100"), Decimal("0"), Decimal("0"), ()),
        (
            Decimal("10"),
            Decimal("20"),
            Decimal("0"),
            None,
            Decimal("15"),
            ("non_positive_revenue",),
        ),
        (
            Decimal("10"),
            Decimal("20"),
            Decimal("-1"),
            None,
            Decimal("15"),
            ("non_positive_revenue",),
        ),
        (
            Decimal("-1"),
            Decimal("20"),
            Decimal("100"),
            None,
            None,
            ("negative_trade_receivables",),
        ),
        (
            Decimal("10"),
            Decimal("-1"),
            Decimal("100"),
            None,
            None,
            ("negative_trade_receivables",),
        ),
    ],
)
def test_receivable_days_handles_zero_and_invalid_inputs(
    beginning: Decimal,
    ending: Decimal,
    revenue: Decimal,
    expected: Decimal | None,
    average: Decimal | None,
    warning: tuple[str, ...],
) -> None:
    features, dataset_id, company_id = _receivable_features(beginning, ending, revenue)

    result = features.receivable_days_as_of(**_feature_request(dataset_id, company_id))

    assert result is not None and result.value == expected
    assert result.average_receivables == average and result.warnings == warning


def test_receivable_days_requires_exact_boundaries_and_uses_latest_availability() -> None:
    features, dataset_id, company_id = _receivable_features(
        Decimal("10"),
        Decimal("20"),
        Decimal("100"),
        ending_available=_at(2027, 6, 1),
    )
    request = _feature_request(dataset_id, company_id)
    valid = features.receivable_days_as_of(**request)
    revenue = _ttm_value(
        "revenue", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id
    )
    missing = _stub_features(
        _SnapshotStub(
            boundaries={
                date(2026, 2, 28): _snapshot(
                    date(2026, 2, 28),
                    {"trade_receivables": Decimal("10")},
                    provider_dataset_id=dataset_id,
                    company_id=company_id,
                    available_at=_at(2026, 5, 1),
                )
            }
        ),
        _TTMStub({"revenue": revenue}),
    ).receivable_days_as_of(**request)

    assert valid is not None and valid.available_at == _at(2027, 6, 1)
    assert missing is None


@pytest.mark.parametrize(
    ("beginning", "ending", "expected"),
    [
        (
            (Decimal("40"), Decimal("30"), Decimal("20")),
            (Decimal("60"), Decimal("35"), Decimal("25")),
            Decimal("20"),
        ),
        (
            (Decimal("60"), Decimal("35"), Decimal("25")),
            (Decimal("40"), Decimal("30"), Decimal("20")),
            Decimal("-20"),
        ),
        (
            (Decimal("40"), Decimal("30"), Decimal("20")),
            (Decimal("50"), Decimal("20"), Decimal("20")),
            Decimal("0"),
        ),
    ],
)
def test_trade_working_capital_change_preserves_change_sign(
    beginning: tuple[Decimal, Decimal, Decimal],
    ending: tuple[Decimal, Decimal, Decimal],
    expected: Decimal,
) -> None:
    dataset_id, company_id = _stub_context()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)

    def values(parts: tuple[Decimal, Decimal, Decimal]) -> dict[str, Decimal]:
        return dict(zip(("trade_receivables", "inventory", "trade_payables"), parts))

    snapshots = _SnapshotStub(
        boundaries={
            beginning_date: _snapshot(
                beginning_date,
                values(beginning),
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2026, 5, 1),
            ),
            ending_date: _snapshot(
                ending_date,
                values(ending),
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2027, 6, 1),
            ),
        }
    )
    revenue = _ttm_value(
        "revenue", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id
    )

    features = _stub_features(snapshots, _TTMStub({"revenue": revenue}))
    result = features.trade_working_capital_change_as_of(
        **_feature_request(dataset_id, company_id)
    )

    assert result is not None and result.value == expected
    assert result.beginning_trade_working_capital == beginning[0] + beginning[1] - beginning[2]
    assert result.ending_trade_working_capital == ending[0] + ending[1] - ending[2]
    assert result.available_at == _at(2027, 6, 1)


@pytest.mark.parametrize("boundary", ["beginning", "ending"])
@pytest.mark.parametrize("negative_metric", ["trade_receivables", "inventory", "trade_payables"])
def test_trade_working_capital_rejects_negative_components(
    boundary: str,
    negative_metric: str,
) -> None:
    dataset_id, company_id = _stub_context()
    beginning_date, ending_date = date(2026, 3, 31), date(2027, 3, 31)
    beginning = {
        "trade_receivables": Decimal("10"),
        "inventory": Decimal("10"),
        "trade_payables": Decimal("10"),
    }
    ending = {
        "trade_receivables": Decimal("10"),
        "inventory": Decimal("10"),
        "trade_payables": Decimal("10"),
    }
    target = beginning if boundary == "beginning" else ending
    target[negative_metric] = Decimal("-1")
    snapshots = _SnapshotStub(
        boundaries={
            beginning_date: _snapshot(
                beginning_date,
                beginning,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2026, 5, 1),
            ),
            ending_date: _snapshot(
                ending_date,
                ending,
                provider_dataset_id=dataset_id,
                company_id=company_id,
                available_at=_at(2027, 5, 1),
            ),
        }
    )
    revenue = _ttm_value(
        "revenue", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id
    )

    features = _stub_features(snapshots, _TTMStub({"revenue": revenue}))
    result = features.trade_working_capital_change_as_of(
        **_feature_request(dataset_id, company_id)
    )

    assert result is not None and result.value is None
    assert result.warnings == ("negative_working_capital_component",)


def test_trade_working_capital_requires_unambiguous_exact_boundaries() -> None:
    dataset_id, company_id = _stub_context()
    revenue = _ttm_value(
        "revenue", Decimal("100"), provider_dataset_id=dataset_id, company_id=company_id
    )

    result = _stub_features(
        _SnapshotStub(boundaries={}), _TTMStub({"revenue": revenue})
    ).trade_working_capital_change_as_of(**_feature_request(dataset_id, company_id))

    assert result is None
