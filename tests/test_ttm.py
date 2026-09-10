"""Phase 3C point-in-time trailing-twelve-month construction tests."""

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
from inflector_data.ttm import TTM_ALGORITHM_VERSION, TrailingTwelveMonthNormalizer
from inflector_database.models import Company, DataProvider, ProviderDataset

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2030, 7, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
FINANCIALS = ProviderMetadata("synthetic_csv", "csv", "financials", "synthetic-development-only")
FINANCIALS_B = ProviderMetadata(
    "synthetic_csv_b", "csv", "financials", "synthetic-development-only"
)


def _at(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _ttm(session, tmp_path: Path, *fixtures: str) -> TrailingTwelveMonthNormalizer:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    for fixture in fixtures:
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / fixture, FINANCIALS, RETRIEVED_AT)
        )
    return TrailingTwelveMonthNormalizer(
        FiscalQuarterNormalizer(PointInTimeFinancialReader(session))
    )


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


def _value(
    normalizer: TrailingTwelveMonthNormalizer,
    session,
    *,
    fiscal_year: int,
    fiscal_quarter: int,
    as_of: datetime,
    metric_code: str = "revenue",
    filing_scope: str = "standalone",
    provider_code: str = "synthetic_csv",
):
    return normalizer.ttm_as_of(
        provider_dataset_id=_dataset_id(session, provider_code),
        company_id=_company_id(session),
        filing_scope=filing_scope,
        metric_code=metric_code,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        as_of=as_of,
    )


def test_ttm_sums_four_pit_clean_quarters_with_nested_lineage_and_boundary(
    session, tmp_path: Path
) -> None:
    normalizer = _ttm(session, tmp_path, "financials_ttm_synthetic.csv")
    before = _value(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=2,
        as_of=_at(2026, 11, 1, 11, 59, 59),
    )
    result = _value(normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 1))

    assert before is None
    assert result is not None
    assert result.value == Decimal("4000000000") and result.unit == "INR"
    assert isinstance(result.value, Decimal)
    assert result.construction_kind == "four_quarters"
    assert result.operation == "sum_four_quarters"
    assert result.algorithm_version == TTM_ALGORITHM_VERSION
    assert result.available_at == _at(2026, 11, 1)
    assert (result.period_start.isoformat(), result.period_end.isoformat()) == (
        "2025-10-01",
        "2026-09-30",
    )
    assert (result.ending_fiscal_year, result.ending_fiscal_quarter) == (2026, 2)
    assert [component.role for component in result.lineage] == [
        "quarter_t_minus_3",
        "quarter_t_minus_2",
        "quarter_t_minus_1",
        "quarter_t",
    ]
    assert result.lineage[-1].quarter.derivation_kind == "derived_ytd_difference"
    assert result.lineage[-1].quarter.operation == "h1_minus_q1"
    assert [
        part.fact.source_record.external_record_id for part in result.lineage[-1].quarter.lineage
    ] == ["TTM-REV-H1-2026", "TTM-REV-Q1-2026"]
    assert all(
        part.fact.source_record.raw_object_key
        and part.fact.source_record.raw_payload_reference is not None
        for part in result.lineage[-1].quarter.lineage
    )


def test_ttm_restated_components_change_only_after_pit_availability(
    session, tmp_path: Path
) -> None:
    normalizer = _ttm(session, tmp_path, "financials_ttm_synthetic.csv")
    original = _value(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 15)
    )
    restated = _value(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 12, 1)
    )

    assert original is not None and original.value == Decimal("4000000000")
    assert restated is not None and restated.value == Decimal("4050000000")
    assert original.lineage[-1].quarter.value == Decimal("1300000000")
    assert restated.lineage[-1].quarter.value == Decimal("1350000000")


def test_ttm_rolls_across_fiscal_years_and_returns_an_ordered_series(
    session, tmp_path: Path
) -> None:
    normalizer = _ttm(session, tmp_path, "financials_ttm_synthetic.csv")
    arguments = {
        "provider_dataset_id": _dataset_id(session),
        "company_id": _company_id(session),
        "filing_scope": "standalone",
        "metric_code": "revenue",
    }
    first_three = normalizer.ttm_series_as_of(as_of=_at(2026, 8, 2), **arguments)
    rolling = normalizer.ttm_series_as_of(as_of=_at(2026, 12, 15), **arguments)
    before_q4 = _value(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(2027, 5, 1, 11, 59, 59),
    )
    q4 = _value(normalizer, session, fiscal_year=2026, fiscal_quarter=4, as_of=_at(2027, 5, 1))

    assert first_three == []
    assert [
        (value.ending_fiscal_year, value.ending_fiscal_quarter, value.value) for value in rolling
    ] == [
        (2026, 2, Decimal("4050000000")),
        (2026, 3, Decimal("4800000000")),
    ]
    assert before_q4 is None
    assert q4 is not None
    assert q4.value == Decimal("5800000000")
    assert (q4.period_start.isoformat(), q4.period_end.isoformat()) == (
        "2026-04-01",
        "2027-03-31",
    )
    assert q4.available_at == _at(2027, 5, 1)


def test_ttm_uses_direct_quarter_only_once_that_quarter_is_pit_visible(
    session, tmp_path: Path
) -> None:
    normalizer = _ttm(session, tmp_path, "financials_ttm_direct_synthetic.csv")
    derived = _value(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 10)
    )
    reported = _value(
        normalizer, session, fiscal_year=2026, fiscal_quarter=2, as_of=_at(2026, 11, 20)
    )

    assert derived is not None and derived.value == Decimal("4000000000")
    assert derived.lineage[-1].quarter.operation == "h1_minus_q1"
    assert reported is not None and reported.value == Decimal("3950000000")
    assert reported.lineage[-1].quarter.operation == "reported"


def test_ttm_refuses_non_additive_and_noncontinuous_quarter_windows(
    session, tmp_path: Path
) -> None:
    normalizer = _ttm(session, tmp_path, "financials_ttm_synthetic.csv")
    cutoff = _at(2026, 12, 15)

    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=2,
            metric_code="eps_basic",
            as_of=cutoff,
        )
        is None
    )
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=2,
            metric_code="total_assets",
            as_of=cutoff,
        )
        is None
    )
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=3,
            metric_code="ebit",
            as_of=cutoff,
        )
        is None
    )
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=2,
            metric_code="operating_profit",
            as_of=cutoff,
        )
        is None
    )
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=2,
            metric_code="finance_cost",
            as_of=cutoff,
        )
        is None
    )
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2027,
            fiscal_quarter=2,
            metric_code="other_income",
            as_of=cutoff,
        )
        is None
    )


def test_ttm_keeps_provider_scope_isolation_and_negative_arithmetic(
    session, tmp_path: Path
) -> None:
    normalizer = _ttm(session, tmp_path, "financials_ttm_synthetic.csv")
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_quarterization_provider_b_synthetic.csv",
            FINANCIALS_B,
            RETRIEVED_AT,
        )
    )
    cutoff = _at(2026, 11, 2)
    negative_pat = _value(
        normalizer,
        session,
        fiscal_year=2026,
        fiscal_quarter=2,
        metric_code="pat",
        as_of=cutoff,
    )

    assert negative_pat is not None and negative_pat.value == Decimal("-700000000")
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=2,
            as_of=cutoff,
            provider_code="synthetic_csv_b",
        )
        is None
    )
    assert (
        _value(
            normalizer,
            session,
            fiscal_year=2026,
            fiscal_quarter=2,
            as_of=cutoff,
            filing_scope="consolidated",
        )
        is None
    )
