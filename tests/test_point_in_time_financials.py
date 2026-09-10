"""Phase 3A point-in-time financial-read tests."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.pit import FiscalPeriodFilter, PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import (
    Company,
    DataProvider,
    FinancialFact,
    FiscalPeriod,
    ProviderDataset,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 7, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
FINANCIALS_A = ProviderMetadata("synthetic_csv", "csv", "financials", "synthetic-development-only")
FINANCIALS_B = ProviderMetadata(
    "synthetic_csv_b", "csv", "financials", "synthetic-development-only"
)


def _at(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _reader_with_financials(session, tmp_path: Path) -> PointInTimeFinancialReader:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    service.ingest_financials(
        CSVFinancialsProvider(FIXTURES / "financials_synthetic.csv", FINANCIALS_A, RETRIEVED_AT)
    )
    return PointInTimeFinancialReader(session)


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


def _period_id(session, *, fiscal_year: int, period_kind: str, is_ytd: bool = False) -> UUID:
    period_id = session.scalar(
        select(FiscalPeriod.id).where(
            FiscalPeriod.company_id == _company_id(session),
            FiscalPeriod.fiscal_year == fiscal_year,
            FiscalPeriod.period_kind == period_kind,
            FiscalPeriod.is_ytd == is_ytd,
        )
    )
    assert period_id is not None
    return period_id


def test_facts_respect_inclusive_availability_and_later_comparative_release(
    session, tmp_path: Path
) -> None:
    reader = _reader_with_financials(session, tmp_path)
    company_id = _company_id(session)
    dataset_id = _dataset_id(session, "synthetic_csv")
    current_quarter_id = _period_id(session, fiscal_year=2025, period_kind="quarter")
    comparative_quarter_id = _period_id(session, fiscal_year=2024, period_kind="quarter")

    before = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=current_quarter_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 11, 1, 11, 59, 59),
    )
    at_boundary = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=current_quarter_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 11, 1),
    )
    comparative_before = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=comparative_quarter_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 10, 31),
    )
    comparative_at = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=comparative_quarter_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 11, 1),
    )

    assert before is None and comparative_before is None
    assert at_boundary is not None and at_boundary.reported_value == Decimal("125")
    assert comparative_at is not None and comparative_at.reported_value == Decimal("100")
    assert comparative_at.fiscal_period.period_end.isoformat() == "2024-09-30"
    assert comparative_at.available_at == _at(2025, 11, 1)


def test_pit_normalizes_non_utc_cutoffs_and_preserves_raw_source_lineage(
    session, tmp_path: Path
) -> None:
    reader = _reader_with_financials(session, tmp_path)
    company_id = _company_id(session)
    dataset_id = _dataset_id(session, "synthetic_csv")
    period_id = _period_id(session, fiscal_year=2025, period_kind="quarter")
    utc = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=period_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 11, 1),
    )
    ist = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=period_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=datetime(2025, 11, 1, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    )

    assert utc is not None and ist is not None and utc.id == ist.id
    assert ist.source_record.external_record_id == "FIN-001"
    assert ist.source_record.raw_payload_reference == "row-1"
    assert ist.source_record.raw_object_key


def test_restatement_is_selected_only_after_its_availability(session, tmp_path: Path) -> None:
    reader = _reader_with_financials(session, tmp_path)
    company_id = _company_id(session)
    dataset_id = _dataset_id(session, "synthetic_csv")
    annual_period_id = _period_id(session, fiscal_year=2025, period_kind="annual")

    before = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=annual_period_id,
        filing_scope="standalone",
        metric_code="pat",
        as_of=_at(2026, 4, 30),
    )
    original = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=annual_period_id,
        filing_scope="standalone",
        metric_code="pat",
        as_of=_at(2026, 5, 15),
    )
    restated = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=annual_period_id,
        filing_scope="standalone",
        metric_code="pat",
        as_of=_at(2026, 6, 15),
    )

    assert before is None
    assert original is not None and original.reported_value == Decimal("100")
    assert restated is not None and restated.reported_value == Decimal("92")
    assert session.scalar(
        select(func.count()).select_from(FinancialFact).where(
            FinancialFact.fiscal_period_id == annual_period_id
        )
    ) == 2


def test_provider_and_scope_are_explicitly_isolated(session, tmp_path: Path) -> None:
    reader = _reader_with_financials(session, tmp_path)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financial_provider_b_synthetic.csv", FINANCIALS_B, RETRIEVED_AT
        )
    )
    company_id = _company_id(session)
    period_id = _period_id(session, fiscal_year=2025, period_kind="quarter")
    as_of = _at(2025, 11, 2)
    provider_a = reader.financial_fact_as_of(
        provider_dataset_id=_dataset_id(session, "synthetic_csv"),
        company_id=company_id,
        fiscal_period_id=period_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=as_of,
    )
    provider_b = reader.financial_fact_as_of(
        provider_dataset_id=_dataset_id(session, "synthetic_csv_b"),
        company_id=company_id,
        fiscal_period_id=period_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=as_of,
    )
    consolidated = reader.financial_fact_as_of(
        provider_dataset_id=_dataset_id(session, "synthetic_csv"),
        company_id=company_id,
        fiscal_period_id=period_id,
        filing_scope="consolidated",
        metric_code="revenue",
        as_of=as_of,
    )

    assert provider_a is not None and provider_a.reported_value == Decimal("125")
    assert provider_b is not None and provider_b.reported_value == Decimal("126")
    assert consolidated is not None and consolidated.reported_value == Decimal("135")


def test_series_preserves_period_semantics_and_excludes_noncanonical_sources(
    session, tmp_path: Path
) -> None:
    reader = _reader_with_financials(session, tmp_path)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financial_equivalent_other_id_synthetic.csv", FINANCIALS_A, RETRIEVED_AT
        )
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_invalid_synthetic.csv", FINANCIALS_A, RETRIEVED_AT
        )
    )
    company_id = _company_id(session)
    dataset_id = _dataset_id(session, "synthetic_csv")
    series = reader.financial_series_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 11, 2),
    )
    current_quarter = reader.financial_series_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=_at(2025, 11, 2),
        period_filter=FiscalPeriodFilter(
            period_kind="quarter",
            fiscal_year=2025,
            fiscal_quarter=2,
            is_ytd=False,
        ),
    )

    assert [fact.reported_value for fact in series] == [
        Decimal("100"),
        Decimal("240"),
        Decimal("125"),
    ]
    assert [(fact.fiscal_period.period_kind, fact.fiscal_period.is_ytd) for fact in series] == [
        ("quarter", False),
        ("half_year", True),
        ("quarter", False),
    ]
    assert len(current_quarter) == 1 and current_quarter[0].reported_value == Decimal("125")
    assert all(fact.source_record.validation_status == "accepted" for fact in series)
    assert series[-1].normalized_value == Decimal("1250000000")


def test_pit_rejects_naive_as_of_and_retains_nonmonetary_normalization_none(
    session, tmp_path: Path
) -> None:
    reader = _reader_with_financials(session, tmp_path)
    company_id = _company_id(session)
    dataset_id = _dataset_id(session, "synthetic_csv")
    period_id = _period_id(session, fiscal_year=2025, period_kind="quarter")

    with pytest.raises(ValueError, match="timezone-aware"):
        reader.financial_fact_as_of(
            provider_dataset_id=dataset_id,
            company_id=company_id,
            fiscal_period_id=period_id,
            filing_scope="standalone",
            metric_code="revenue",
            as_of=datetime(2025, 11, 2),
        )

    eps = reader.financial_fact_as_of(
        provider_dataset_id=dataset_id,
        company_id=company_id,
        fiscal_period_id=period_id,
        filing_scope="standalone",
        metric_code="eps_basic",
        as_of=_at(2025, 11, 2),
    )
    assert eps is not None
    assert eps.reported_value == Decimal("12.5")
    assert eps.normalized_value is None and eps.normalized_unit is None
