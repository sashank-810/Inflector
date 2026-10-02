"""Production D profile, range-bootstrap, GDELT, and immutable-policy regressions."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select

from inflector_core.providers import ProviderMetadata
from inflector_data import nse_cli
from inflector_data.archive import LocalRawObjectStore
from inflector_data.gdelt_attention import (
    GDELT_NEWS_DATASET_CODE,
    GDELT_NEWS_METHOD_DEFINITION_SHA256,
    GDELT_NEWS_METHOD_VERSION,
    GDELT_NEWS_SCOPE_CODE,
    AcquiredGDELTResponse,
    GDELTAttentionError,
    GDELTNewsAttentionProvider,
    gdelt_news_url,
    validate_gdelt_url,
)
from inflector_data.production_policy import bind_production_policy, initialize_production_model
from inflector_data.production_research import ProductionResearchAssembler
from inflector_data.providers import (
    CSVBenchmarkDataProvider,
    CSVFinancialsProvider,
    CSVMarketDataProvider,
)
from inflector_data.research_cli import _run_symbol
from inflector_data.research_profile import (
    load_profile_financial_primitive_policy,
    load_research_profile,
)
from inflector_data.service import IngestionService
from inflector_database.models import (
    AttentionObservation,
    Company,
    DataProvider,
    ExchangeListing,
    ProviderDataset,
    ScoreComponent,
    ScoreSnapshot,
    Security,
    SourceRecord,
)

ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "config" / "research" / "production_research_v1.json"
PROFILE_V3 = ROOT / "config" / "research" / "production_research_v3.json"
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 4, tzinfo=UTC)
RETRIEVED = datetime(2026, 10, 1, 8, tzinfo=UTC)


class _StaticGDELTClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def acquire(self, url: str) -> AcquiredGDELTResponse:
        self.urls.append(url)
        return AcquiredGDELTResponse(url, self.payload, RETRIEVED)


def _payload(*values: int, title: str = '"Fictional Research Limited"') -> bytes:
    return json.dumps(
        {
            "query_details": {"title": title, "date_resolution": "day"},
            "timeline": [
                {
                    "series": "Article Count",
                    "data": [
                        {
                            "date": f"202609{index + 1:02d}T000000Z",
                            "value": value,
                            "norm": 1000,
                        }
                        for index, value in enumerate(values)
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _metadata() -> ProviderMetadata:
    return ProviderMetadata(
        provider_code="gdelt",
        provider_type="https",
        dataset_code=GDELT_NEWS_DATASET_CODE,
        licence_class="public-api-source-terms-reviewed-locally",
        redistributable=False,
    )


def _nse_metadata(dataset_code: str) -> ProviderMetadata:
    return ProviderMetadata(
        provider_code="nse_official",
        provider_type="https",
        dataset_code=dataset_code,
        licence_class="official-source-terms-reviewed-locally",
        redistributable=False,
    )


def test_production_profile_is_explicit_decimal_and_deterministic() -> None:
    first = load_research_profile(PROFILE)
    second = load_research_profile(PROFILE)

    assert first.research_profile_code == "nse_current_research_v1"
    assert first.benchmark_code == "NIFTY 50"
    assert first.financial_scope_priority == ("consolidated", "standalone")
    assert first.source_reliability.as_tuple().exponent == 0
    assert first.analyst_coverage_source is None
    assert first.critical_data_quality_rule_codes == ()
    assert first.checksum_sha256 == second.checksum_sha256
    financial = first.provider_datasets["financial"]
    assert financial is not None
    assert financial.dataset_code == "nse_integrated_financials_xbrl"


def test_production_policy_binds_exact_database_dataset_ids(session) -> None:
    nse = DataProvider(
        code="nse_official",
        provider_type="https",
        licence_name="official-source-terms-reviewed-locally",
        enabled=True,
    )
    gdelt = DataProvider(
        code="gdelt",
        provider_type="https",
        licence_name="public-api-source-terms-reviewed-locally",
        enabled=True,
    )
    session.add_all((nse, gdelt))
    session.flush()
    datasets = {
        domain: ProviderDataset(
            provider_id=(gdelt.id if domain == "news_attention" else nse.id),
            code=code,
            licence_class="reviewed",
            redistributable=False,
        )
        for domain, code in {
            "financial": "nse_integrated_financials_xbrl",
            "market": "nse_cash_market_udiff_daily",
            "benchmark": "nse_indices_daily",
            "corporate_actions": "nse_corporate_actions",
            "business_events": "nse_corporate_announcements",
            "news_attention": "gdelt_doc_company_news_mentions",
        }.items()
    }
    session.add_all(datasets.values())
    session.commit()

    bound = bind_production_policy(session, load_research_profile(PROFILE), repository_root=ROOT)

    assert bound.policy.component_weights.low_market_attention == Decimal("0.05")
    assert bound.policy.financial_context.provider_dataset_priority == (datasets["financial"].id,)
    assert bound.policy.financial_context.filing_scope_priority == (
        "consolidated",
        "standalone",
    )
    assert bound.policy.low_market_attention is not None
    assert bound.policy.low_market_attention.news_series.provider_dataset_id == (
        datasets["news_attention"].id
    )


def test_gdelt_exact_query_and_raw_count_zero_semantics() -> None:
    url = gdelt_news_url("Fictional Research Limited", START, END)
    query = parse_qs(urlparse(url).query)

    assert query == {
        "query": ['"Fictional Research Limited"'],
        "mode": ["TimelineVolRaw"],
        "format": ["json"],
        "startdatetime": ["20260901000000"],
        "enddatetime": ["20260904000000"],
    }
    validate_gdelt_url(url)
    client = _StaticGDELTClient(_payload(0, 0, 0))
    batch = GDELTNewsAttentionProvider(
        client=client,
        metadata=_metadata(),
        company_legal_name="Fictional Research Limited",
        window_start_at=START,
        window_end_at=END,
    ).fetch_attention_data()

    assert batch.raw_payload == _payload(0, 0, 0)
    assert batch.records[0].record.reported_count == 0
    assert batch.records[0].record.coverage_status == "complete"
    assert batch.records[0].available_at == RETRIEVED
    assert batch.records[0].record.window_start_at == START
    assert batch.records[0].record.window_end_at == END
    assert batch.records[0].record.methodology_version == GDELT_NEWS_METHOD_VERSION
    assert batch.records[0].record.scope_code == GDELT_NEWS_SCOPE_CODE
    assert (
        batch.records[0].record.measurement_definition_sha256 == GDELT_NEWS_METHOD_DEFINITION_SHA256
    )


@pytest.mark.parametrize(
    "payload",
    (
        b"not-json",
        b"{}",
        _payload(1, title='"Different Company"'),
        _payload(1, 0),
        json.dumps(
            {
                "query_details": {"title": '"Fictional Research Limited"'},
                "timeline": [{"series": "Article Count", "data": []}],
            }
        ).encode(),
    ),
)
def test_gdelt_malformed_or_incomplete_response_is_unavailable_not_zero(
    payload: bytes,
) -> None:
    provider = GDELTNewsAttentionProvider(
        client=_StaticGDELTClient(payload),
        metadata=_metadata(),
        company_legal_name="Fictional Research Limited",
        window_start_at=START,
        window_end_at=END,
    )
    with pytest.raises(GDELTAttentionError):
        provider.fetch_attention_data()


def test_gdelt_exact_bytes_archive_before_observation_acceptance(session, tmp_path: Path) -> None:
    session.add(
        Company(
            legal_name="Fictional Research Limited",
            display_name="Fictional Research",
            sector="",
            industry="",
        )
    )
    session.commit()
    payload = _payload(2, 3, 5)
    result = IngestionService(session, LocalRawObjectStore(tmp_path / "raw")).ingest_attention_data(
        GDELTNewsAttentionProvider(
            client=_StaticGDELTClient(payload),
            metadata=_metadata(),
            company_legal_name="Fictional Research Limited",
            window_start_at=START,
            window_end_at=END,
        )
    )

    assert result.records_accepted == 1
    observation = session.scalar(select(AttentionObservation))
    source = session.scalar(select(SourceRecord))
    assert observation is not None and observation.reported_count == 10
    assert observation.available_at.replace(tzinfo=UTC) == RETRIEVED
    assert source is not None
    assert (tmp_path / "raw" / source.raw_object_key).read_bytes() == payload
    assert session.scalar(select(func.count()).select_from(AttentionObservation)) == 1


def test_market_range_attempts_exact_dates_and_keeps_source_not_available(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted: list[str] = []

    def source_not_available(*, stage: str, provider: object, ingest: object):
        del provider, ingest
        attempted.append(stage)
        return {
            "stage": stage,
            "status": "source_not_available",
            "source_uri": None,
            "retrieved_at": None,
            "run_id": None,
            "records_received": 0,
            "records_accepted": 0,
            "records_quarantined": 0,
            "records_duplicated": 0,
            "provider_skipped_rows": 0,
        }, False

    monkeypatch.setattr(nse_cli, "_run_stage", source_not_available)
    args = Namespace(
        database_url="unused",
        raw_root=tmp_path / "raw",
        license_class="official-source-terms-reviewed-locally",
        from_date=date(2026, 9, 26),
        to_date=date(2026, 9, 28),
        request_delay_seconds=0,
    )
    summaries, succeeded = nse_cli._execute_market_range(args, session)

    assert succeeded is True
    assert attempted == ["market", "benchmark"] * 3
    assert summaries[0]["requested_calendar_dates"] == 3
    assert summaries[0]["market_source_not_available"] == 3
    assert summaries[0]["benchmark_source_not_available"] == 3
    attempts_value = cast(list[dict[str, object]], summaries[0]["attempts"])
    assert [item["requested_date"] for item in attempts_value] == [
        "2026-09-26",
        "2026-09-26",
        "2026-09-27",
        "2026-09-27",
        "2026-09-28",
        "2026-09-28",
    ]


def test_market_range_rejects_more_than_150_calendar_days(session, tmp_path: Path) -> None:
    args = Namespace(
        raw_root=tmp_path / "raw",
        license_class="official-source-terms-reviewed-locally",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 5, 31),
        request_delay_seconds=0,
    )
    with pytest.raises(ValueError, match="150 calendar days"):
        nse_cli._execute_market_range(args, session)


def test_production_evidence_assembles_partial_v5_without_fabricated_values(
    session, tmp_path: Path
) -> None:
    company = Company(
        legal_name="Fictional Production Components Limited",
        display_name="Fictional Production Components",
        sector="Industrials",
        industry="Components",
    )
    security = Security(
        company=company,
        isin="INE0FPD01019",
        security_type="equity",
        status="active",
    )
    ExchangeListing(
        security=security,
        exchange="NSE",
        symbol="FICTPROD",
        valid_from=date(2020, 1, 1),
        valid_to=None,
        status="active",
    )
    session.add(company)
    session.commit()

    availability = datetime(2026, 9, 30, 12, tzinfo=UTC)
    raw_root = tmp_path / "raw"
    service = IngestionService(session, LocalRawObjectStore(raw_root))
    financial_path = tmp_path / "financials.csv"
    financial_header = (
        "external_id,company_legal_name,filing_external_id,filing_type,filing_scope,"
        "is_restatement,period_kind,period_start,period_end,fiscal_year,fiscal_quarter,"
        "is_ytd,metric_code,reported_value,reported_unit,reported_scale,reported_currency,"
        "published_at,available_at,revision_at"
    )
    financial_rows: list[str] = [financial_header]
    quarter_ends = (
        (2024, 1, date(2024, 4, 1), date(2024, 6, 30)),
        (2024, 2, date(2024, 7, 1), date(2024, 9, 30)),
        (2024, 3, date(2024, 10, 1), date(2024, 12, 31)),
        (2024, 4, date(2025, 1, 1), date(2025, 3, 31)),
        (2025, 1, date(2025, 4, 1), date(2025, 6, 30)),
        (2025, 2, date(2025, 7, 1), date(2025, 9, 30)),
        (2025, 3, date(2025, 10, 1), date(2025, 12, 31)),
        (2025, 4, date(2026, 1, 1), date(2026, 3, 31)),
    )
    for index, (year, quarter, start, end) in enumerate(quarter_ends, start=1):
        values = {
            "operating_revenue": Decimal(100 + index * 10),
            "pat": Decimal(10 + index),
            "cash_flow_from_operations": Decimal(12 + index),
            "total_equity": Decimal(200 + index * 5),
            "ebit": Decimal(18 + index),
            "total_debt": Decimal(40),
            "cash_and_equivalents": Decimal(15),
        }
        for metric, value in values.items():
            financial_rows.append(
                ",".join(
                    (
                        f"financial-{year}-{quarter}-{metric}",
                        company.legal_name,
                        f"filing-{year}-{quarter}",
                        "integrated_financial_results",
                        "standalone",
                        "false",
                        "quarter",
                        start.isoformat(),
                        end.isoformat(),
                        str(year),
                        str(quarter),
                        "false",
                        metric,
                        str(value),
                        "INR",
                        "ones",
                        "INR",
                        "",
                        availability.isoformat(),
                        "",
                    )
                )
            )
    financial_path.write_text("\n".join(financial_rows) + "\n", encoding="utf-8")
    financial_result = service.ingest_financials(
        CSVFinancialsProvider(
            financial_path,
            _nse_metadata("nse_integrated_financials_xbrl"),
            availability,
        )
    )
    assert financial_result.records_quarantined == 0

    market_path = tmp_path / "market.csv"
    benchmark_path = tmp_path / "benchmark.csv"
    market_rows = [
        "external_id,security_isin,trading_date,interval,open,high,low,close,volume,"
        "market_cap,delivery_quantity,delivery_percentage,available_at,revision_at"
    ]
    benchmark_rows = [
        "external_id,benchmark_code,benchmark_display_name,currency,trading_date,interval,"
        "open,high,low,close,available_at,revision_at"
    ]
    for index in range(40):
        trading_date = date(2026, 8, 1) + timedelta(days=index)
        market_rows.append(
            f"market-{index},{security.isin},{trading_date.isoformat()},1d,100,101,99,100,"
            f"1000000,,,,{availability.isoformat()},"
        )
        benchmark_rows.append(
            f"benchmark-{index},NIFTY 50,NIFTY 50,INR,{trading_date.isoformat()},1d,"
            f"24000,24100,23900,24050,{availability.isoformat()},"
        )
    market_path.write_text("\n".join(market_rows) + "\n", encoding="utf-8")
    benchmark_path.write_text("\n".join(benchmark_rows) + "\n", encoding="utf-8")
    assert service.ingest_market_data(
        CSVMarketDataProvider(
            market_path,
            _nse_metadata("nse_cash_market_udiff_daily"),
            availability,
        )
    ).records_quarantined == 0
    assert service.ingest_benchmark_data(
        CSVBenchmarkDataProvider(
            benchmark_path,
            _nse_metadata("nse_indices_daily"),
            availability,
        )
    ).records_quarantined == 0

    nse = session.scalar(select(DataProvider).where(DataProvider.code == "nse_official"))
    assert nse is not None
    session.add_all(
        (
            ProviderDataset(
                provider_id=nse.id,
                code="nse_cash_market_delivery_daily",
                licence_class="official-source-terms-reviewed-locally",
                redistributable=False,
            ),
            ProviderDataset(
                provider_id=nse.id,
                code="nse_corporate_actions",
                licence_class="official-source-terms-reviewed-locally",
                redistributable=False,
            ),
            ProviderDataset(
                provider_id=nse.id,
                code="nse_corporate_announcements",
                licence_class="official-source-terms-reviewed-locally",
                redistributable=False,
            ),
        )
    )
    gdelt = DataProvider(
        code="gdelt",
        provider_type="https",
        licence_name="public-api-source-terms-reviewed-locally",
        enabled=True,
    )
    session.add(gdelt)
    session.flush()
    session.add(
        ProviderDataset(
            provider_id=gdelt.id,
            code=GDELT_NEWS_DATASET_CODE,
            licence_class="public-api-source-terms-reviewed-locally",
            redistributable=False,
        )
    )
    session.commit()

    profile = load_research_profile(PROFILE_V3)
    initialized = initialize_production_model(
        session,
        profile=profile,
        repository_root=ROOT,
        model_family="production_test_v1",
        model_semantic_version="1.0.0",
        git_sha="a" * 40,
        effective_from=datetime(2026, 9, 1, tzinfo=UTC),
    )
    bound = bind_production_policy(session, profile, repository_root=ROOT)
    assembled = ProductionResearchAssembler(
        session,
        profile,
        load_profile_financial_primitive_policy(profile, ROOT),
    ).assemble(
        symbol="FICTPROD",
        fiscal_year=2025,
        fiscal_quarter=4,
        knowledge_cutoff=datetime(2026, 10, 1, tzinfo=UTC),
        dataset_ids=initialized.dataset_ids,
        policy=bound.policy,
    )
    standalone = next(
        candidate
        for candidate in assembled.cross_domain_context_candidates
        if candidate.filing_scope == "standalone"
    )
    assert standalone.financial_inflection is not None
    assert standalone.financial_inflection.revenue_acceleration is not None
    assert standalone.financial_inflection.revenue_acceleration.metric_code == "revenue"
    args = Namespace(
        model_family="production_test_v1",
        research_profile=PROFILE_V3,
        fiscal_year=2025,
        fiscal_quarter=4,
        knowledge_cutoff=datetime(2026, 10, 1, tzinfo=UTC),
        ingest_gdelt_news=False,
        gdelt_raw_root=None,
        gdelt_license_class=None,
    )
    first = _run_symbol(
        session,
        args=args,
        symbol="FICTPROD",
        repository_root=ROOT,
    )
    second = _run_symbol(
        session,
        args=args,
        symbol="FICTPROD",
        repository_root=ROOT,
    )

    assert first["snapshot_status"] == "partial_component_set", json.dumps(first, default=str)
    assert first["final_score"] is None
    assert first["financial_primitive_policy"] == {
        "code": "nse_indas_financial_primitives_v1",
        "checksum_sha256": "d41513bf624f24f11c5a54a3979b4865a0f524514a77cf5849693f57ace95d92",
    }
    assert first["revenue_source_status"] == "APPROVED_DERIVED"
    assert first["reported_ebitda_status"] == "NOT_APPROVED"
    assert first["debt_source_status"] == {
        "borrowings_current": "APPROVED_DIRECT",
        "borrowings_non_current": "APPROVED_DIRECT",
    }
    assert first["total_debt_status"] == "NOT_APPROVED"
    assert first["selected_financial_context"] == {
        "provider_dataset_id": initialized.dataset_ids["financial"],
        "filing_scope": "standalone",
    }
    assert first["unavailable_reasons"] == {
        "valuation": "market_cap_missing",
        "delivery": "delivery_data_missing",
        "analyst_attention": "approved_source_not_configured",
    }
    assert "valuation" in cast(list[str], first["missing_component_codes"])
    assert first["created"] is True
    assert second["created"] is False
    assert second["snapshot_id"] == first["snapshot_id"]
    snapshot = session.get(ScoreSnapshot, first["snapshot_id"])
    assert snapshot is not None and snapshot.algorithm_version == "score_snapshot_v5"
    assert snapshot.final_score is None
    components = list(
        session.scalars(
            select(ScoreComponent).where(ScoreComponent.score_snapshot_id == snapshot.id)
        )
    )
    assert components
    assert all(component.final_contribution is None for component in components)
