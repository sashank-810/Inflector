"""Phase 3H-A deterministic valuation feature tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_snapshots import InstantFinancialSnapshotReader
from inflector_data.market_pit import PointInTimeMarketReader
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import (
    CSVFinancialsProvider,
    CSVMarketDataProvider,
    CSVUniverseProvider,
)
from inflector_data.service import IngestionService
from inflector_data.ttm import TrailingTwelveMonthNormalizer
from inflector_data.valuation_features import ValuationFeaturePrimitives
from inflector_database.models import Company, DataProvider, ProviderDataset, Security

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2027, 6, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("valuation_universe", "csv", "universe", "synthetic")
MARKET = ProviderMetadata("valuation_market", "csv", "market_daily", "synthetic")
FINANCIAL = ProviderMetadata("valuation_financial", "csv", "financials", "synthetic")


def _dataset_id(session, code: str, dataset: str) -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(DataProvider.code == code, ProviderDataset.code == dataset)
    )
    assert value is not None
    return value


def _setup(session, tmp_path: Path, *, restatements: bool = False, market_null: bool = False):
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    service.ingest_market_data(
        CSVMarketDataProvider(FIXTURES / "valuation_market_synthetic.csv", MARKET, RETRIEVED_AT)
    )
    if market_null:
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "valuation_market_null_synthetic.csv", MARKET, RETRIEVED_AT
            )
        )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "valuation_financials_synthetic.csv", FINANCIAL, RETRIEVED_AT
        )
    )
    if restatements:
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "valuation_market_correction_synthetic.csv", MARKET, RETRIEVED_AT
            )
        )
        service.ingest_financials(
            CSVFinancialsProvider(
                FIXTURES / "valuation_financials_restatement_synthetic.csv",
                FINANCIAL,
                RETRIEVED_AT,
            )
        )
    security = session.scalar(select(Security).where(Security.isin == "INF0AUR01018"))
    company = session.scalar(
        select(Company).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    assert security is not None and company is not None
    market_reader = PointInTimeMarketReader(session)
    financial_reader = PointInTimeFinancialReader(session)
    primitives = ValuationFeaturePrimitives(
        session,
        market_reader,
        TrailingTwelveMonthNormalizer(FiscalQuarterNormalizer(financial_reader)),
        InstantFinancialSnapshotReader(financial_reader),
    )
    arguments = {
        "market_provider_dataset_id": _dataset_id(session, "valuation_market", "market_daily"),
        "financial_provider_dataset_id": _dataset_id(session, "valuation_financial", "financials"),
        "security_id": security.id,
        "company_id": company.id,
        "filing_scope": "standalone",
        "ending_fiscal_year": 2026,
        "ending_fiscal_quarter": 4,
        "interval": "1d",
    }
    return service, primitives, arguments


def _bundle(primitives, arguments, as_of: datetime):
    return primitives.features_as_of(as_of=as_of, **arguments)


def test_full_real_pit_bundle_exact_values_lineage_and_availability(
    session, tmp_path: Path
) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    bundle = _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC))

    assert bundle.market_bar is not None
    assert bundle.market_bar.market_cap == Decimal("1000")
    assert bundle.market_bar.source_record.validation_status == "accepted"
    assert bundle.ttm_pat is not None and bundle.ttm_pat.value == Decimal("100")
    assert bundle.ttm_revenue is not None and bundle.ttm_revenue.value == Decimal("500")
    assert bundle.ttm_ebitda is not None and bundle.ttm_ebitda.value == Decimal("250.0")
    assert bundle.market_cap_to_ttm_pat.value == Decimal("10")
    assert bundle.market_cap_to_total_equity.value == Decimal("2.5")
    assert bundle.market_cap_to_ttm_revenue.value == Decimal("2")
    assert bundle.simplified_enterprise_value.net_debt == Decimal("250")
    assert bundle.simplified_enterprise_value.value == Decimal("1250")
    assert bundle.simplified_ev_to_ttm_ebitda.value == Decimal("5")
    assert bundle.simplified_ev_to_ttm_revenue.value == Decimal("2.5")
    assert bundle.market_cap_to_ttm_pat.available_at == bundle.ttm_pat.available_at
    assert bundle.net_debt_snapshot is not None
    assert bundle.simplified_enterprise_value.available_at == bundle.net_debt_snapshot.available_at
    assert bundle.market_cap_to_ttm_pat.evidence == (bundle.market_bar, bundle.ttm_pat)
    assert bundle.market_cap_to_total_equity.evidence == (
        bundle.market_bar,
        bundle.equity_snapshot,
    )
    assert len(bundle.ttm_pat.lineage) == 4


def test_market_and_financial_corrections_obey_inclusive_pit_boundaries(session, tmp_path) -> None:
    _, primitives, arguments = _setup(session, tmp_path, restatements=True)
    before_market = _bundle(primitives, arguments, datetime(2027, 5, 10, 17, 59, tzinfo=UTC))
    at_market = _bundle(primitives, arguments, datetime(2027, 5, 10, 18, tzinfo=UTC))
    before_financial = _bundle(primitives, arguments, datetime(2027, 5, 15, 11, 59, tzinfo=UTC))
    at_financial = _bundle(primitives, arguments, datetime(2027, 5, 15, 12, tzinfo=UTC))

    assert before_market.market_cap_to_ttm_pat.value == Decimal("10")
    assert at_market.market_cap_to_ttm_pat.value == Decimal("12")
    assert before_financial.market_cap_to_total_equity.value == Decimal("3")
    assert at_financial.market_cap_to_ttm_pat.value == Decimal("15")
    assert at_financial.market_cap_to_total_equity.value == Decimal("2.4")
    assert _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC)) == _bundle(
        primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC)
    )


def test_latest_atomic_market_bar_with_null_market_cap_has_no_fallback(session, tmp_path) -> None:
    _, primitives, arguments = _setup(session, tmp_path, market_null=True)
    bundle = _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC))

    assert bundle.market_bar is not None
    assert bundle.market_bar.trading_date.isoformat() == "2027-04-11"
    assert bundle.market_bar.market_cap is None
    assert bundle.simplified_enterprise_value.value is None
    assert bundle.simplified_enterprise_value.warnings == ("missing_market_cap",)
    for multiple in (
        bundle.market_cap_to_ttm_pat,
        bundle.market_cap_to_total_equity,
        bundle.market_cap_to_ttm_revenue,
        bundle.simplified_ev_to_ttm_ebitda,
        bundle.simplified_ev_to_ttm_revenue,
    ):
        assert multiple.value is None
        assert "missing_market_cap" in multiple.warnings


def test_security_company_cutoff_scope_provider_and_market_date_are_explicit(
    session, tmp_path
) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    other_company = session.scalar(
        select(Company).where(Company.legal_name == "Meridian Components Limited")
    )
    assert other_company is not None
    with pytest.raises(ValueError, match="does not belong"):
        primitives.features_as_of(
            **{**arguments, "company_id": other_company.id},
            as_of=datetime(2027, 4, 30, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="unknown security"):
        primitives.features_as_of(
            **{**arguments, "security_id": uuid4()}, as_of=datetime(2027, 4, 30, tzinfo=UTC)
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _bundle(primitives, arguments, datetime(2027, 4, 30))

    equivalent = _bundle(
        primitives,
        arguments,
        datetime(2027, 4, 30, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    )
    assert equivalent.as_of == datetime(2027, 4, 30, 12, tzinfo=UTC)
    standalone = _bundle(
        primitives, {**arguments, "filing_scope": "consolidated"}, equivalent.as_of
    )
    assert standalone.ttm_pat is None
    no_market = _bundle(
        primitives,
        {**arguments, "market_provider_dataset_id": arguments["financial_provider_dataset_id"]},
        equivalent.as_of,
    )
    assert no_market.market_bar is None


def test_exact_endpoint_snapshot_is_unambiguous_and_partial_independent(session, tmp_path) -> None:
    service, primitives, arguments = _setup(session, tmp_path)
    cutoff = datetime(2027, 4, 30, tzinfo=UTC)
    exact = primitives._snapshot_reader.snapshot_for_fiscal_endpoint_as_of(
        provider_dataset_id=arguments["financial_provider_dataset_id"],
        company_id=arguments["company_id"],
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        metric_codes=("total_equity",),
        as_of=cutoff,
    )
    assert exact is not None and exact.fiscal_period.period_end.isoformat() == "2027-03-31"
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "valuation_financials_ambiguous_synthetic.csv", FINANCIAL, RETRIEVED_AT
        )
    )
    ambiguous = primitives._snapshot_reader.snapshot_for_fiscal_endpoint_as_of(
        provider_dataset_id=arguments["financial_provider_dataset_id"],
        company_id=arguments["company_id"],
        filing_scope="standalone",
        fiscal_year=2026,
        fiscal_quarter=4,
        metric_codes=("total_equity",),
        as_of=cutoff,
    )
    assert ambiguous is None


def test_partial_and_invalid_denominator_semantics_are_independent(session, tmp_path) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    full = _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC))

    class FakeTTM:
        def __init__(self, values):
            self.values = values

        def ttm_as_of(self, *, metric_code, **kwargs):
            del kwargs
            return self.values.get(metric_code)

    primitives._ttm_normalizer = cast(
        Any,
        FakeTTM(
            {
                "revenue": full.ttm_revenue,
                "pat": None,
                "ebitda_reported": full.ttm_ebitda,
            }
        ),
    )
    partial = _bundle(primitives, arguments, full.as_of)
    assert partial.market_cap_to_ttm_pat.warnings == ("missing_ttm_pat",)
    assert partial.market_cap_to_ttm_revenue.value == Decimal("2")
    assert partial.market_cap_to_total_equity.value == Decimal("2.5")
    assert partial.simplified_ev_to_ttm_ebitda.value == Decimal("5")

    assert full.ttm_revenue is not None
    assert full.ttm_pat is not None
    assert full.ttm_ebitda is not None
    primitives._ttm_normalizer = cast(
        Any,
        FakeTTM(
            {
                "revenue": replace(full.ttm_revenue, value=Decimal("0")),
                "pat": replace(full.ttm_pat, value=Decimal("-1")),
                "ebitda_reported": replace(full.ttm_ebitda, value=Decimal("0")),
            }
        ),
    )
    invalid = _bundle(primitives, arguments, full.as_of)
    assert invalid.market_cap_to_ttm_pat.warnings == ("non_positive_ttm_pat",)
    assert invalid.market_cap_to_ttm_revenue.warnings == ("non_positive_ttm_revenue",)
    assert invalid.simplified_ev_to_ttm_ebitda.warnings == ("non_positive_ttm_ebitda",)
    assert invalid.simplified_ev_to_ttm_revenue.warnings == ("non_positive_ttm_revenue",)


def test_missing_equity_net_debt_and_ebitda_do_not_create_bundle_failure(session, tmp_path) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    full = _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC))

    class FakeSnapshots:
        def __init__(self, *, equity=True, net_debt=True):
            self.equity = equity
            self.net_debt = net_debt

        def snapshot_for_fiscal_endpoint_as_of(self, *, metric_codes, **kwargs):
            del kwargs
            if tuple(metric_codes) == ("total_equity",):
                return full.equity_snapshot if self.equity else None
            return full.net_debt_snapshot if self.net_debt else None

    primitives._snapshot_reader = cast(Any, FakeSnapshots(net_debt=False))
    no_net_debt = _bundle(primitives, arguments, full.as_of)
    assert no_net_debt.market_cap_to_total_equity.value == Decimal("2.5")
    assert no_net_debt.simplified_enterprise_value.warnings == ("missing_net_debt_snapshot",)
    assert no_net_debt.simplified_ev_to_ttm_ebitda.value is None

    primitives._snapshot_reader = cast(Any, FakeSnapshots(equity=False))
    no_equity = _bundle(primitives, arguments, full.as_of)
    assert no_equity.market_cap_to_total_equity.warnings == ("missing_total_equity",)
    assert no_equity.simplified_enterprise_value.value == Decimal("1250")
    assert no_equity.simplified_ev_to_ttm_revenue.value == Decimal("2.5")

    class FakeTTM:
        def ttm_as_of(self, *, metric_code, **kwargs):
            del kwargs
            return {
                "revenue": full.ttm_revenue,
                "pat": full.ttm_pat,
                "ebitda_reported": None,
            }[metric_code]

    primitives._snapshot_reader = cast(Any, FakeSnapshots())
    primitives._ttm_normalizer = cast(Any, FakeTTM())
    no_ebitda = _bundle(primitives, arguments, full.as_of)
    assert no_ebitda.simplified_ev_to_ttm_ebitda.warnings == ("missing_ttm_ebitda",)
    assert no_ebitda.market_cap_to_ttm_pat.value == Decimal("10")
    assert no_ebitda.simplified_ev_to_ttm_revenue.value == Decimal("2.5")


def test_non_positive_market_cap_equity_and_evidence_contracts_fail_safely(
    session, tmp_path
) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    full = _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC))
    assert full.market_bar is not None
    assert full.equity_snapshot is not None
    assert full.ttm_pat is not None
    market_bar = full.market_bar
    ttm_pat = full.ttm_pat

    class FakeMarket:
        def latest_market_bar_as_of(self, **kwargs):
            del kwargs
            return replace(market_bar, market_cap=Decimal("0"))

    primitives._market_reader = cast(Any, FakeMarket())
    zero_market = _bundle(primitives, arguments, full.as_of)
    assert zero_market.market_cap_to_ttm_pat.warnings == ("non_positive_market_cap",)
    assert zero_market.simplified_enterprise_value.warnings == ("non_positive_market_cap",)

    equity_component = full.equity_snapshot.components[0]
    bad_equity = replace(
        full.equity_snapshot,
        components=(replace(equity_component, value=Decimal("-1")),),
    )

    class FakeSnapshots:
        def snapshot_for_fiscal_endpoint_as_of(self, *, metric_codes, **kwargs):
            del kwargs
            return (
                bad_equity if tuple(metric_codes) == ("total_equity",) else full.net_debt_snapshot
            )

    primitives._market_reader = PointInTimeMarketReader(session)
    primitives._snapshot_reader = cast(Any, FakeSnapshots())
    negative_equity = _bundle(primitives, arguments, full.as_of)
    assert negative_equity.market_cap_to_total_equity.warnings == ("non_positive_total_equity",)

    class WrongTTM:
        def ttm_as_of(self, *, metric_code, **kwargs):
            del kwargs
            return replace(ttm_pat, metric_code="revenue") if metric_code == "pat" else None

    primitives._ttm_normalizer = cast(Any, WrongTTM())
    with pytest.raises(ValueError, match="requested valuation context"):
        _bundle(primitives, arguments, full.as_of)

    class WrongUnitTTM:
        def ttm_as_of(self, *, metric_code, **kwargs):
            del kwargs
            return replace(ttm_pat, unit="USD") if metric_code == "pat" else None

    primitives._ttm_normalizer = cast(Any, WrongUnitTTM())
    with pytest.raises(ValueError, match="exact INR Decimal"):
        _bundle(primitives, arguments, full.as_of)


def test_net_cash_and_non_positive_simplified_ev_are_not_repaired(session, tmp_path) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    full = _bundle(primitives, arguments, datetime(2027, 4, 30, tzinfo=UTC))
    assert full.net_debt_snapshot is not None
    debt = next(c for c in full.net_debt_snapshot.components if c.metric_code == "total_debt")
    cash = next(
        c for c in full.net_debt_snapshot.components if c.metric_code == "cash_and_equivalents"
    )
    net_cash_snapshot = replace(
        full.net_debt_snapshot,
        components=(replace(cash, value=Decimal("300")), replace(debt, value=Decimal("100"))),
    )
    net_cash = primitives._simplified_ev(full.market_bar, net_cash_snapshot, full.as_of)
    assert net_cash.net_debt == Decimal("-200")
    assert net_cash.value == Decimal("800")
    negative_snapshot = replace(
        net_cash_snapshot,
        components=(replace(cash, value=Decimal("1200")), replace(debt, value=Decimal("0"))),
    )
    negative = primitives._simplified_ev(full.market_bar, negative_snapshot, full.as_of)
    ratio = primitives._ev_multiple(
        "simplified_ev_to_ttm_revenue",
        negative,
        full.ttm_revenue,
        "missing_ttm_revenue",
        "non_positive_ttm_revenue",
        "test",
        full.as_of,
    )
    assert negative.value == Decimal("-200")
    assert ratio.value is None
    assert ratio.warnings == ("non_positive_simplified_enterprise_value",)


def test_market_on_or_before_and_no_adjusted_price_dependency(session, tmp_path) -> None:
    _, primitives, arguments = _setup(session, tmp_path)
    missing = _bundle(
        primitives,
        {**arguments, "market_on_or_before": datetime(2027, 4, 9).date()},
        datetime(2027, 4, 30, tzinfo=UTC),
    )
    assert missing.market_bar is None
    assert missing.market_cap_to_ttm_pat.warnings == ("missing_market_cap",)
