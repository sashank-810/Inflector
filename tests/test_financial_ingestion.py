"""Phase 2B financial reporting spine tests."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import (
    DataQualityIssue,
    FinancialFact,
    FinancialFiling,
    FiscalPeriod,
    IngestionRun,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2026, 7, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
FINANCIALS = ProviderMetadata("synthetic_csv", "csv", "financials", "synthetic-development-only")


def _service_with_universe(session, tmp_path: Path) -> IngestionService:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    return service


def test_financial_fixture_is_multi_period_idempotent_and_normalized(
    session, tmp_path: Path
) -> None:
    service = _service_with_universe(session, tmp_path)
    provider = CSVFinancialsProvider(
        FIXTURES / "financials_synthetic.csv", FINANCIALS, RETRIEVED_AT
    )
    result = service.ingest_financials(provider)
    repeat = service.ingest_financials(provider)

    facts = list(session.scalars(select(FinancialFact)))
    periods = list(session.scalars(select(FiscalPeriod)))
    assert result.records_accepted == 7 and repeat.records_duplicated == 7
    assert len(periods) == 4 and len(facts) == 7
    revenue = next(fact for fact in facts if fact.reported_value == Decimal("125"))
    eps = next(fact for fact in facts if fact.reported_unit == "INR/share")
    assert revenue.normalized_value == Decimal("1250000000")
    assert eps.normalized_value is None


def test_scope_and_restatement_remain_append_only(session, tmp_path: Path) -> None:
    service = _service_with_universe(session, tmp_path)
    service.ingest_financials(
        CSVFinancialsProvider(FIXTURES / "financials_synthetic.csv", FINANCIALS, RETRIEVED_AT)
    )

    pat_facts = list(
        session.scalars(select(FinancialFact).where(FinancialFact.reported_value.in_([100, 92])))
    )
    filings = list(session.scalars(select(FinancialFiling)))
    revenue_filings = [
        filing for filing in filings if filing.external_filing_id == "SYN-FILING-Q2-2025"
    ]
    assert {fact.reported_value for fact in pat_facts} == {Decimal("100"), Decimal("92")}
    assert {filing.filing_scope for filing in revenue_filings} == {"standalone", "consolidated"}


def test_invalid_financial_rows_are_quarantined(session, tmp_path: Path) -> None:
    service = _service_with_universe(session, tmp_path)
    result = service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_invalid_synthetic.csv", FINANCIALS, RETRIEVED_AT
        )
    )

    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert result.records_quarantined == 4
    assert {
        "invalid_scale",
        "unsupported_currency",
        "unknown_metric",
        "invalid_period_start",
    }.issubset(rules)


def test_financial_source_locator_survives(session, tmp_path: Path) -> None:
    service = _service_with_universe(session, tmp_path)
    service.ingest_financials(
        CSVFinancialsProvider(FIXTURES / "financials_synthetic.csv", FINANCIALS, RETRIEVED_AT)
    )
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "FIN-001")
    )
    assert source is not None and source.raw_payload_reference == "row-1"


def test_equivalent_financial_source_is_provenance_only(session, tmp_path: Path) -> None:
    service = _service_with_universe(session, tmp_path)
    service.ingest_financials(
        CSVFinancialsProvider(FIXTURES / "financials_synthetic.csv", FINANCIALS, RETRIEVED_AT)
    )
    result = service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financial_equivalent_other_id_synthetic.csv", FINANCIALS, RETRIEVED_AT
        )
    )
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id == "FIN-EQUIV")
    )
    assert result.records_duplicated == 1
    assert source is not None and source.validation_status == "duplicate_economic"


def test_ambiguous_financial_revision_is_quarantined(session, tmp_path: Path) -> None:
    service = _service_with_universe(session, tmp_path)
    service.ingest_financials(
        CSVFinancialsProvider(FIXTURES / "financials_synthetic.csv", FINANCIALS, RETRIEVED_AT)
    )
    result = service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financial_ambiguous_revision_synthetic.csv", FINANCIALS, RETRIEVED_AT
        )
    )
    issue = session.scalar(
        select(DataQualityIssue).where(DataQualityIssue.rule_code == "ambiguous_financial_revision")
    )
    assert result.records_quarantined == 1 and issue is not None


def test_failed_batch_counters_only_describe_durable_outcomes(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service_with_universe(session, tmp_path)
    original = service._repository.add_financial_fact
    calls = 0

    def fail_second(**kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second persistence failure")
        original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service._repository, "add_financial_fact", fail_second)
    with pytest.raises(RuntimeError, match="second persistence failure"):
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / "financials_synthetic.csv", FINANCIALS, RETRIEVED_AT)
        )

    run = session.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()))
    assert run is not None
    assert run.status == "failed" and run.records_accepted == run.records_quarantined == 0
    assert session.scalar(select(FinancialFact)) is None
