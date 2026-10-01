"""Production NSE Integrated Filing discovery, XBRL, identity, and PIT tests."""

from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import func, select

from inflector_core.providers import (
    FinancialRecord,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.nse_cli import _financial_symbols
from inflector_data.nse_financials import (
    NSE_FINANCIAL_DATASET_CODE,
    NSE_INTEGRATED_FINANCIAL_MAPPING_VERSION,
    NSE_INTENTIONALLY_UNMAPPED_TARGET_METRICS,
    NSEFinancialFiling,
    NSEFinancialFormatError,
    NSEIntegratedFinancialDiscovery,
    NSEIntegratedFinancialsProvider,
    parse_financial_discovery,
)
from inflector_data.nse_http import AcquiredNSEArtifact, NSEHostNotAllowedError
from inflector_data.nse_providers import NSEArtifactSource
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.service import IngestionService
from inflector_data.ttm import TrailingTwelveMonthNormalizer
from inflector_database.models import (
    Company,
    DataQualityIssue,
    ExchangeListing,
    FinancialFact,
    FinancialFiling,
    FinancialMetricDefinition,
    ProviderDataset,
    Security,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
ISIN = "INE0FIC01019"
SYMBOL = "FICTEQ"
RETRIEVED_AT = datetime(2026, 10, 1, 7, 15, 30, tzinfo=UTC)
LATER_RETRIEVED_AT = datetime(2026, 10, 1, 8, 45, tzinfo=UTC)
XBRL_URI = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_FICTIONAL_20260930120000_WEB.xml"
)


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        "nse_official",
        "https",
        NSE_FINANCIAL_DATASET_CODE,
        "official-source-terms-reviewed-locally",
        redistributable=False,
    )


class _StaticSource:
    def __init__(
        self,
        payload: bytes,
        retrieved_at: datetime = RETRIEVED_AT,
        uri: str = XBRL_URI,
    ) -> None:
        self.source_uri = uri
        self._artifact = AcquiredNSEArtifact(uri, payload, retrieved_at)

    def acquire(self) -> AcquiredNSEArtifact:
        return self._artifact


class _DiscoveryClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def acquire(self, uri: str) -> AcquiredNSEArtifact:
        self.urls.append(uri)
        return AcquiredNSEArtifact(uri, self.payload, RETRIEVED_AT)


class _BatchProvider:
    def __init__(self, batch: ProviderBatch[FinancialRecord]) -> None:
        self._batch = batch

    def fetch_financials(self) -> ProviderBatch[FinancialRecord]:
        return self._batch


def _filing(
    *,
    scope: str = "standalone",
    sequence_id: str = "FICT-001",
    quarter_end: date = date(2026, 6, 30),
    uri: str = XBRL_URI,
    submission_type: str = "Original",
) -> NSEFinancialFiling:
    return NSEFinancialFiling(
        sequence_id=sequence_id,
        symbol=SYMBOL,
        company_name="Fictional Components Limited",
        quarter_end=quarter_end,
        scope=scope,
        submission_type=submission_type,
        xbrl_uri=uri,
    )


def _provider(
    fixture: str,
    *,
    scope: str = "standalone",
    retrieved_at: datetime = RETRIEVED_AT,
    filing: NSEFinancialFiling | None = None,
) -> NSEIntegratedFinancialsProvider:
    payload = (FIXTURES / fixture).read_bytes()
    source_uri = (filing or _filing()).xbrl_uri
    return NSEIntegratedFinancialsProvider(
        cast(NSEArtifactSource, _StaticSource(payload, retrieved_at, source_uri)),
        _metadata(),
        filing or _filing(scope=scope),
        expected_isin=ISIN,
    )


def _canonical_identity(session) -> Company:
    company = Company(
        legal_name="Fictional Components Limited",
        display_name="Fictional Components",
        sector="Industrials",
        industry="Components",
    )
    security = Security(company=company, isin=ISIN, security_type="equity", status="active")
    ExchangeListing(
        security=security,
        exchange="NSE",
        symbol=SYMBOL,
        valid_from=date(2020, 1, 1),
        valid_to=None,
        status="active",
    )
    session.add(company)
    session.commit()
    return company


def _records(provider: NSEIntegratedFinancialsProvider) -> tuple[FinancialRecord, ...]:
    return tuple(envelope.record for envelope in provider.fetch_financials().records)


def test_discovery_uses_observed_structured_fields_and_bounded_date_window() -> None:
    payload = json.dumps(
        {
            "data": [
                {
                    "seq_Id": 98765,
                    "symbol": SYMBOL,
                    "smName": "Fictional Components Limited",
                    "qe_Date": "30-Jun-2026",
                    "consolidated": "Standalone",
                    "type_Sub": "Original",
                    "xbrl": XBRL_URI,
                    "broadcast_Date": "29-Jul-2026 18:30:00",
                    "revised_Date": None,
                }
            ],
            "page": 1,
            "size": 20,
            "totalCount": 1,
        }
    ).encode()
    client = _DiscoveryClient(payload)
    discovery = NSEIntegratedFinancialDiscovery(cast(object, client))  # type: ignore[arg-type]
    filings = discovery.discover(
        symbol=SYMBOL,
        max_filings=4,
        from_date=date(2026, 4, 1),
        to_date=date(2026, 9, 30),
    )

    assert filings == (_filing(sequence_id="98765"),)
    assert "api/integrated-filing-results" in client.urls[0]
    assert "from_date=01-04-2026" in client.urls[0]
    assert "to_date=30-09-2026" in client.urls[0]
    assert "type=Integrated+Filing-+Financials" in client.urls[0]
    with pytest.raises(ValueError, match="exceeds 366 days"):
        discovery.discover(
            symbol=SYMBOL,
            max_filings=1,
            from_date=date(2025, 1, 1),
            to_date=date(2026, 2, 1),
        )


def test_discovery_fails_closed_for_wrong_symbol_or_nonofficial_xbrl() -> None:
    row = {
        "seq_Id": "1",
        "symbol": "OTHER",
        "smName": "Other Limited",
        "qe_Date": "30-Jun-2026",
        "consolidated": "Standalone",
        "type_Sub": "Original",
        "xbrl": XBRL_URI,
    }
    with pytest.raises(NSEFinancialFormatError, match="another symbol"):
        parse_financial_discovery(
            json.dumps({"data": [row]}).encode(), requested_symbol=SYMBOL, max_filings=1
        )
    row["symbol"] = SYMBOL
    row["xbrl"] = "https://example.com/filing.xml"
    with pytest.raises(NSEHostNotAllowedError, match="allowlisted"):
        parse_financial_discovery(
            json.dumps({"data": [row]}).encode(), requested_symbol=SYMBOL, max_filings=1
        )


def test_controlled_mapping_exact_decimal_units_and_intentional_absences() -> None:
    provider = _provider("nse_financials_standalone_quarter.xml")
    records = _records(provider)
    by_metric = {record.metric_code: record for record in records}

    assert NSE_INTEGRATED_FINANCIAL_MAPPING_VERSION == "nse_integrated_financial_mapping_v1"
    assert by_metric["operating_revenue"].reported_value == Decimal("100000000")
    assert by_metric["operating_revenue"].reported_unit == "INR"
    assert by_metric["operating_revenue"].reported_scale == "ones"
    assert by_metric["eps_basic"].reported_value == Decimal("1.25")
    assert by_metric["eps_basic"].reported_unit == "INR/share"
    assert all(isinstance(record.reported_value, Decimal) for record in records)
    assert "revenue" not in by_metric
    assert "ebitda_reported" not in by_metric
    assert "total_debt" not in by_metric
    assert "capex_reported" not in by_metric
    assert set(NSE_INTENTIONALLY_UNMAPPED_TARGET_METRICS).isdisjoint(by_metric)
    assert provider.recognized_source_facts == len(records)


def test_context_period_scope_and_balance_sheet_semantics() -> None:
    records = _records(
        _provider("nse_financials_consolidated_periods.xml", scope="consolidated")
    )
    revenue = [record for record in records if record.metric_code == "operating_revenue"]
    periods = {
        (record.period_kind, record.fiscal_year, record.fiscal_quarter, record.is_ytd)
        for record in revenue
    }
    assert periods == {
        ("quarter", 2025, 3, False),
        ("half_year", 2025, 2, True),
        ("nine_month", 2025, 3, True),
        ("annual", 2025, 4, False),
    }
    assert all(record.filing_scope == "consolidated" for record in records)
    assert all(record.period_start != date(2025, 2, 1) for record in records)
    assets = next(record for record in records if record.metric_code == "total_assets")
    assert (assets.period_kind, assets.period_end, assets.reported_value) == (
        "nine_month",
        date(2025, 12, 31),
        Decimal("2000"),
    )
    cash_flow = next(
        record for record in records if record.metric_code == "cash_flow_from_operations"
    )
    assert (cash_flow.period_kind, cash_flow.is_ytd) == ("nine_month", True)


def test_malformed_entities_identity_scope_and_unsupported_taxonomy_fail_closed() -> None:
    with pytest.raises(NSEFinancialFormatError, match="malformed XML"):
        _provider("nse_financials_malformed.xml").fetch_financials()
    malicious = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "https://example.com">]><x>&e;</x>'
    with pytest.raises(NSEFinancialFormatError, match="entities are forbidden"):
        NSEIntegratedFinancialsProvider(
            cast(NSEArtifactSource, _StaticSource(malicious)),
            _metadata(),
            _filing(),
            expected_isin=ISIN,
        ).fetch_financials()

    payload = (FIXTURES / "nse_financials_standalone_quarter.xml").read_bytes()
    with pytest.raises(NSEFinancialFormatError, match="contradicts"):
        NSEIntegratedFinancialsProvider(
            cast(NSEArtifactSource, _StaticSource(payload)),
            _metadata(),
            _filing(),
            expected_isin="INE0BAD01019",
        ).fetch_financials()

    wrong_scope = _filing(scope="consolidated")
    batch = NSEIntegratedFinancialsProvider(
        cast(NSEArtifactSource, _StaticSource(payload)),
        _metadata(),
        wrong_scope,
        expected_isin=ISIN,
    ).fetch_financials()
    assert batch.records and all(
        "financial_filing_scope_mismatch" in item.record.parse_errors for item in batch.records
    )

    banking_uri = XBRL_URI.replace("INDAS", "BANKING")
    banking_filing = _filing(uri=banking_uri)
    banking = NSEIntegratedFinancialsProvider(
        cast(
            NSEArtifactSource,
            _StaticSource(
                (FIXTURES / "nse_financials_unsupported_banking.xml").read_bytes(),
                uri=banking_uri,
            ),
        ),
        _metadata(),
        banking_filing,
        expected_isin=ISIN,
    )
    assert banking.fetch_financials().records == ()
    assert banking.unsupported_reason == "unsupported_financial_taxonomy"


def test_duplicate_equal_is_deterministic_and_conflict_is_quarantinable() -> None:
    equal = _provider("nse_financials_duplicate_equal.xml").fetch_financials()
    assert len(equal.records) == 1
    assert equal.records[0].record.reported_value == Decimal("100")
    assert equal.records[0].raw_payload_reference.endswith("fact-000007")

    conflict = _provider("nse_financials_duplicate_conflict.xml").fetch_financials()
    assert len(conflict.records) == 1
    assert conflict.records[0].record.reported_value is None
    assert conflict.records[0].record.parse_errors == (
        "conflicting_duplicate_financial_fact",
    )


def test_archive_identity_observed_availability_idempotency_and_revision(
    session, tmp_path: Path
) -> None:
    company = _canonical_identity(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    payload = (FIXTURES / "nse_financials_standalone_quarter.xml").read_bytes()
    first_provider = _provider("nse_financials_standalone_quarter.xml")
    first = service.ingest_financials(first_provider)
    duplicate = service.ingest_financials(_provider("nse_financials_standalone_quarter.xml"))

    source = session.scalar(
        select(SourceRecord).where(
            SourceRecord.external_record_id.like("nse-integrated-financials:%")
        )
    )
    facts = tuple(session.scalars(select(FinancialFact)))
    filings = tuple(session.scalars(select(FinancialFiling)))
    assert first.records_accepted == len(facts) > 0
    assert duplicate.records_duplicated == first.records_received
    assert source is not None
    assert (tmp_path / "raw" / source.raw_object_key).read_bytes() == payload
    assert source.raw_payload_reference.startswith("xbrl:{")
    assert all(fact.available_at.replace(tzinfo=UTC) == RETRIEVED_AT for fact in facts)
    assert all(fact.available_at.date() == date(2026, 10, 1) for fact in facts)
    assert all(filing.published_at is None and filing.revision_at is None for filing in filings)
    assert {filing.company_id for filing in filings} == {company.id}

    corrected_payload = payload.replace(b">100000000<", b">100100000<")
    corrected_provider = NSEIntegratedFinancialsProvider(
        cast(NSEArtifactSource, _StaticSource(corrected_payload, LATER_RETRIEVED_AT)),
        _metadata(),
        _filing(submission_type="Revision"),
        expected_isin=ISIN,
    )
    corrected = service.ingest_financials(corrected_provider)
    revenue_metric_id = session.scalar(
        select(FinancialMetricDefinition.id).where(
            FinancialMetricDefinition.code == "operating_revenue"
        )
    )
    revenues = tuple(
        session.scalars(
            select(FinancialFact)
            .where(FinancialFact.metric_definition_id == revenue_metric_id)
            .order_by(FinancialFact.available_at)
        )
    )
    assert corrected.records_accepted == 1
    assert [fact.reported_value for fact in revenues] == [
        Decimal("100000000"),
        Decimal("100100000"),
    ]
    assert revenues[1].available_at.replace(tzinfo=UTC) == LATER_RETRIEVED_AT


def test_security_identity_resolves_company_and_contradiction_quarantines(
    session, tmp_path: Path
) -> None:
    _canonical_identity(session)
    other = Company(
        legal_name="Different Fictional Limited",
        display_name="Different Fictional",
        sector="",
        industry="",
    )
    session.add(other)
    session.commit()
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    batch = _provider("nse_financials_duplicate_equal.xml").fetch_financials()
    coherent = service.ingest_financials(_BatchProvider(batch))
    assert coherent.records_accepted == 1

    original = batch.records[0]
    conflicting_record = replace(
        original.record,
        company_legal_name=other.legal_name,
        filing_external_id="FICT-MISMATCH",
    )
    conflicting_envelope = replace(
        original,
        external_record_id="FICT-MISMATCH:operating_revenue:OneD",
        content_sha256="f" * 64,
        record=conflicting_record,
    )
    conflict_batch = ProviderBatch(
        batch.provider,
        batch.source_uri,
        batch.raw_payload,
        batch.retrieved_at,
        (conflicting_envelope,),
    )
    result = service.ingest_financials(_BatchProvider(conflict_batch))
    rules = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert result.records_quarantined == 1
    assert "financial_company_security_mismatch" in rules

    unknown_record = replace(
        original.record,
        security_isin="INE0ZZZ01019",
        filing_external_id="FICT-UNKNOWN",
    )
    unknown = replace(
        original,
        external_record_id="FICT-UNKNOWN:operating_revenue:OneD",
        content_sha256="e" * 64,
        record=unknown_record,
    )
    result = service.ingest_financials(
        _BatchProvider(ProviderBatch(
            batch.provider,
            batch.source_uri,
            batch.raw_payload,
            batch.retrieved_at,
            (unknown,),
        ))
    )
    assert result.records_quarantined == 1
    assert "unknown_security" in set(session.scalars(select(DataQualityIssue.rule_code)))


def test_existing_pit_and_ttm_consume_production_financial_facts_without_special_case(
    session, tmp_path: Path
) -> None:
    company = _canonical_identity(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_financials(_provider("nse_financials_four_quarters.xml"))
    assert result.records_accepted == 4
    dataset_id = session.scalar(
        select(ProviderDataset.id).where(
            ProviderDataset.code == NSE_FINANCIAL_DATASET_CODE
        )
    )
    assert dataset_id is not None
    normalizer = TrailingTwelveMonthNormalizer(
        FiscalQuarterNormalizer(PointInTimeFinancialReader(session))
    )
    ttm = normalizer.ttm_as_of(
        provider_dataset_id=dataset_id,
        company_id=company.id,
        filing_scope="standalone",
        metric_code="operating_revenue",
        fiscal_year=2025,
        fiscal_quarter=4,
        as_of=RETRIEVED_AT,
    )
    assert ttm is not None
    assert ttm.value == Decimal("100")
    assert ttm.available_at == RETRIEVED_AT
    assert [part.quarter.value for part in ttm.lineage] == [
        Decimal("10"),
        Decimal("20"),
        Decimal("30"),
        Decimal("40"),
    ]


def test_financial_provider_module_remains_database_free() -> None:
    module = Path(__file__).parents[1] / "packages/data/inflector_data/nse_financials.py"
    source = module.read_text()
    forbidden = (
        "sqlalchemy",
        "inflector_database",
        "ScoreSnapshot",
        "ComponentScorer",
        "ScoreSnapshotOrchestrator",
    )
    assert not any(token in source for token in forbidden)
    assert "float(" not in source


def test_symbols_file_is_ordered_deduplicated_and_bounded(tmp_path: Path) -> None:
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text(" tcs\nITC\nTCS\n\n", encoding="utf-8")
    arguments = Namespace(
        symbol=None,
        symbols_file=symbols_file,
        max_symbols=3,
        max_filings=20,
        request_delay_seconds=1.0,
    )
    assert _financial_symbols(arguments) == ("TCS", "ITC")
    arguments.max_symbols = 1
    with pytest.raises(ValueError, match="exceeds max-symbols"):
        _financial_symbols(arguments)


def test_conflicting_duplicate_reaches_existing_quarantine_path(session, tmp_path: Path) -> None:
    _canonical_identity(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_financials(_provider("nse_financials_duplicate_conflict.xml"))
    assert result.records_quarantined == 1
    assert session.scalar(select(func.count()).select_from(FinancialFact)) == 0
    assert "conflicting_duplicate_financial_fact" in set(
        session.scalars(select(DataQualityIssue.rule_code))
    )
