"""Phase 4D-I Business Catalyst cross-domain snapshot acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from inflector_core.business_catalyst_scoring import BUSINESS_CATALYST_COMPONENT_VERSION
from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_RULESET_CODE,
    BUSINESS_EVENT_QUANT_RULESET_VERSION,
)
from inflector_data.announcement_pit import PointInTimeDocument
from inflector_data.business_event_features import (
    QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
    BusinessEventFeatureBundle,
    ResolvedQuantitativeObservation,
)
from inflector_data.business_event_pit import (
    BusinessEventEvidenceView,
    PointInTimeBusinessEvent,
)
from inflector_data.business_event_quantitative_pit import (
    BusinessEventQuantitativeFactView,
    PointInTimeBusinessEventQuantitativeDerivation,
)
from inflector_data.document_text import DocumentAssetView, DocumentTextExtractionView
from inflector_data.period_normalization import QuarterizationLineage, QuarterizedFinancialValue
from inflector_data.pit import (
    FinancialFilingView,
    FinancialMetricView,
    FinancialPeriodView,
    PointInTimeFinancialFact,
    SourceRecordView,
)
from inflector_data.score_orchestration import (
    BUSINESS_CATALYST_CROSS_DOMAIN_COMPONENT_ORDER,
    SCORE_SNAPSHOT_BUSINESS_CATALYST_V4_VERSION,
    BusinessCatalystContextCandidate,
    ScoreSnapshotOrchestrator,
)
from inflector_data.ttm import TrailingTwelveMonthValue, TTMQuarterComponent
from inflector_database.score_repository import (
    V4_COMPONENT_CODES,
    V4_COMPONENT_ORDER,
    V4_SNAPSHOT_STATUSES,
    ScoreSnapshotIntegrityError,
    ScoreSnapshotRepository,
)
from test_business_catalyst_scoring import _bundle as _scoring_bundle
from test_score_orchestration import (
    AS_OF,
    COMPANY_ID,
    SECURITY_ID,
    _confidence,
    _eligibility,
    _identities,
    _market_structure_bundle,
    _persist_policy,
    _run_v3,
    _v3_candidate,
    _v3_orchestrator,
    _v3_security,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _v4_policy_mapping(providers: tuple[UUID, ...]) -> dict[str, object]:
    value = json.loads(
        (FIXTURES / "inflection_model_v1_business_catalyst_development.json").read_text(
            encoding="utf-8"
        )
    )
    value["financial_context"]["provider_dataset_priority"] = [
        str(provider) for provider in providers
    ]
    return cast(dict[str, object], value)


def _bundle(
    *,
    event_provider_id: UUID,
    financial_provider_id: UUID,
    event_type: str = "order_award",
    age_days: Decimal = Decimal("30"),
    order_ratio: Decimal | None = Decimal("0.25"),
    event_id: UUID | None = None,
    announcement_id: UUID | None = None,
    security_id: UUID | None = SECURITY_ID,
) -> BusinessEventFeatureBundle:
    bundle = _scoring_bundle(
        event_type,
        age_days=age_days,
        order_ratio=order_ratio,
        event_id=event_id,
        announcement_id=announcement_id,
        company_id=COMPANY_ID,
        event_provider_id=event_provider_id,
        financial_provider_id=financial_provider_id,
        security_id=security_id,
        as_of=AS_OF,
    )
    return replace(
        bundle,
        quantitative_ruleset_code=None,
        quantitative_ruleset_semantic_version=None,
    )


def _lineaged_order_bundle(
    *, event_provider_id: UUID, financial_provider_id: UUID
) -> BusinessEventFeatureBundle:
    """Build one compact synthetic evidence graph spanning Phases 6A through 6C-C."""

    base = _bundle(
        event_provider_id=event_provider_id,
        financial_provider_id=financial_provider_id,
    )
    event = cast(PointInTimeBusinessEvent, base.event_age_days.evidence[0])
    source = SourceRecordView(
        id=uuid4(),
        external_record_id="V4-DOCUMENT",
        source_uri="synthetic://v4/document",
        raw_object_key="sha256/aa/v4-provider-payload",
        raw_payload_reference="row:V4-DOCUMENT",
        content_sha256="a" * 64,
        validation_status="accepted",
    )
    document = PointInTimeDocument(
        id=uuid4(),
        company_id=COMPANY_ID,
        security_id=SECURITY_ID,
        provider_dataset_id=event_provider_id,
        document_type="exchange_filing",
        title="Fictional order award",
        language="en",
        media_type="text/plain",
        document_uri="synthetic://v4/order.txt",
        document_content_sha256="b" * 64,
        role="primary",
        available_at=base.source_available_at,
        revision_at=None,
        ingested_at=base.source_available_at + timedelta(hours=1),
        source_record=source,
    )
    asset = DocumentAssetView(
        id=uuid4(),
        document_id=document.id,
        content_sha256="b" * 64,
        object_key="sha256/bb/v4-document-bytes",
        size_bytes=64,
        requested_uri=document.document_uri,
        resolved_uri=None,
        declared_media_type="text/plain",
        detected_media_type="text/plain",
        retrieved_at=base.source_available_at + timedelta(hours=2),
        status="verified",
        warnings=(),
    )
    extraction = DocumentTextExtractionView(
        id=uuid4(),
        document_asset_id=asset.id,
        extractor_code="plain_text",
        extractor_semantic_version="plain_text_v1",
        extractor_runtime_version="python-3.12",
        text_object_key="sha256/cc/v4-text",
        text_sha256="c" * 64,
        character_count=72,
        page_count=1,
        status="success",
        warnings=(),
        extracted_at=base.source_available_at + timedelta(hours=3),
    )
    excerpt = "Fictional Engineering Limited has received an order worth INR 250 crore."
    evidence = BusinessEventEvidenceView(
        id=uuid4(),
        evidence_kind="document_text",
        document=document,
        document_asset=asset,
        text_extraction=extraction,
        rule_code="order_award_received_order_v1",
        rule_semantic_version="business_event_rule_v1",
        start_offset=0,
        end_offset=len(excerpt),
        page_numbers=(1,),
        page_text_sha256s=(sha256(excerpt.encode()).hexdigest(),),
        excerpt_text=excerpt,
        excerpt_sha256=sha256(excerpt.encode()).hexdigest(),
        source_available_at=base.source_available_at,
        evidence_fingerprint_sha256="d" * 64,
    )
    announcement = replace(event.announcement, documents=(document,))
    event = replace(event, announcement=announcement, evidence=(evidence,))
    raw_text = "INR 250 crore"
    fact = BusinessEventQuantitativeFactView(
        id=uuid4(),
        business_event_evidence_id=evidence.id,
        fact_code="order_value",
        fact_kind="monetary",
        rule_code="order_value_worth_v1",
        rule_semantic_version="business_event_quantitative_rule_v1",
        start_offset=58,
        end_offset=71,
        raw_text=raw_text,
        raw_text_sha256=sha256(raw_text.encode()).hexdigest(),
        reported_value=Decimal("250"),
        reported_scale="crore",
        reported_unit=None,
        reported_currency="INR",
        normalized_value=Decimal("2500000000"),
        normalized_unit="currency_major",
        date_value=None,
        source_available_at=base.source_available_at,
        warnings=(),
        fact_fingerprint_sha256="e" * 64,
    )
    derivation = PointInTimeBusinessEventQuantitativeDerivation(
        id=uuid4(),
        event=event,
        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
        source_available_at=base.source_available_at,
        available_fact_codes=("order_value",),
        warnings=(),
        derivation_fingerprint_sha256="f" * 64,
        derived_at=base.source_available_at + timedelta(hours=4),
        facts=(fact,),
    )
    financial_source = replace(
        source,
        id=uuid4(),
        external_record_id="V4-REVENUE",
        raw_object_key="sha256/11/v4-financial-payload",
        content_sha256="1" * 64,
    )
    financial_fact = PointInTimeFinancialFact(
        id=uuid4(),
        provider_dataset_id=financial_provider_id,
        company_id=COMPANY_ID,
        metric_code="revenue",
        metric=FinancialMetricView(
            code="revenue",
            statement_kind="income_statement",
            unit_category="monetary",
            semantic_type="flow",
        ),
        reported_value=Decimal("10000000000"),
        reported_unit="INR",
        reported_scale="unit",
        reported_currency="INR",
        normalized_value=Decimal("10000000000"),
        normalized_unit="INR",
        available_at=base.source_available_at - timedelta(days=1),
        revision_at=None,
        ingested_at=base.source_available_at - timedelta(hours=12),
        fiscal_period=FinancialPeriodView(
            id=uuid4(),
            period_kind="quarter",
            period_start=date(2027, 4, 1),
            period_end=date(2027, 6, 30),
            fiscal_year=2027,
            fiscal_quarter=1,
            is_ytd=False,
        ),
        filing=FinancialFilingView(
            id=uuid4(),
            external_filing_id="V4-REVENUE-FILING",
            filing_type="quarterly",
            filing_scope="consolidated",
            is_restatement=False,
            published_at=base.source_available_at - timedelta(days=1),
            available_at=base.source_available_at - timedelta(days=1),
            revision_at=None,
        ),
        source_record=financial_source,
    )
    quarter = QuarterizedFinancialValue(
        provider_dataset_id=financial_provider_id,
        company_id=COMPANY_ID,
        filing_scope="consolidated",
        metric_code="revenue",
        fiscal_year=2027,
        fiscal_quarter=1,
        period_start=date(2027, 4, 1),
        period_end=date(2027, 6, 30),
        value=Decimal("10000000000"),
        unit="INR",
        derivation_kind="reported_quarter",
        operation="identity",
        algorithm_version="period_normalization_v1",
        as_of=base.source_available_at,
        available_at=financial_fact.available_at,
        lineage=(
            QuarterizationLineage(
                role="reported_quarter",
                fact=financial_fact,
                value=financial_fact.normalized_value or financial_fact.reported_value,
                unit="INR",
            ),
        ),
    )
    ttm = TrailingTwelveMonthValue(
        provider_dataset_id=financial_provider_id,
        company_id=COMPANY_ID,
        filing_scope="consolidated",
        metric_code="revenue",
        ending_fiscal_year=2027,
        ending_fiscal_quarter=1,
        period_start=date(2026, 7, 1),
        period_end=date(2027, 6, 30),
        value=Decimal("10000000000"),
        unit="INR",
        construction_kind="four_quarters",
        operation="sum_four_quarters",
        algorithm_version="ttm_v1",
        as_of=base.source_available_at,
        available_at=financial_fact.available_at,
        lineage=(TTMQuarterComponent(role="quarter_4", quarter=quarter),),
    )
    observation = ResolvedQuantitativeObservation(
        fact_code="order_value",
        value=Decimal("2500000000"),
        unit="currency_major",
        currency="INR",
        date_value=None,
        facts=(fact,),
        warnings=(),
        algorithm_version=QUANTITATIVE_OBSERVATION_RESOLUTION_VERSION,
    )
    return replace(
        base,
        quantitative_ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        quantitative_ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
        event_age_days=replace(base.event_age_days, evidence=(event,)),
        order_value_to_ttm_revenue=replace(
            base.order_value_to_ttm_revenue,
            evidence=(observation, ttm),
        ),
        event_time_ttm_revenue=ttm,
        quantitative_derivation=derivation,
    )


def _candidate(
    financial_provider_id: UUID,
    event_provider_id: UUID,
    *bundles: BusinessEventFeatureBundle,
) -> BusinessCatalystContextCandidate:
    return BusinessCatalystContextCandidate(
        financial_provider_dataset_id=financial_provider_id,
        filing_scope="consolidated",
        event_provider_dataset_id=event_provider_id,
        event_bundles=tuple(bundles),
    )


def _run_v4(
    orchestrator: ScoreSnapshotOrchestrator,
    *,
    cross_domain_candidates,
    catalyst_candidates,
    market_structure=None,
    family: str = "cross_domain_v4",
):
    return orchestrator.orchestrate_business_catalyst_cross_domain_and_persist(
        model_family=family,
        company_id=COMPANY_ID,
        security_id=SECURITY_ID,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        knowledge_cutoff=AS_OF,
        eligibility_inputs=_eligibility(),
        confidence_inputs=_confidence(),
        cross_domain_context_candidates=tuple(cross_domain_candidates),
        business_catalyst_context_candidates=tuple(catalyst_candidates),
        market_structure_evidence=market_structure,
    )


def test_v4_full_snapshot_persists_business_catalyst_in_canonical_order(session) -> None:
    _, datasets = _identities(session, 5)
    financial, event, market, action, benchmark = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="cross_domain_v4",
        mapping=_v4_policy_mapping((financial,)),
    )
    orchestrator = _v3_orchestrator(session)
    market_bundle = _market_structure_bundle(market, action, benchmark)
    financial_candidate = _v3_candidate(financial, market)
    event_bundle = _lineaged_order_bundle(
        event_provider_id=event,
        financial_provider_id=financial,
    )
    catalyst_candidate = _candidate(financial, event, event_bundle)

    first = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(catalyst_candidate,),
        market_structure=market_bundle,
    )
    second = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(catalyst_candidate,),
        market_structure=market_bundle,
    )
    snapshot = first.record
    components = {item.component_code: item for item in snapshot.components}

    assert first.created and not second.created and first.record.id == second.record.id
    assert snapshot.algorithm_version == SCORE_SNAPSHOT_BUSINESS_CATALYST_V4_VERSION
    assert BUSINESS_CATALYST_CROSS_DOMAIN_COMPONENT_ORDER == V4_COMPONENT_ORDER
    assert V4_COMPONENT_CODES == frozenset(V4_COMPONENT_ORDER)
    assert V4_SNAPSHOT_STATUSES == frozenset(
        {"ineligible", "implemented_components_unavailable", "partial_component_set"}
    )
    assert snapshot.available_component_codes_json == list(V4_COMPONENT_ORDER)
    assert snapshot.missing_component_codes_json == ["low_market_attention"]
    assert snapshot.top_level_component_weight_coverage == Decimal("0.95")
    assert snapshot.final_score is None
    assert all(item.final_contribution is None for item in snapshot.components)
    assert components["valuation"].score == Decimal("79.5")
    assert components["market_structure"].score == Decimal("81.0")

    catalyst = components["business_catalyst"]
    assert catalyst.score == Decimal("68")
    assert catalyst.configured_top_level_weight == Decimal("0.20")
    assert catalyst.subfactor_weight_coverage == Decimal("1")
    assert catalyst.algorithm_version == BUSINESS_CATALYST_COMPONENT_VERSION
    assert catalyst.final_contribution is None
    assert len(catalyst.explanations) == 1
    explanation = catalyst.explanations[0]
    assert explanation.factor_code == "selected_business_catalyst_event"
    assert explanation.raw_value == explanation.normalized_score == Decimal("68")
    assert explanation.configured_weight == explanation.effective_weight == Decimal("1")
    assert explanation.component_contribution == Decimal("68")
    assert explanation.direction is None
    explanation_available = explanation.input_available_at.replace(
        tzinfo=event_bundle.source_available_at.tzinfo
    )
    assert explanation_available == event_bundle.source_available_at
    assert catalyst.detail_json["event_provider_dataset_id"] == str(event)
    assert catalyst.detail_json["selected_event_type"] == "order_award"
    assert len(cast(list[object], catalyst.detail_json["event_scores"])) == 1

    manifest = snapshot.input_manifest_json
    union = cast(dict[str, object], manifest["union"])
    assert cast(list[object], union["business_events"])
    assert cast(list[object], union["announcements"])
    assert cast(list[object], union["documents"])
    assert cast(list[object], union["document_assets"])
    assert cast(list[object], union["text_extractions"])
    assert cast(list[object], union["business_event_evidence"])
    assert cast(list[object], union["quantitative_derivations"])
    assert cast(list[object], union["quantitative_facts"])
    assert cast(list[object], union["financial_facts"])
    assert cast(list[object], union["market_bars"])
    assert cast(list[object], union["benchmark_bars"])
    assert cast(list[object], union["corporate_actions"])


def test_v4_without_catalyst_retains_v3_coverage_and_fingerprints(session) -> None:
    _, datasets = _identities(session, 4)
    financial, market, action, benchmark = (item.id for item in datasets)
    _v3_security(session)
    mapping = _v4_policy_mapping((financial,))
    _persist_policy(session, (financial,), family="v4_no_catalyst", mapping=mapping)
    orchestrator = _v3_orchestrator(session)
    financial_candidate = _v3_candidate(financial, market)
    market_bundle = _market_structure_bundle(market, action, benchmark)

    v3_before = _run_v3(
        orchestrator,
        (financial_candidate,),
        market_bundle,
        family="v4_no_catalyst",
    )
    v4 = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(),
        market_structure=market_bundle,
        family="v4_no_catalyst",
    )
    v3_after = _run_v3(
        orchestrator,
        (financial_candidate,),
        market_bundle,
        family="v4_no_catalyst",
    )

    assert v4.record.top_level_component_weight_coverage == Decimal("0.75")
    assert v4.record.missing_component_codes_json == [
        "business_catalyst",
        "low_market_attention",
    ]
    v4_attempt = cast(
        dict[str, object], v4.record.context_resolution_json["business_catalyst_attempt"]
    )
    assert v4_attempt["warnings"] == [
        "business_catalyst_evidence_missing"
    ]
    assert v3_before.record.top_level_component_weight_coverage == Decimal("0.75")
    assert v3_before.record.snapshot_fingerprint_sha256 == (
        v3_after.record.snapshot_fingerprint_sha256
    )
    assert v3_before.record.id == v3_after.record.id


def test_business_catalyst_cannot_select_financial_context(session) -> None:
    _, datasets = _identities(session, 4)
    preferred, fallback, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (preferred, fallback),
        family="v4_context",
        mapping=_v4_policy_mapping((preferred, fallback)),
    )
    orchestrator = _v3_orchestrator(session)
    event_id, announcement_id = uuid4(), uuid4()
    preferred_bundle = _bundle(
        event_provider_id=event,
        financial_provider_id=preferred,
        order_ratio=Decimal("1"),
        event_id=event_id,
        announcement_id=announcement_id,
    )
    fallback_bundle = replace(
        preferred_bundle,
        financial_provider_dataset_id=fallback,
        order_value_to_ttm_revenue=replace(
            preferred_bundle.order_value_to_ttm_revenue,
            value=Decimal("0.25"),
        ),
    )
    preferred_financial = _v3_candidate(
        preferred, market, financial=False, valuation=False
    )
    fallback_financial = _v3_candidate(fallback, market)
    preferred_catalyst = _candidate(preferred, event, preferred_bundle)
    fallback_catalyst = _candidate(fallback, event, fallback_bundle)
    result = _run_v4(
        orchestrator,
        cross_domain_candidates=(
            preferred_financial,
            fallback_financial,
        ),
        catalyst_candidates=(
            preferred_catalyst,
            fallback_catalyst,
        ),
        family="v4_context",
    )
    reordered = _run_v4(
        orchestrator,
        cross_domain_candidates=(fallback_financial, preferred_financial),
        catalyst_candidates=(fallback_catalyst, preferred_catalyst),
        family="v4_context",
    )

    assert result.record.selected_provider_dataset_id == fallback
    assert reordered.created is False and reordered.record.id == result.record.id
    assert reordered.record.snapshot_fingerprint_sha256 == (
        result.record.snapshot_fingerprint_sha256
    )
    component = next(
        item for item in result.record.components if item.component_code == "business_catalyst"
    )
    assert component.score == Decimal("68")
    assert component.detail_json["financial_provider_dataset_id"] == str(fallback)


def test_catalyst_alone_cannot_create_partial_snapshot(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_no_financial",
        mapping=_v4_policy_mapping((financial,)),
    )
    result = _run_v4(
        _v3_orchestrator(session),
        cross_domain_candidates=(
            _v3_candidate(financial, market, financial=False, valuation=False),
        ),
        catalyst_candidates=(
            _candidate(
                financial,
                event,
                _bundle(event_provider_id=event, financial_provider_id=financial),
            ),
        ),
        family="v4_no_financial",
    )

    assert result.record.snapshot_status == "implemented_components_unavailable"
    assert result.record.components == []
    assert result.record.top_level_component_weight_coverage == Decimal("0")
    attempt = cast(
        dict[str, object], result.record.context_resolution_json["business_catalyst_attempt"]
    )
    assert attempt["warnings"] == [
        "financial_context_not_selected"
    ]


def test_zero_catalyst_score_is_persisted_as_available(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_zero",
        mapping=_v4_policy_mapping((financial,)),
    )
    result = _run_v4(
        _v3_orchestrator(session),
        cross_domain_candidates=(_v3_candidate(financial, market),),
        catalyst_candidates=(
            _candidate(
                financial,
                event,
                _bundle(
                    event_provider_id=event,
                    financial_provider_id=financial,
                    age_days=Decimal("365"),
                ),
            ),
        ),
        family="v4_zero",
    )
    catalyst = next(
        item for item in result.record.components if item.component_code == "business_catalyst"
    )
    assert catalyst.score == Decimal("0")
    assert result.record.top_level_component_weight_coverage == Decimal("0.90")
    assert "business_catalyst" in result.record.available_component_codes_json


def test_unscoreable_event_set_stays_missing_but_audited(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_neutral",
        mapping=_v4_policy_mapping((financial,)),
    )
    result = _run_v4(
        _v3_orchestrator(session),
        cross_domain_candidates=(_v3_candidate(financial, market),),
        catalyst_candidates=(
            _candidate(
                financial,
                event,
                _bundle(
                    event_provider_id=event,
                    financial_provider_id=financial,
                    event_type="capex_announcement",
                ),
                _bundle(
                    event_provider_id=event,
                    financial_provider_id=financial,
                    event_type="acquisition_agreement",
                ),
            ),
        ),
        family="v4_neutral",
    )

    assert "business_catalyst" not in {
        item.component_code for item in result.record.components
    }
    attempt = cast(
        dict[str, object], result.record.context_resolution_json["business_catalyst_attempt"]
    )
    assert attempt["scoreable"] is False
    assert attempt["warnings"] == ["no_scoreable_business_catalyst_event"]


def test_candidate_and_event_order_do_not_change_v4_fingerprint(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_order",
        mapping=_v4_policy_mapping((financial,)),
    )
    orchestrator = _v3_orchestrator(session)
    order = _bundle(event_provider_id=event, financial_provider_id=financial)
    capacity = _bundle(
        event_provider_id=event,
        financial_provider_id=financial,
        event_type="capacity_expansion",
    )
    financial_candidate = _v3_candidate(financial, market)
    first = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(_candidate(financial, event, order, capacity),),
        family="v4_order",
    )
    second = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(_candidate(financial, event, capacity, order),),
        family="v4_order",
    )

    assert first.record.id == second.record.id
    assert first.record.snapshot_fingerprint_sha256 == second.record.snapshot_fingerprint_sha256
    catalyst = next(
        item for item in first.record.components if item.component_code == "business_catalyst"
    )
    assert catalyst.score == Decimal("76.5")
    expected_available = max(order.source_available_at, capacity.source_available_at)
    assert catalyst.available_at is not None
    assert catalyst.available_at.replace(tzinfo=expected_available.tzinfo) == expected_available


def test_business_catalyst_candidate_coherence_fails_closed(session) -> None:
    _, datasets = _identities(session, 5)
    financial, other_financial, event, other_event, market = (
        item.id for item in datasets
    )
    _v3_security(session)
    _persist_policy(
        session,
        (financial, other_financial),
        family="v4_validation",
        mapping=_v4_policy_mapping((financial, other_financial)),
    )
    orchestrator = _v3_orchestrator(session)
    bundle = _bundle(event_provider_id=event, financial_provider_id=financial)
    base = _candidate(financial, event, bundle)
    kwargs = {
        "cross_domain_candidates": (_v3_candidate(financial, market),),
        "market_structure": None,
        "family": "v4_validation",
    }

    with pytest.raises(ValueError, match="duplicate business catalyst candidate"):
        _run_v4(orchestrator, catalyst_candidates=(base, base), **kwargs)
    with pytest.raises(ValueError, match="must not be empty"):
        _run_v4(
            orchestrator,
            catalyst_candidates=(_candidate(financial, event),),
            **kwargs,
        )
    with pytest.raises(ValueError, match="different event providers"):
        _run_v4(
            orchestrator,
            catalyst_candidates=(
                base,
                _candidate(
                    other_financial,
                    other_event,
                    replace(bundle, financial_provider_dataset_id=other_financial),
                ),
            ),
            **kwargs,
        )
    with pytest.raises(ValueError, match="different event identity sets"):
        _run_v4(
            orchestrator,
            catalyst_candidates=(
                base,
                _candidate(
                    other_financial,
                    event,
                    _bundle(
                        event_provider_id=event,
                        financial_provider_id=other_financial,
                    ),
                ),
            ),
            **kwargs,
        )
    with pytest.raises(ValueError, match="event_age_days feature"):
        _run_v4(
            orchestrator,
            catalyst_candidates=(
                base,
                _candidate(
                    other_financial,
                    event,
                    replace(
                        bundle,
                        financial_provider_dataset_id=other_financial,
                        event_age_days=replace(
                            bundle.event_age_days,
                            algorithm_version="business_event_age_days_v2",
                        ),
                    ),
                ),
            ),
            **kwargs,
        )
    with pytest.raises(ValueError, match="security"):
        _run_v4(
            orchestrator,
            catalyst_candidates=(
                _candidate(
                    financial,
                    event,
                    replace(bundle, security_id=uuid4()),
                ),
            ),
            **kwargs,
        )


def test_company_level_business_event_is_supported(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_company_event",
        mapping=_v4_policy_mapping((financial,)),
    )
    result = _run_v4(
        _v3_orchestrator(session),
        cross_domain_candidates=(_v3_candidate(financial, market),),
        catalyst_candidates=(
            _candidate(
                financial,
                event,
                _bundle(
                    event_provider_id=event,
                    financial_provider_id=financial,
                    security_id=None,
                ),
            ),
        ),
        family="v4_company_event",
    )
    catalyst = next(
        item for item in result.record.components if item.component_code == "business_catalyst"
    )
    assert catalyst.score == Decimal("68")
    assert catalyst.detail_json["security_id"] is None


def test_corrected_event_replay_is_immutable_and_idempotent(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event_provider, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_correction",
        mapping=_v4_policy_mapping((financial,)),
    )
    orchestrator = _v3_orchestrator(session)
    financial_candidate = _v3_candidate(financial, market)
    original = _bundle(
        event_provider_id=event_provider,
        financial_provider_id=financial,
        order_ratio=Decimal("0.10"),
    )
    corrected = _bundle(
        event_provider_id=event_provider,
        financial_provider_id=financial,
        order_ratio=Decimal("0.25"),
    )

    first = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(_candidate(financial, event_provider, original),),
        family="v4_correction",
    )
    second = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(_candidate(financial, event_provider, corrected),),
        family="v4_correction",
    )
    replay = _run_v4(
        orchestrator,
        cross_domain_candidates=(financial_candidate,),
        catalyst_candidates=(_candidate(financial, event_provider, original),),
        family="v4_correction",
    )

    first_catalyst = next(
        item for item in first.record.components if item.component_code == "business_catalyst"
    )
    second_catalyst = next(
        item for item in second.record.components if item.component_code == "business_catalyst"
    )
    assert first_catalyst.score == Decimal("51")
    assert second_catalyst.score == Decimal("68")
    assert first.record.id != second.record.id
    assert first.record.snapshot_fingerprint_sha256 != second.record.snapshot_fingerprint_sha256
    assert replay.created is False and replay.record.id == first.record.id


def test_v4_repository_requires_selected_security_and_supported_codes(session) -> None:
    _, datasets = _identities(session, 3)
    financial, event, market = (item.id for item in datasets)
    _v3_security(session)
    _persist_policy(
        session,
        (financial,),
        family="v4_repository",
        mapping=_v4_policy_mapping((financial,)),
    )
    result = _run_v4(
        _v3_orchestrator(session),
        cross_domain_candidates=(_v3_candidate(financial, market),),
        catalyst_candidates=(
            _candidate(
                financial,
                event,
                _bundle(event_provider_id=event, financial_provider_id=financial),
            ),
        ),
        family="v4_repository",
    )
    snapshot = result.record
    snapshot.selected_security_id = None
    session.flush()
    session.expire(snapshot)
    with pytest.raises(ScoreSnapshotIntegrityError, match="selected security"):
        ScoreSnapshotRepository(session).get_score_snapshot(snapshot.id)
