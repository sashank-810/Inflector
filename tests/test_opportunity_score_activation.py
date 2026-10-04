"""Phase 4D-K immutable Opportunity Score V5 acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from inflector_core.low_market_attention_scoring import (
    LOW_MARKET_ATTENTION_COMPONENT_VERSION,
)
from inflector_data.attention_features import (
    AttentionFeatureBundle,
    AttentionFeaturePrimitives,
    AttentionSeriesIdentity,
)
from inflector_data.attention_pit import (
    PointInTimeAttentionObservation,
    PointInTimeAttentionReader,
)
from inflector_data.pit import SourceRecordView
from inflector_data.score_orchestration import (
    OPPORTUNITY_SCORE_AGGREGATION_VERSION,
    SCORE_SNAPSHOT_OPPORTUNITY_V5_VERSION,
    V5_COMPONENT_ORDER,
    ScoreSnapshotOrchestrator,
)
from inflector_database.score_repository import (
    V5_COMPONENT_CODES,
    V5_SNAPSHOT_STATUSES,
    ScoreSnapshotIntegrityError,
    ScoreSnapshotRepository,
)
from test_business_catalyst_snapshot_orchestration import (
    _candidate as _catalyst_candidate,
)
from test_business_catalyst_snapshot_orchestration import (
    _lineaged_order_bundle,
)
from test_score_orchestration import (
    AS_OF,
    COMPANY_ID,
    SECURITY_ID,
    _confidence,
    _eligibility,
    _identities,
    _market_structure_bundle,
    _persist_policy,
    _v3_candidate,
    _v3_orchestrator,
    _v3_security,
)

FIXTURES = Path(__file__).parent / "fixtures"
NEWS_PROVIDER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ANALYST_PROVIDER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
NEWS_DEFINITION = "a" * 64
ANALYST_DEFINITION = "b" * 64
NEWS_SERIES = AttentionSeriesIdentity(
    provider_dataset_id=NEWS_PROVIDER_ID,
    scope_code="synthetic_indexed_business_news",
    methodology_version="synthetic_news_search_v1",
    measurement_definition_sha256=NEWS_DEFINITION,
)
ANALYST_SERIES = AttentionSeriesIdentity(
    provider_dataset_id=ANALYST_PROVIDER_ID,
    scope_code="synthetic_active_equity_analysts",
    methodology_version="synthetic_analyst_coverage_v1",
    measurement_definition_sha256=ANALYST_DEFINITION,
)


class _Reader:
    def __init__(
        self,
        news: PointInTimeAttentionObservation | None,
        analyst: PointInTimeAttentionObservation | None,
    ) -> None:
        self.news = news
        self.analyst = analyst

    def news_count_for_exact_window_as_of(
        self, **_: object
    ) -> PointInTimeAttentionObservation | None:
        return self.news

    def latest_analyst_coverage_as_of(
        self, **_: object
    ) -> PointInTimeAttentionObservation | None:
        return self.analyst


def _policy_mapping(providers: tuple[UUID, ...]) -> dict[str, object]:
    value = json.loads(
        (
            FIXTURES / "inflection_model_v1_business_catalyst_development.json"
        ).read_text(encoding="utf-8")
    )
    extension = json.loads(
        (
            FIXTURES / "inflection_model_v1_low_market_attention_development.json"
        ).read_text(encoding="utf-8")
    )
    value.update(extension)
    value["financial_context"]["provider_dataset_priority"] = [
        str(provider) for provider in providers
    ]
    return cast(dict[str, object], value)


def _source(code: str, provider_id: UUID, available_at: datetime) -> SourceRecordView:
    digest = sha256(code.encode()).hexdigest()
    return SourceRecordView(
        id=UUID(digest[:32]),
        external_record_id=code,
        source_uri=f"synthetic://attention/{code}",
        raw_object_key=f"sha256/{digest}",
        raw_payload_reference=f"record:{code}",
        content_sha256=sha256(f"content:{code}".encode()).hexdigest(),
        validation_status="accepted",
        provider_dataset_id=provider_id,
        raw_content_sha256=sha256(f"raw:{code}".encode()).hexdigest(),
        reported_at=available_at,
        published_at=available_at,
        available_at=available_at,
        revision_at=None,
        parse_status="parsed",
    )


def _attention_bundle(
    *,
    news_count: int = 5,
    analyst_count: int = 2,
    news_coverage: str = "complete",
    analyst_coverage: str = "complete",
    news_ingested_at: datetime | None = None,
    analyst_ingested_at: datetime | None = None,
) -> AttentionFeatureBundle:
    window_end = datetime(AS_OF.year, AS_OF.month, AS_OF.day, tzinfo=UTC)
    window_start = window_end - timedelta(days=30)
    news_available = AS_OF - timedelta(hours=4)
    analyst_available = AS_OF - timedelta(hours=3)
    news = PointInTimeAttentionObservation(
        id=UUID("33333333-aaaa-4aaa-8aaa-333333333333"),
        company_id=COMPANY_ID,
        security_id=None,
        provider_dataset_id=NEWS_PROVIDER_ID,
        metric_code="news_mentions_count",
        reported_count=news_count,
        reported_unit="count",
        scope_code=NEWS_SERIES.scope_code,
        methodology_version=NEWS_SERIES.methodology_version,
        measurement_definition_sha256=NEWS_DEFINITION,
        coverage_status=news_coverage,
        observation_date=None,
        window_start_at=window_start,
        window_end_at=window_end,
        available_at=news_available,
        revision_at=None,
        ingested_at=news_ingested_at or news_available + timedelta(minutes=5),
        source_record=_source("NEWS", NEWS_PROVIDER_ID, news_available),
    )
    analyst = PointInTimeAttentionObservation(
        id=UUID("44444444-aaaa-4aaa-8aaa-444444444444"),
        company_id=COMPANY_ID,
        security_id=None,
        provider_dataset_id=ANALYST_PROVIDER_ID,
        metric_code="analyst_coverage_count",
        reported_count=analyst_count,
        reported_unit="count",
        scope_code=ANALYST_SERIES.scope_code,
        methodology_version=ANALYST_SERIES.methodology_version,
        measurement_definition_sha256=ANALYST_DEFINITION,
        coverage_status=analyst_coverage,
        observation_date=AS_OF.date() - timedelta(days=1),
        window_start_at=None,
        window_end_at=None,
        available_at=analyst_available,
        revision_at=None,
        ingested_at=analyst_ingested_at or analyst_available + timedelta(minutes=5),
        source_record=_source("ANALYST", ANALYST_PROVIDER_ID, analyst_available),
    )
    return AttentionFeaturePrimitives(
        cast(PointInTimeAttentionReader, _Reader(news, analyst))
    ).features_as_of(
        company_id=COMPANY_ID,
        security_id=None,
        company_level_only=True,
        as_of=AS_OF,
        news_series=NEWS_SERIES,
        news_window_start_at=window_start,
        news_window_end_at=window_end,
        analyst_series=ANALYST_SERIES,
        analyst_observation_on_or_before=None,
    )


def _run_v5(
    orchestrator: ScoreSnapshotOrchestrator,
    *,
    cross_domain_candidates,
    catalyst_candidates,
    market_structure,
    attention: AttentionFeatureBundle | None,
    family: str = "opportunity_v5",
    eligibility_inputs=None,
):
    return orchestrator.orchestrate_opportunity_score_and_persist(
        model_family=family,
        company_id=COMPANY_ID,
        security_id=SECURITY_ID,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        knowledge_cutoff=AS_OF,
        eligibility_inputs=eligibility_inputs or _eligibility(),
        confidence_inputs=_confidence(),
        cross_domain_context_candidates=tuple(cross_domain_candidates),
        business_catalyst_context_candidates=tuple(catalyst_candidates),
        market_structure_evidence=market_structure,
        low_market_attention_evidence=attention,
    )


def _setup_complete(
    session,
    *,
    family: str = "opportunity_v5",
    include_lma_policy: bool = True,
):
    _, datasets = _identities(session, 5)
    financial, event, market, action, benchmark = (item.id for item in datasets)
    _v3_security(session)
    mapping = _policy_mapping((financial,))
    if not include_lma_policy:
        mapping.pop("low_market_attention")
    _persist_policy(
        session,
        (financial,),
        family=family,
        mapping=mapping,
    )
    financial_candidate = _v3_candidate(financial, market)
    catalyst = _catalyst_candidate(
        financial,
        event,
        _lineaged_order_bundle(
            event_provider_id=event,
            financial_provider_id=financial,
        ),
    )
    return (
        _v3_orchestrator(session),
        financial_candidate,
        catalyst,
        _market_structure_bundle(market, action, benchmark),
    )


def test_v5_complete_reference_activates_exact_opportunity_score(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    first = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    )
    second = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    )
    snapshot = first.record

    assert first.created is True
    assert second.created is False
    assert second.record.id == snapshot.id
    assert snapshot.algorithm_version == SCORE_SNAPSHOT_OPPORTUNITY_V5_VERSION
    assert snapshot.snapshot_status == "final_score_available"
    assert snapshot.top_level_component_weight_coverage == Decimal("1")
    assert snapshot.available_component_codes_json == list(V5_COMPONENT_ORDER)
    assert snapshot.missing_component_codes_json == []
    assert snapshot.final_score == Decimal("70.675")
    assert [item.component_code for item in snapshot.components] == list(V5_COMPONENT_ORDER)
    assert [item.final_contribution for item in snapshot.components] == [
        Decimal("16.6875"),
        Decimal("13.60"),
        Decimal("10.425"),
        Decimal("7.3875"),
        Decimal("6.70"),
        Decimal("7.95"),
        Decimal("4.05"),
        Decimal("3.875"),
    ]
    assert snapshot.fingerprint_payload_json[
        "opportunity_score_aggregation_version"
    ] == OPPORTUNITY_SCORE_AGGREGATION_VERSION
    lma = next(
        item for item in snapshot.components if item.component_code == "low_market_attention"
    )
    assert lma.score == Decimal("77.5")
    assert lma.algorithm_version == LOW_MARKET_ATTENTION_COMPONENT_VERSION
    assert [item.factor_code for item in lma.explanations] == [
        "news_mentions_count",
        "analyst_coverage_count",
    ]
    assert all(
        item.template_code == "low_market_attention_subfactor_v1"
        for item in lma.explanations
    )
    union = cast(dict[str, object], snapshot.input_manifest_json["union"])
    assert len(cast(list[object], union["attention_observations"])) == 2
    assert len(cast(list[object], union["attention_source_records"])) == 2
    assert "ingested_at" not in json.dumps(union)


def test_v5_missing_lma_is_partial_without_top_level_contributions(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=None,
    ).record

    assert snapshot.snapshot_status == "partial_component_set"
    assert snapshot.top_level_component_weight_coverage == Decimal("0.95")
    assert snapshot.missing_component_codes_json == ["low_market_attention"]
    assert snapshot.final_score is None
    assert all(item.final_contribution is None for item in snapshot.components)
    attempt = cast(
        dict[str, object],
        snapshot.context_resolution_json["low_market_attention_attempt"],
    )
    assert attempt["warnings"] == ["low_market_attention_evidence_missing"]


def test_v5_valid_zero_lma_is_available_and_final(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(news_count=80, analyst_count=30),
    ).record

    lma = next(
        item for item in snapshot.components if item.component_code == "low_market_attention"
    )
    assert lma.score == Decimal("0")
    assert lma.final_contribution == Decimal("0")
    assert snapshot.snapshot_status == "final_score_available"
    assert snapshot.top_level_component_weight_coverage == Decimal("1")
    assert snapshot.final_score == Decimal("66.8")


def test_v5_partial_attention_evidence_retains_lineage_but_not_component(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(news_coverage="partial"),
    ).record

    assert snapshot.snapshot_status == "partial_component_set"
    assert snapshot.final_score is None
    assert "low_market_attention" not in snapshot.available_component_codes_json
    attempt = cast(
        dict[str, object],
        snapshot.context_resolution_json["low_market_attention_attempt"],
    )
    assert attempt["warnings"] == ["insufficient_subfactor_coverage"]
    union = cast(dict[str, object], snapshot.input_manifest_json["union"])
    assert len(cast(list[object], union["attention_observations"])) == 2


def test_v5_missing_other_positive_component_never_renormalizes(session) -> None:
    orchestrator, financial, _, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(),
        market_structure=market,
        attention=_attention_bundle(),
    ).record

    assert snapshot.snapshot_status == "partial_component_set"
    assert snapshot.top_level_component_weight_coverage == Decimal("0.80")
    assert snapshot.missing_component_codes_json == ["business_catalyst"]
    assert snapshot.final_score is None
    assert all(item.final_contribution is None for item in snapshot.components)


def test_v5_lma_policy_absence_is_audited_and_remains_partial(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(
        session,
        family="opportunity_v5_no_lma_policy",
        include_lma_policy=False,
    )
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
        family="opportunity_v5_no_lma_policy",
    ).record

    attempt = cast(
        dict[str, object],
        snapshot.context_resolution_json["low_market_attention_attempt"],
    )
    assert attempt["warnings"] == ["low_market_attention_scoring_not_configured"]
    assert snapshot.top_level_component_weight_coverage == Decimal("0.95")
    assert snapshot.final_score is None


def test_v5_ineligible_snapshot_has_no_components_or_final_values(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
        eligibility_inputs=_eligibility(liquidity="0"),
    ).record

    assert snapshot.snapshot_status == "ineligible"
    assert snapshot.components == []
    assert snapshot.top_level_component_weight_coverage == Decimal("0")
    assert snapshot.final_score is None


def test_v5_attention_operational_ingestion_time_does_not_change_fingerprint(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    first = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    ).record
    changed = _attention_bundle(
        news_ingested_at=AS_OF + timedelta(days=10),
        analyst_ingested_at=AS_OF + timedelta(days=20),
    )
    second = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=changed,
    ).record

    assert second.id == first.id
    assert second.snapshot_fingerprint_sha256 == first.snapshot_fingerprint_sha256


def test_v5_repository_constants_and_read_integrity(session) -> None:
    assert V5_COMPONENT_CODES == frozenset(V5_COMPONENT_ORDER)
    assert V5_SNAPSHOT_STATUSES == frozenset(
        {
            "ineligible",
            "implemented_components_unavailable",
            "partial_component_set",
            "final_score_available",
        }
    )
    orchestrator, financial, catalyst, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    ).record
    snapshot.final_score = Decimal("70")
    with pytest.raises(ScoreSnapshotIntegrityError, match="contribution sum"):
        ScoreSnapshotRepository(session).get_score_snapshot(snapshot.id)
    session.rollback()


def test_v5_rejects_attention_company_security_or_cutoff_mismatch(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    valid = _attention_bundle()
    for invalid, message in (
        (replace(valid, company_id=UUID("99999999-9999-4999-8999-999999999999")), "company"),
        (replace(valid, security_id=UUID("88888888-8888-4888-8888-888888888888")), "security"),
        (replace(valid, as_of=AS_OF + timedelta(seconds=1)), "cutoff"),
    ):
        with pytest.raises(ValueError, match=message):
            _run_v5(
                orchestrator,
                cross_domain_candidates=(financial,),
                catalyst_candidates=(catalyst,),
                market_structure=market,
                attention=invalid,
            )


def test_v5_lma_alone_cannot_create_a_partial_snapshot(session) -> None:
    _, datasets = _identities(session, 1)
    financial = datasets[0].id
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="opportunity_v5",
        mapping=_policy_mapping((financial,)),
    )
    snapshot = _run_v5(
        _v3_orchestrator(session),
        cross_domain_candidates=(),
        catalyst_candidates=(),
        market_structure=None,
        attention=_attention_bundle(news_count=0, analyst_count=0),
    ).record

    assert snapshot.snapshot_status == "implemented_components_unavailable"
    assert snapshot.components == []
    assert snapshot.top_level_component_weight_coverage == Decimal("0")
    assert snapshot.final_score is None
    attempt = cast(
        dict[str, object],
        snapshot.context_resolution_json["low_market_attention_attempt"],
    )
    assert attempt["warnings"] == ["financial_context_not_selected"]


def test_v5_does_not_change_v4_identity(session) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    before = orchestrator.orchestrate_business_catalyst_cross_domain_and_persist(
        model_family="opportunity_v5",
        company_id=COMPANY_ID,
        security_id=SECURITY_ID,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        knowledge_cutoff=AS_OF,
        eligibility_inputs=_eligibility(),
        confidence_inputs=_confidence(),
        cross_domain_context_candidates=(financial,),
        business_catalyst_context_candidates=(catalyst,),
        market_structure_evidence=market,
    ).record
    _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    )
    after = orchestrator.orchestrate_business_catalyst_cross_domain_and_persist(
        model_family="opportunity_v5",
        company_id=COMPANY_ID,
        security_id=SECURITY_ID,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        knowledge_cutoff=AS_OF,
        eligibility_inputs=_eligibility(),
        confidence_inputs=_confidence(),
        cross_domain_context_candidates=(financial,),
        business_catalyst_context_candidates=(catalyst,),
        market_structure_evidence=market,
    ).record
    assert after.id == before.id
    assert after.snapshot_fingerprint_sha256 == before.snapshot_fingerprint_sha256


def test_v5_schema_remains_unchanged_by_later_operations_and_evaluation_migrations() -> None:
    versions = tuple((Path("migrations/versions")).glob("*.py"))
    assert len(versions) == 23
    assert any("20260929_0015_attention_observations" in item.name for item in versions)
    assert any("20261002_0016_operational_runs" in item.name for item in versions)
    assert any("20261002_0017_market_delivery_observations" in item.name for item in versions)
    assert any("20261003_0018_historical_backtesting" in item.name for item in versions)
    assert any("20261003_0019_opportunity_discovery" in item.name for item in versions)
    assert any("20261003_0020_opportunity_change_detection" in item.name for item in versions)
    assert any("20261003_0021_research_notification_outbox" in item.name for item in versions)
    assert any(
        "20261003_0022_research_notification_delivery" in item.name for item in versions
    )
    assert any(
        "20261004_0023_historical_multibagger_labels" in item.name for item in versions
    )
