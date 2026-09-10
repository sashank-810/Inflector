"""Phase 3D real ingestion/PIT propagation tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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


def _at(y, m, d):
    return datetime(y, m, d, 12, tzinfo=UTC)


def _feature(session, tmp_path, fixture):
    s = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    s.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, _at(2030, 1, 1))
    )
    s.ingest_financials(CSVFinancialsProvider(FIXTURES / fixture, META, _at(2030, 1, 1)))
    return FinancialInflectionFeatures(FiscalQuarterNormalizer(PointInTimeFinancialReader(session)))


def _ids(session):
    c = session.scalar(select(Company.id).where(Company.legal_name == "Aurora Fabrication Limited"))
    d = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(DataProvider.code == "synthetic_csv", ProviderDataset.code == "financials")
    )
    assert c and d
    return d, c


def _yoy(f, session, as_of):
    d, c = _ids(session)
    return f.quarter_growth_as_of(
        provider_dataset_id=d,
        company_id=c,
        filing_scope="standalone",
        metric_code="revenue",
        fiscal_year=2026,
        fiscal_quarter=2,
        comparison_kind="yoy",
        as_of=as_of,
    )


def test_feature_growth_pit_direct_quarter_replacement(session, tmp_path):
    f = _feature(session, tmp_path, "financials_ttm_direct_synthetic.csv")
    before = _yoy(f, session, _at(2026, 11, 10))
    at = _yoy(f, session, _at(2026, 11, 20))
    after = _yoy(f, session, _at(2026, 12, 1))
    again = _yoy(f, session, _at(2026, 11, 10))
    assert (
        before
        and before.value == Decimal("0.3")
        and at
        and at.value == Decimal("0.25")
        and after
        and after.value == Decimal("0.25")
        and again
        and again.value == Decimal("0.3")
    )


def test_feature_growth_pit_restatement(session, tmp_path):
    f = _feature(session, tmp_path, "financials_ttm_synthetic.csv")
    before = _yoy(f, session, _at(2026, 11, 15))
    at = _yoy(f, session, _at(2026, 12, 1))
    again = _yoy(f, session, _at(2026, 11, 15))
    assert (
        before
        and before.value == Decimal("0.3")
        and at
        and at.value == Decimal("0.35")
        and again
        and again.value == Decimal("0.3")
    )


def test_feature_growth_provider_datasets_are_isolated(session, tmp_path):
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_synthetic.csv",
            UNIVERSE,
            _at(2030, 1, 1),
        )
    )

    meta_b = ProviderMetadata(
        "synthetic_csv_b",
        "csv",
        "financials",
        "synthetic-development-only",
    )

    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_features_provider_a_isolation_synthetic.csv",
            META,
            _at(2030, 1, 1),
        )
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_features_provider_b_isolation_synthetic.csv",
            meta_b,
            _at(2030, 1, 1),
        )
    )

    company_id = session.scalar(
        select(Company.id).where(
            Company.legal_name == "Aurora Fabrication Limited"
        )
    )
    assert company_id is not None

    provider_a = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(
            DataProvider.code == "synthetic_csv",
            ProviderDataset.code == "financials",
        )
    )
    provider_b = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(
            DataProvider.code == "synthetic_csv_b",
            ProviderDataset.code == "financials",
        )
    )
    assert provider_a is not None
    assert provider_b is not None

    feature = FinancialInflectionFeatures(
        FiscalQuarterNormalizer(PointInTimeFinancialReader(session))
    )

    def growth(dataset_id):
        return feature.quarter_growth_as_of(
            provider_dataset_id=dataset_id,
            company_id=company_id,
            filing_scope="standalone",
            metric_code="revenue",
            fiscal_year=2026,
            fiscal_quarter=4,
            comparison_kind="yoy",
            as_of=_at(2027, 5, 2),
        )

    assert growth(provider_a) is None
    assert growth(provider_b) is None


def test_feature_growth_filing_scopes_are_isolated(session, tmp_path):
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_synthetic.csv",
            UNIVERSE,
            _at(2030, 1, 1),
        )
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_features_scope_isolation_synthetic.csv",
            META,
            _at(2030, 1, 1),
        )
    )

    dataset_id, company_id = _ids(session)
    feature = FinancialInflectionFeatures(
        FiscalQuarterNormalizer(PointInTimeFinancialReader(session))
    )

    def growth(scope):
        return feature.quarter_growth_as_of(
            provider_dataset_id=dataset_id,
            company_id=company_id,
            filing_scope=scope,
            metric_code="revenue",
            fiscal_year=2026,
            fiscal_quarter=4,
            comparison_kind="yoy",
            as_of=_at(2027, 5, 2),
        )

    assert growth("standalone") is None
    assert growth("consolidated") is None
