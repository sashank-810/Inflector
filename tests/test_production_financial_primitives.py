"""Production G financial semantic qualification and primitive activation tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import func, select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_primitive_policy import load_financial_primitive_policy
from inflector_data.financials import METRICS
from inflector_data.nse_financials import (
    NSE_FINANCIAL_DATASET_CODE,
    NSEFinancialFiling,
    NSEIntegratedFinancialsProvider,
)
from inflector_data.nse_http import AcquiredNSEArtifact
from inflector_data.nse_providers import NSEArtifactSource
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.research_profile import load_research_profile
from inflector_data.service import IngestionService
from inflector_database.models import (
    Company,
    ExchangeListing,
    FinancialFact,
    FinancialMetricDefinition,
    ProviderDataset,
    Security,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config" / "financial" / "production_financial_primitives_v1.json"
FIXTURE = ROOT / "tests" / "fixtures" / "nse_financial_primitives.xml"
XBRL_URI = (
    "https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_FICTIONAL_PRIMITIVES_WEB.xml"
)
ISIN = "INE0PRM01019"
RETRIEVED = datetime(2026, 10, 2, 12, tzinfo=UTC)


class _StaticSource:
    source_uri = XBRL_URI

    def __init__(self, payload: bytes, retrieved_at: datetime = RETRIEVED) -> None:
        self._artifact = AcquiredNSEArtifact(XBRL_URI, payload, retrieved_at)

    def acquire(self) -> AcquiredNSEArtifact:
        return self._artifact


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        "nse_official",
        "https",
        NSE_FINANCIAL_DATASET_CODE,
        "official-source-terms-reviewed-locally",
        redistributable=False,
    )


def _provider(
    retrieved_at: datetime = RETRIEVED, payload: bytes | None = None
) -> NSEIntegratedFinancialsProvider:
    return NSEIntegratedFinancialsProvider(
        cast(NSEArtifactSource, _StaticSource(payload or FIXTURE.read_bytes(), retrieved_at)),
        _metadata(),
        NSEFinancialFiling(
            sequence_id="FICT-PRIMITIVE-001",
            symbol="FICTPRIM",
            company_name="Fictional Primitive Industries Limited",
            quarter_end=date(2026, 3, 31),
            scope="consolidated",
            submission_type="Original",
            xbrl_uri=XBRL_URI,
        ),
        expected_isin=ISIN,
        primitive_policy=load_financial_primitive_policy(POLICY_PATH),
    )


def _identity(session) -> Company:
    company = Company(
        legal_name="Fictional Primitive Industries Limited",
        display_name="Fictional Primitive Industries",
        sector="Industrials",
        industry="Fictional Components",
    )
    security = Security(company=company, isin=ISIN, security_type="equity", status="active")
    ExchangeListing(
        security=security,
        exchange="NSE",
        symbol="FICTPRIM",
        valid_from=date(2020, 1, 1),
        valid_to=None,
        status="active",
    )
    session.add(company)
    session.commit()
    return company


def test_financial_primitive_policy_is_explicit_deterministic_and_versioned(
    tmp_path: Path,
) -> None:
    policy = load_financial_primitive_policy(POLICY_PATH)
    assert policy.financial_primitive_policy_code == "nse_indas_financial_primitives_v1"
    assert policy.normalized_identity_source("revenue") == "operating_revenue"
    assert policy.status("ebitda_reported") == "NOT_APPROVED"
    assert policy.status("total_debt") == "NOT_APPROVED"
    assert policy.direct_source_mappings() == {
        "BorrowingsCurrent": "borrowings_current",
        "BorrowingsNoncurrent": "borrowings_non_current",
        "FinanceCosts": "finance_cost",
        "DepreciationDepletionAndAmortisationExpense": "depreciation_amortisation",
    }
    assert policy.checksum_sha256 == load_financial_primitive_policy(POLICY_PATH).checksum_sha256

    changed = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    changed["qualifications"]["revenue"]["rationale"] += " Changed semantics."
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    assert load_financial_primitive_policy(changed_path).checksum_sha256 != policy.checksum_sha256

    changed["qualifications"]["revenue"]["status"] = "APPROVED_DIRECT"
    changed["qualifications"]["revenue"]["classification"] = "direct"
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="direct financial primitive qualification revenue"):
        load_financial_primitive_policy(invalid_path)

    v1 = load_research_profile(ROOT / "config/research/production_research_v1.json")
    v2 = load_research_profile(ROOT / "config/research/production_research_v2.json")
    v3 = load_research_profile(ROOT / "config/research/production_research_v3.json")
    assert v1.checksum_sha256 == "182d139eaf6a6603b3fa817cdc590676454d2e4051e49a495a32591747f51f80"
    assert v2.checksum_sha256 == "ed14c7c3aa73278793da16d2d6a1de23ea19274f98bd91b64908fedbee9677b8"
    assert v3.financial_primitive_policy_checksum_sha256 == policy.checksum_sha256
    assert v3.checksum_sha256 == (
        "a1510f311babe3edcab242b228f79fc6353e34a399bc8145aec6764909eb4676"
    )


def test_xbrl_policy_emits_only_qualified_direct_primitives() -> None:
    batch = _provider().fetch_financials()
    records = {envelope.record.metric_code: envelope for envelope in batch.records}

    assert records["operating_revenue"].record.reported_value == 125000000
    assert records["finance_cost"].record.reported_value == 3000000
    assert records["depreciation_amortisation"].record.reported_value == 5000000
    assert records["borrowings_current"].record.reported_value == 11000000
    assert records["borrowings_non_current"].record.reported_value == 0
    assert {"revenue", "ebitda_reported", "total_debt"}.isdisjoint(records)
    assert "BorrowingsCurrent" in (records["borrowings_current"].raw_payload_reference or "")
    assert "DepreciationDepletionAndAmortisationExpense" in (
        records["depreciation_amortisation"].raw_payload_reference or ""
    )

    missing_current = FIXTURE.read_bytes().replace(
        b'<in-capmkt:BorrowingsCurrent contextRef="OneI" unitRef="INR" decimals="0">'
        b"11000000</in-capmkt:BorrowingsCurrent>",
        b'<in-capmkt:BorrowingsCurrent contextRef="OneI" unitRef="INR" xsi:nil="true"/>',
    )
    missing_records = {
        envelope.record.metric_code
        for envelope in _provider(payload=missing_current).fetch_financials().records
    }
    assert "borrowings_current" not in missing_records
    assert "borrowings_non_current" in missing_records

    wrong_context = FIXTURE.read_bytes().replace(
        b'<in-capmkt:BorrowingsCurrent contextRef="OneI"',
        b'<in-capmkt:BorrowingsCurrent contextRef="ThreeD"',
    )
    wrong_context_records = {
        envelope.record.metric_code
        for envelope in _provider(payload=wrong_context).fetch_financials().records
    }
    assert "borrowings_current" not in wrong_context_records


def test_ingested_primitives_preserve_pit_source_lineage_and_missingness(
    session, tmp_path: Path
) -> None:
    company = _identity(session)
    store = LocalRawObjectStore(tmp_path / "raw")
    result = IngestionService(session, store).ingest_financials(_provider())
    assert result.records_quarantined == 0
    assert result.records_accepted == 6
    archived_source = session.scalar(
        select(SourceRecord).where(
            SourceRecord.external_record_id.like("nse-integrated-financials:%")
        )
    )
    assert archived_source is not None
    assert FIXTURE.read_bytes() == (tmp_path / "raw" / archived_source.raw_object_key).read_bytes()

    dataset_id = session.scalar(
        select(ProviderDataset.id).where(ProviderDataset.code == NSE_FINANCIAL_DATASET_CODE)
    )
    assert dataset_id is not None
    reader = PointInTimeFinancialReader(session)
    assert (
        reader.financial_series_as_of(
            provider_dataset_id=dataset_id,
            company_id=company.id,
            filing_scope="consolidated",
            metric_code="borrowings_current",
            as_of=RETRIEVED.replace(hour=11),
        )
        == []
    )
    current = reader.financial_series_as_of(
        provider_dataset_id=dataset_id,
        company_id=company.id,
        filing_scope="consolidated",
        metric_code="borrowings_current",
        as_of=RETRIEVED,
    )
    non_current = reader.financial_series_as_of(
        provider_dataset_id=dataset_id,
        company_id=company.id,
        filing_scope="consolidated",
        metric_code="borrowings_non_current",
        as_of=RETRIEVED,
    )
    assert current[0].normalized_value == 11000000
    assert non_current[0].normalized_value == 0
    assert current[0].source_record.raw_payload_reference is not None
    assert (
        reader.financial_series_as_of(
            provider_dataset_id=dataset_id,
            company_id=company.id,
            filing_scope="consolidated",
            metric_code="total_debt",
            as_of=RETRIEVED,
        )
        == []
    )
    assert (
        reader.financial_series_as_of(
            provider_dataset_id=dataset_id,
            company_id=company.id,
            filing_scope="consolidated",
            metric_code="ebitda_reported",
            as_of=RETRIEVED,
        )
        == []
    )

    policy = load_financial_primitive_policy(POLICY_PATH)
    assert (
        FiscalQuarterNormalizer(reader).quarter_as_of(
            provider_dataset_id=dataset_id,
            company_id=company.id,
            filing_scope="consolidated",
            metric_code="revenue",
            fiscal_year=2025,
            fiscal_quarter=4,
            as_of=RETRIEVED,
        )
        is None
    )
    revenue = FiscalQuarterNormalizer(reader, policy).quarter_as_of(
        provider_dataset_id=dataset_id,
        company_id=company.id,
        filing_scope="consolidated",
        metric_code="revenue",
        fiscal_year=2025,
        fiscal_quarter=4,
        as_of=RETRIEVED,
    )
    assert revenue is not None
    assert revenue.metric_code == "revenue"
    assert revenue.value == 125000000
    assert revenue.derivation_kind == "semantic_normalized_identity"
    assert policy.checksum_sha256 in revenue.operation
    assert {item.fact.metric_code for item in revenue.lineage} == {"operating_revenue"}
    assert (
        session.scalar(
            select(func.count(FinancialFact.id))
            .join(FinancialMetricDefinition)
            .where(FinancialMetricDefinition.code == "revenue")
        )
        == 0
    )
    source = session.scalar(
        select(SourceRecord).where(SourceRecord.external_record_id.contains("xbrl-fact"))
    )
    assert source is not None and source.validation_status == "accepted"


def test_metric_dictionary_contains_direct_primitives_but_not_a_debt_formula() -> None:
    metrics = {code: (statement, unit, semantic) for code, statement, unit, semantic in METRICS}
    assert metrics["borrowings_current"] == ("balance_sheet", "monetary", "instant")
    assert metrics["borrowings_non_current"] == ("balance_sheet", "monetary", "instant")
    assert metrics["depreciation_amortisation"] == ("income", "monetary", "duration")
    assert "total_debt" in metrics
