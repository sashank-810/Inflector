"""Phase 3D PIT financial-inflection primitive tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_features import FinancialInflectionFeatures
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import Company, DataProvider, ProviderDataset

FIXTURES = Path(__file__).parent / "fixtures"
META = ProviderMetadata("synthetic_csv", "csv", "financials", "synthetic-development-only")
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")


def _at(y: int, m: int, d: int, h: int = 12) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def _features(session, tmp_path: Path) -> FinancialInflectionFeatures:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, _at(2030, 1, 1))
    )
    service.ingest_financials(
        CSVFinancialsProvider(FIXTURES / "financials_features_synthetic.csv", META, _at(2030, 1, 1))
    )
    return FinancialInflectionFeatures(FiscalQuarterNormalizer(PointInTimeFinancialReader(session)))


def _ids(session) -> tuple[UUID, UUID]:
    company = session.scalar(
        select(Company.id).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    dataset = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(DataProvider.code == "synthetic_csv", ProviderDataset.code == "financials")
    )
    assert company and dataset
    return dataset, company


def _growth(features, session, year, quarter, kind="yoy", metric="revenue", as_of=None):
    dataset, company = _ids(session)
    return features.quarter_growth_as_of(
        provider_dataset_id=dataset,
        company_id=company,
        filing_scope="standalone",
        metric_code=metric,
        fiscal_year=year,
        fiscal_quarter=quarter,
        comparison_kind=kind,
        as_of=as_of or _at(2027, 5, 2),
    )


def test_growth_acceleration_and_series_are_pit_clean(session, tmp_path: Path) -> None:
    f = _features(session, tmp_path)
    yoy = _growth(f, session, 2026, 4)
    qoq = _growth(f, session, 2026, 1, "qoq")
    assert yoy and yoy.value == Decimal("0.4") and yoy.calculation_mode == "percentage_change"
    assert qoq and qoq.value == Decimal("0.1")
    acceleration = f.growth_acceleration_as_of(
        provider_dataset_id=_ids(session)[0],
        company_id=_ids(session)[1],
        filing_scope="standalone",
        metric_code="revenue",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 5, 2),
    )
    assert (
        acceleration
        and acceleration.prior_median == Decimal("0.2")
        and acceleration.acceleration == Decimal("0.2")
    )
    assert len(acceleration.prior_yoys) == 3 and acceleration.available_at == _at(2027, 5, 1)
    series = f.growth_series_as_of(
        provider_dataset_id=_ids(session)[0],
        company_id=_ids(session)[1],
        filing_scope="standalone",
        metric_code="revenue",
        comparison_kind="yoy",
        as_of=_at(2027, 5, 2),
    )
    assert [(x.ending_fiscal_year, x.ending_fiscal_quarter) for x in series] == [
        (2026, 1),
        (2026, 2),
        (2026, 3),
        (2026, 4),
    ]


def test_margins_and_expansion_preserve_decimal_lineage(session, tmp_path: Path) -> None:
    f = _features(session, tmp_path)
    dataset, company = _ids(session)
    operating = f.quarter_margin_as_of(
        provider_dataset_id=dataset,
        company_id=company,
        filing_scope="standalone",
        margin_code="operating_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(2026, 8, 1),
    )
    ebitda = f.quarter_margin_as_of(
        provider_dataset_id=dataset,
        company_id=company,
        filing_scope="standalone",
        margin_code="ebitda_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(2026, 8, 1),
    )
    expansion = f.margin_expansion_as_of(
        provider_dataset_id=dataset,
        company_id=company,
        filing_scope="standalone",
        margin_code="ebitda_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(2026, 8, 1),
    )
    assert operating and operating.value == Decimal("-0.1") and operating.numerator_quarter.lineage
    assert ebitda and ebitda.value == Decimal("0.2")
    assert (
        expansion
        and expansion.change == Decimal("0.05")
        and expansion.basis_points == Decimal("500")
    )
