"""Phase 3B point-in-time fiscal-quarter normalization tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import Company, DataProvider, ProviderDataset

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2030, 7, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
FINANCIALS = ProviderMetadata("synthetic_csv", "csv", "financials", "synthetic-development-only")
FINANCIALS_B = ProviderMetadata(
    "synthetic_csv_b", "csv", "financials", "synthetic-development-only"
)


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _normalizer(session, tmp_path: Path, *financial_fixtures: str) -> FiscalQuarterNormalizer:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    for fixture in financial_fixtures:
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / fixture, FINANCIALS, RETRIEVED_AT)
        )
    return FiscalQuarterNormalizer(PointInTimeFinancialReader(session))


def _company_id(session) -> UUID:
    company_id = session.scalar(
        select(Company.id).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    assert company_id is not None
    return company_id


def _dataset_id(session, provider_code: str = "synthetic_csv") -> UUID:
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(DataProvider.code == provider_code, ProviderDataset.code == "financials")
    )
    assert dataset_id is not None
    return dataset_id


def _quarter(
    normalizer: FiscalQuarterNormalizer,
    session,
    *,
    fiscal_year: int,
    fiscal_quarter: int,
    metric_code: str = "revenue",
    scope: str = "standalone",
    as_of: datetime,
    provider_code: str = "synthetic_csv",
):
    return normalizer.quarter_as_of(
        provider_dataset_id=_dataset_id(session, provider_code),
        company_id=_company_id(session),
        filing_scope=scope,
        metric_code=metric_code,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        as_of=as_of,
    )


def test_ytd_differences_respect_availability_restatements_and_lineage(
    session, tmp_path: Path
) -> None:
    normalizer = _normalizer(session, tmp_path, "financials_quarterization_synthetic.csv")

    before_h1 = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 10, 31)
    )
    q2_at_h1 = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 1)
    )
    q3_before_restatement = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=3, as_of=_at(2026, 11, 20)
    )
    q2_after_restatement = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 12, 15)
    )
    q4 = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 5, 2)
    )
    negative_pat = _quarter(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=2,
        metric_code="pat",
        as_of=_at(2026, 11, 2),
    )

    assert before_h1 is None
    assert q2_at_h1 is not None
    assert q2_at_h1.value == Decimal("1300000000") and q2_at_h1.unit == "INR"
    assert q2_at_h1.operation == "h1_minus_q1"
    assert q2_at_h1.available_at == _at(2026, 11, 1)
    assert (q2_at_h1.period_start.isoformat(), q2_at_h1.period_end.isoformat()) == (
        "2026-07-01",
        "2026-09-30",
    )
    assert [component.role for component in q2_at_h1.lineage] == ["minuend", "subtrahend"]
    assert [component.fact.source_record.external_record_id for component in q2_at_h1.lineage] == [
        "QTR-REV-H1",
        "QTR-REV-Q1",
    ]
    assert q3_before_restatement is not None
    assert q3_before_restatement.value == Decimal("1600000000")
    assert q3_before_restatement.operation == "nine_month_minus_h1"
    assert q2_after_restatement is not None
    assert q2_after_restatement.value == Decimal("1350000000")
    assert q4 is not None and q4.value == Decimal("1900000000")
    assert q4.operation == "annual_minus_nine_month"
    assert negative_pat is not None and negative_pat.value == Decimal("-100000000")


def test_direct_quarter_overrides_conflicting_ytd_difference(session, tmp_path: Path) -> None:
    normalizer = _normalizer(session, tmp_path, "financials_quarterization_direct_synthetic.csv")
    result = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 20)
    )

    assert result is not None
    assert result.derivation_kind == "reported"
    assert result.operation == "reported"
    assert result.value == Decimal("1250000000")
    assert result.lineage[0].role == "reported"


def test_direct_quarter_precedence_changes_only_when_pit_visible(session, tmp_path: Path) -> None:
    normalizer = _normalizer(session, tmp_path, "financials_quarterization_direct_synthetic.csv")
    before_direct_quarter = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 10)
    )
    at_direct_quarter_availability = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 20)
    )
    after_direct_quarter_availability = _quarter(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 12, 1)
    )

    assert before_direct_quarter is not None
    assert before_direct_quarter.derivation_kind == "derived_ytd_difference"
    assert before_direct_quarter.operation == "h1_minus_q1"
    assert before_direct_quarter.value == Decimal("1300000000")

    for result in (at_direct_quarter_availability, after_direct_quarter_availability):
        assert result is not None
        assert result.derivation_kind == "reported"
        assert result.operation == "reported"
        assert result.value == Decimal("1250000000")


def test_non_additive_missing_incompatible_provider_and_scope_inputs_are_refused(
    session, tmp_path: Path
) -> None:
    normalizer = _normalizer(
        session,
        tmp_path,
        "financials_quarterization_synthetic.csv",
        "financials_quarterization_incompatible_synthetic.csv",
    )
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_quarterization_provider_b_synthetic.csv",
            FINANCIALS_B,
            RETRIEVED_AT,
        )
    )

    eps = _quarter(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=2,
        metric_code="eps_basic",
        as_of=_at(2026, 11, 2),
    )
    assets = _quarter(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=2,
        metric_code="total_assets",
        as_of=_at(2026, 11, 2),
    )
    mismatched_start = _quarter(
        normalizer, session, fiscal_year=2027, fiscal_quarter=2, as_of=_at(2027, 11, 2)
    )
    missing_nine_month = _quarter(
        normalizer, session, fiscal_year=2028, fiscal_quarter=4, as_of=_at(2029, 5, 2)
    )
    standalone_scope = _quarter(
        normalizer, session, fiscal_year=2029, fiscal_quarter=2, as_of=_at(2029, 11, 2)
    )
    consolidated_scope = _quarter(
        normalizer,
        session,
        fiscal_year=2029,
        fiscal_quarter=2,
        scope="consolidated",
        as_of=_at(2029, 11, 2),
    )
    provider_b = _quarter(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=2,
        as_of=_at(2026, 11, 2),
        provider_code="synthetic_csv_b",
    )

    assert eps is None and assets is None
    assert mismatched_start is None and missing_nine_month is None
    assert standalone_scope is None and consolidated_scope is None
    assert provider_b is None


def test_quarter_series_is_ordered_and_does_not_fill_missing_values(
    session, tmp_path: Path
) -> None:
    normalizer = _normalizer(session, tmp_path, "financials_quarterization_synthetic.csv")
    arguments = {
        "provider_dataset_id": _dataset_id(session),
        "company_id": _company_id(session),
        "filing_scope": "standalone",
        "metric_code": "revenue",
    }
    early_series = normalizer.quarter_series_as_of(as_of=_at(2026, 11, 1), **arguments)
    complete_series = normalizer.quarter_series_as_of(as_of=_at(2027, 5, 2), **arguments)

    assert [(value.fiscal_quarter, value.value) for value in early_series] == [
        (1, Decimal("1000000000")),
        (2, Decimal("1300000000")),
    ]
    assert [(value.fiscal_quarter, value.value) for value in complete_series] == [
        (1, Decimal("1000000000")),
        (2, Decimal("1350000000")),
        (3, Decimal("1550000000")),
        (4, Decimal("1900000000")),
    ]
