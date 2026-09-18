"""Phase 4C immutable score snapshot and orchestration acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from inflector_core.component_scoring import (
    FinancialInflectionComponentScorer,
    FinancialInflectionEvidence,
)
from inflector_core.providers import ProviderMetadata
from inflector_core.score_audit import audit_fingerprint_sha256, canonical_audit_value
from inflector_core.scoring_policy import (
    ConfidenceEvaluator,
    ConfidenceInputs,
    EligibilityEvaluator,
    EligibilityInputs,
    FinancialContextPolicyResolver,
    scoring_policy_from_mapping,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.capital_features import ReturnOnCapitalEmployedValue
from inflector_data.financial_features import (
    FinancialInflectionFeatures,
    GrowthAccelerationValue,
    MarginExpansionValue,
    QuarterGrowthValue,
    QuarterMarginValue,
)
from inflector_data.financial_snapshots import InstantFinancialSnapshot
from inflector_data.growth_history_features import (
    ComparableGrowthWindow,
    GrowthConsistencyValue,
    GrowthPersistenceValue,
)
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.score_orchestration import (
    NoActiveScoringConfigurationError,
    ScoreSnapshotOrchestrator,
)
from inflector_data.service import IngestionService
from inflector_data.ttm import TrailingTwelveMonthValue
from inflector_database.models import (
    Company,
    DataProvider,
    ProviderDataset,
    ScoreComponent,
    ScoreExplanation,
    ScoreSnapshot,
)
from inflector_database.score_repository import (
    ScoreSnapshotIntegrityError,
    ScoreSnapshotRepository,
)
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
POLICY_FIXTURE = FIXTURES / "inflection_model_v1_scoring_development.json"
AS_OF = datetime(2027, 8, 1, 12, tzinfo=UTC)
COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _policy_mapping(
    providers: tuple[UUID, ...],
    *,
    minimum_coverage: str = "0.70",
) -> dict[str, object]:
    value = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
    value["financial_context"]["provider_dataset_priority"] = [
        str(provider) for provider in providers
    ]
    value["financial_inflection"]["scoring"]["minimum_weight_coverage"] = (
        minimum_coverage
    )
    return cast(dict[str, object], value)


def _identities(session, provider_count: int = 2) -> tuple[Company, tuple[ProviderDataset, ...]]:
    company = Company(
        id=COMPANY_ID,
        legal_name="Phase 4C Synthetic Limited",
        display_name="Phase 4C Synthetic",
        sector="Synthetic",
        industry="Testing",
    )
    session.add(company)
    datasets: list[ProviderDataset] = []
    for index in range(provider_count):
        provider = DataProvider(
            code=f"phase4c_provider_{index}",
            provider_type="synthetic",
            licence_name="synthetic-development-only",
            enabled=True,
        )
        session.add(provider)
        session.flush()
        dataset = ProviderDataset(
            provider_id=provider.id,
            code="financials",
            licence_class="synthetic",
            redistributable=False,
        )
        session.add(dataset)
        datasets.append(dataset)
    session.flush()
    return company, tuple(datasets)


def _persist_policy(
    session,
    providers: tuple[UUID, ...],
    *,
    family: str = "inflection",
    semantic_version: str = "1.0.0",
    config_version: str = "1",
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
    minimum_coverage: str = "0.70",
    mapping: dict[str, object] | None = None,
):
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family=family,
        semantic_version=semantic_version,
        git_sha=semantic_version.replace(".", "") * 10,
        status="active",
    )
    persisted = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version=config_version,
        status="active",
        policy=scoring_policy_from_mapping(
            mapping or _policy_mapping(providers, minimum_coverage=minimum_coverage)
        ),
        effective_from=effective_from,
        effective_to=effective_to,
    )
    return repository, model, persisted


def _orchestrator(session) -> ScoreSnapshotOrchestrator:
    return ScoreSnapshotOrchestrator(
        ScoringPolicyRepository(session),
        ScoreSnapshotRepository(session),
        FinancialContextPolicyResolver(),
        EligibilityEvaluator(),
        ConfidenceEvaluator(),
        FinancialInflectionComponentScorer(),
    )


def _eligibility(*, liquidity: str = "1000000") -> EligibilityInputs:
    return EligibilityInputs(
        security_type="equity",
        security_status="active",
        listing_status="active",
        comparable_yoy_observations=8,
        financial_core_available=4,
        financial_core_required=4,
        average_daily_traded_value_inr=Decimal(liquidity),
        has_critical_data_quality_issue=False,
        is_new_listing=False,
    )


def _confidence(cutoff: datetime = AS_OF) -> ConfidenceInputs:
    return ConfidenceInputs(
        required_feature_count=4,
        available_feature_count=4,
        source_reliability=Decimal("1"),
        freshest_required_evidence_at=None,
        knowledge_cutoff=cutoff,
        comparable_history_observations=8,
        evidence_confidence=None,
    )


def _acceleration(
    provider_id: UUID,
    metric: str,
    value: str,
    *,
    scope: str = "consolidated",
    as_of: datetime = AS_OF,
) -> GrowthAccelerationValue:
    growth = cast(QuarterGrowthValue, object())
    return GrowthAccelerationValue(
        provider_dataset_id=provider_id,
        company_id=COMPANY_ID,
        filing_scope=scope,
        metric_code=metric,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        current_yoy=growth,
        prior_yoys=(growth, growth, growth),
        prior_median=Decimal("0.1"),
        acceleration=Decimal(value),
        operation="current_yoy_minus_prior3_median",
        as_of=as_of,
        available_at=as_of - timedelta(days=1),
        algorithm_version="growth_acceleration_v1",
    )


def _margin(provider_id: UUID, bps: str, scope: str = "consolidated") -> MarginExpansionValue:
    margin = cast(QuarterMarginValue, object())
    return MarginExpansionValue(
        provider_dataset_id=provider_id,
        company_id=COMPANY_ID,
        filing_scope=scope,
        margin_code="operating_margin",
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        current_margin=margin,
        prior_year_margin=margin,
        change=Decimal(bps) / Decimal("10000"),
        basis_points=Decimal(bps),
        operation="current_margin_minus_prior_year_margin",
        as_of=AS_OF,
        available_at=AS_OF - timedelta(days=2),
        algorithm_version="margin_expansion_v1",
    )


def _roce(provider_id: UUID, year: int, value: str, scope: str) -> ReturnOnCapitalEmployedValue:
    return ReturnOnCapitalEmployedValue(
        provider_dataset_id=provider_id,
        company_id=COMPANY_ID,
        filing_scope=scope,
        ending_fiscal_year=year,
        ending_fiscal_quarter=2,
        value=Decimal(value),
        unit="ratio",
        ttm_ebit=cast(TrailingTwelveMonthValue, object()),
        beginning_snapshot=cast(InstantFinancialSnapshot, object()),
        ending_snapshot=cast(InstantFinancialSnapshot, object()),
        beginning_capital_employed=Decimal("100"),
        ending_capital_employed=Decimal("120"),
        average_capital_employed=Decimal("110"),
        warnings=(),
        as_of=AS_OF,
        available_at=AS_OF - timedelta(days=3),
        algorithm_version="roce_v1",
    )


def _consistency(provider_id: UUID, share: str, scope: str) -> GrowthConsistencyValue:
    return GrowthConsistencyValue(
        provider_dataset_id=provider_id,
        company_id=COMPANY_ID,
        filing_scope=scope,
        metric_code="revenue",
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        window_size=4,
        positive_count=int(Decimal(share) * 4),
        non_positive_count=4 - int(Decimal(share) * 4),
        positive_share=Decimal(share),
        unit="ratio",
        window=cast(ComparableGrowthWindow, object()),
        as_of=AS_OF,
        available_at=AS_OF - timedelta(days=4),
        algorithm_version="growth_consistency_v1",
    )


def _persistence(provider_id: UUID, count: int, scope: str) -> GrowthPersistenceValue:
    return GrowthPersistenceValue(
        provider_dataset_id=provider_id,
        company_id=COMPANY_ID,
        filing_scope=scope,
        metric_code="revenue",
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        window_size=4,
        threshold=Decimal("0.1"),
        streak_count=count,
        window_saturated=count == 4,
        streak_observations=(),
        stopping_observation=None,
        window=cast(ComparableGrowthWindow, object()),
        as_of=AS_OF,
        available_at=AS_OF - timedelta(days=5),
        algorithm_version="growth_persistence_v1",
    )


def _candidate(
    provider_id: UUID,
    *,
    scope: str = "consolidated",
    mode: str = "standard",
) -> FinancialInflectionEvidence:
    if mode == "low":
        revenue, pat, bps, current_roce, prior_roce, share, streak = (
            "-20",
            "-20",
            "-1000",
            "0",
            "0.1",
            "0",
            0,
        )
    elif mode == "high":
        revenue, pat, bps, current_roce, prior_roce, share, streak = (
            "20",
            "20",
            "2000",
            "1",
            "0",
            "1",
            4,
        )
    else:
        revenue, pat, bps, current_roce, prior_roce, share, streak = (
            "0.2",
            "0.1",
            "500",
            "0.20",
            "0.15",
            "0.75",
            3,
        )
    context = FinancialContextPolicyResolver().resolve(
        scoring_policy_from_mapping(_policy_mapping((provider_id,))).financial_context,
        ((provider_id, scope),),
    )
    assert context is not None
    return FinancialInflectionEvidence(
        company_id=COMPANY_ID,
        context=context,
        fiscal_year=2027,
        fiscal_quarter=2,
        as_of=AS_OF,
        revenue_acceleration=_acceleration(provider_id, "revenue", revenue, scope=scope),
        pat_acceleration=_acceleration(provider_id, "pat", pat, scope=scope),
        margin_expansion=_margin(provider_id, bps, scope),
        current_roce=_roce(provider_id, 2027, current_roce, scope),
        prior_year_roce=_roce(provider_id, 2026, prior_roce, scope),
        growth_consistency=_consistency(provider_id, share, scope),
        growth_persistence=_persistence(provider_id, streak, scope),
    )


def _partial_candidate(
    provider_id: UUID,
    scope: str = "consolidated",
) -> FinancialInflectionEvidence:
    full = _candidate(provider_id, scope=scope)
    return FinancialInflectionEvidence(
        company_id=full.company_id,
        context=full.context,
        fiscal_year=full.fiscal_year,
        fiscal_quarter=full.fiscal_quarter,
        as_of=full.as_of,
        revenue_acceleration=full.revenue_acceleration,
    )


def _run(
    orchestrator: ScoreSnapshotOrchestrator,
    candidates: tuple[FinancialInflectionEvidence, ...],
    *,
    model_family: str = "inflection",
    eligibility: EligibilityInputs | None = None,
    cutoff: datetime = AS_OF,
):
    return orchestrator.orchestrate_and_persist(
        model_family=model_family,
        company_id=COMPANY_ID,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        knowledge_cutoff=cutoff,
        eligibility_inputs=eligibility or _eligibility(),
        confidence_inputs=_confidence(cutoff),
        financial_inflection_candidates=candidates,
    )


def test_partial_snapshot_is_idempotent_and_keeps_score_confidence_separate(session) -> None:
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    orchestrator = _orchestrator(session)

    first = _run(orchestrator, (_candidate(datasets[0].id),))
    second = _run(orchestrator, (_candidate(datasets[0].id),))
    snapshot = first.record
    component = snapshot.components[0]
    explanations = sorted(component.explanations, key=lambda item: item.rank)

    assert first.created and not second.created and first.record.id == second.record.id
    assert snapshot.snapshot_status == "partial_component_set"
    assert snapshot.final_score is None and snapshot.confidence == Decimal("0.65")
    assert snapshot.top_level_component_weight_coverage == Decimal("0.25")
    assert snapshot.available_component_codes_json == ["financial_inflection"]
    assert snapshot.missing_component_codes_json == [
        "business_catalyst",
        "business_quality",
        "cash_flow_quality",
        "balance_sheet",
        "valuation",
        "market_structure",
        "low_market_attention",
    ]
    assert component.score == Decimal("66.75")
    assert component.configured_top_level_weight == Decimal("0.25")
    assert component.subfactor_weight_coverage == Decimal("1")
    assert component.final_contribution is None
    assert [item.factor_code for item in explanations] == [
        "revenue_acceleration",
        "margin_expansion",
        "pat_acceleration",
        "roce_improvement",
        "growth_consistency",
        "growth_persistence",
    ]
    assert explanations[0].raw_value == Decimal("0.2")
    assert explanations[0].normalized_score == Decimal("70")
    assert explanations[0].component_contribution == Decimal("21")
    assert all(item.direction is None for item in explanations)
    assert session.scalar(select(func.count()).select_from(ScoreSnapshot)) == 1
    assert session.scalar(select(func.count()).select_from(ScoreComponent)) == 1
    assert session.scalar(select(func.count()).select_from(ScoreExplanation)) == 6


def test_context_selection_prefers_priority_not_score_or_extra_coverage(session) -> None:
    _, datasets = _identities(session, 2)
    _persist_policy(session, tuple(item.id for item in datasets))
    orchestrator = _orchestrator(session)

    score_priority = _run(
        orchestrator,
        (_candidate(datasets[0].id, mode="low"), _candidate(datasets[1].id, mode="high")),
    ).record
    assert score_priority.selected_provider_dataset_id == datasets[0].id
    assert score_priority.components[0].score == Decimal("0")

    session.query(ScoreExplanation).delete()
    session.query(ScoreComponent).delete()
    session.query(ScoreSnapshot).delete()
    session.flush()
    partial = _candidate(datasets[0].id)
    partial = FinancialInflectionEvidence(
        company_id=partial.company_id,
        context=partial.context,
        fiscal_year=partial.fiscal_year,
        fiscal_quarter=partial.fiscal_quarter,
        as_of=partial.as_of,
        revenue_acceleration=partial.revenue_acceleration,
        pat_acceleration=partial.pat_acceleration,
        current_roce=partial.current_roce,
        prior_year_roce=partial.prior_year_roce,
    )
    coverage_priority = _run(
        orchestrator,
        (partial, _candidate(datasets[1].id, mode="high")),
    ).record
    assert coverage_priority.selected_provider_dataset_id == datasets[0].id
    assert coverage_priority.components[0].subfactor_weight_coverage == Decimal("0.70")


def test_context_fallback_uses_scoreability_and_provider_first_lexicographic_order(session) -> None:
    _, datasets = _identities(session, 2)
    _persist_policy(session, tuple(item.id for item in datasets))
    orchestrator = _orchestrator(session)

    record = _run(
        orchestrator,
        (
            _partial_candidate(datasets[0].id, "consolidated"),
            _candidate(datasets[0].id, scope="standalone"),
            _candidate(datasets[1].id, scope="consolidated", mode="high"),
        ),
    ).record

    assert record.selected_provider_dataset_id == datasets[0].id
    assert record.selected_filing_scope == "standalone"
    selected = cast(dict[str, object], record.context_resolution_json["selected"])
    assert selected["fallback_used"] is True
    assert selected["selection_reason"] == "preferred_provider_scope_fallback"
    attempts = cast(list[dict[str, object]], record.context_resolution_json["attempts"])
    assert [attempt["scoreable"] for attempt in attempts] == [False, True, True]
    assert attempts[0]["subfactor_weight_coverage"] == "0.3"


def test_ineligible_and_unavailable_snapshots_never_store_zero_scores(session) -> None:
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    orchestrator = _orchestrator(session)

    ineligible = _run(
        orchestrator,
        (_candidate(datasets[0].id),),
        eligibility=_eligibility(liquidity="999999"),
    ).record
    unavailable = _run(orchestrator, (_partial_candidate(datasets[0].id),)).record

    assert ineligible.snapshot_status == "ineligible"
    assert not ineligible.eligibility_eligible
    assert ineligible.eligibility_reasons_json == ["below_liquidity_floor"]
    assert ineligible.confidence == Decimal("0.65")
    assert ineligible.final_score is None and ineligible.components == []
    assert unavailable.snapshot_status == "financial_inflection_unavailable"
    assert unavailable.final_score is None and unavailable.components == []
    assert unavailable.top_level_component_weight_coverage == Decimal("0")


def test_zero_weight_future_component_is_not_reported_missing(session) -> None:
    _, datasets = _identities(session, 1)
    mapping = _policy_mapping((datasets[0].id,))
    weights = mapping["component_weights"]
    weights["balance_sheet"] = "0"  # type: ignore[index]
    weights["valuation"] = "0.20"  # type: ignore[index]
    _persist_policy(session, (datasets[0].id,), mapping=mapping)

    record = _run(_orchestrator(session), (_candidate(datasets[0].id),)).record
    assert record.top_level_component_weight_coverage == Decimal("0.25")
    assert "balance_sheet" not in record.missing_component_codes_json
    assert record.components[0].score == Decimal("66.75")


def test_same_cutoff_changed_evidence_creates_distinct_immutable_history(session) -> None:
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    orchestrator = _orchestrator(session)

    original_candidate = _candidate(datasets[0].id)
    assert original_candidate.revenue_acceleration is not None
    changed_acceleration = replace(
        original_candidate.revenue_acceleration,
        algorithm_version="growth_acceleration_v1_backfill",
    )
    changed_candidate = replace(
        original_candidate,
        revenue_acceleration=changed_acceleration,
    )
    original = _run(orchestrator, (original_candidate,)).record
    changed = _run(orchestrator, (changed_candidate,)).record
    history = ScoreSnapshotRepository(session).list_company_score_snapshots(COMPANY_ID)
    repeated_history = ScoreSnapshotRepository(session).list_company_score_snapshots(COMPANY_ID)

    assert original.id != changed.id
    assert original.snapshot_fingerprint_sha256 != changed.snapshot_fingerprint_sha256
    assert original.components[0].score == Decimal("66.75")
    assert changed.components[0].score == Decimal("66.75")
    assert original.input_manifest_json != changed.input_manifest_json
    assert {item.id for item in history} == {original.id, changed.id}
    assert [item.id for item in history] == [item.id for item in repeated_history]


def test_wide_decimals_round_trip_exactly_without_float_conversion(session) -> None:
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    candidate = _candidate(datasets[0].id)
    candidate = replace(candidate, growth_persistence=None)
    confidence = ConfidenceInputs(
        required_feature_count=4,
        available_feature_count=2,
        source_reliability=Decimal("1"),
        freshest_required_evidence_at=AS_OF,
        knowledge_cutoff=AS_OF,
        comparable_history_observations=8,
        evidence_confidence=Decimal("1"),
    )
    result = _orchestrator(session).orchestrate_and_persist(
        model_family="inflection",
        company_id=COMPANY_ID,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        knowledge_cutoff=AS_OF,
        eligibility_inputs=_eligibility(),
        confidence_inputs=confidence,
        financial_inflection_candidates=(candidate,),
    ).record

    assert result.confidence == Decimal("0.875")
    assert result.top_level_component_weight_coverage == Decimal("0.25")
    assert result.components[0].score is not None
    assert result.components[0].subfactor_weight_coverage == Decimal("0.95")


def test_configuration_and_model_changes_create_new_fingerprints(session) -> None:
    _, datasets = _identities(session, 1)
    repository, model, first_config = _persist_policy(session, (datasets[0].id,))
    orchestrator = _orchestrator(session)
    first = _run(orchestrator, (_candidate(datasets[0].id),)).record

    first_config.record.status = "retired"
    second_config = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="2",
        status="active",
        policy=scoring_policy_from_mapping(_policy_mapping((datasets[0].id,))),
    )
    session.flush()
    second = _run(orchestrator, (_candidate(datasets[0].id),)).record
    assert first.scoring_configuration_id != second.scoring_configuration_id
    assert first.snapshot_fingerprint_sha256 != second.snapshot_fingerprint_sha256

    second_config.record.status = "retired"
    model.status = "retired"
    _, new_model, _ = _persist_policy(
        session,
        (datasets[0].id,),
        semantic_version="2.0.0",
        config_version="3",
    )
    third = _run(orchestrator, (_candidate(datasets[0].id),)).record
    assert third.model_version_id == new_model.id
    assert third.snapshot_fingerprint_sha256 != second.snapshot_fingerprint_sha256


def test_active_policy_interval_boundary_and_no_active_configuration(session) -> None:
    _, datasets = _identities(session, 1)
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="temporal",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="active",
    )
    old_policy = scoring_policy_from_mapping(_policy_mapping((datasets[0].id,)))
    changed_mapping = _policy_mapping((datasets[0].id,))
    changed_mapping["financial_inflection"]["scoring"]["minimum_weight_coverage"] = "0.80"  # type: ignore[index]
    boundary = AS_OF + timedelta(days=1)
    old = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="active",
        policy=old_policy,
        effective_to=boundary,
    )
    new = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="2",
        status="active",
        policy=scoring_policy_from_mapping(changed_mapping),
        effective_from=boundary,
    )
    orchestrator = _orchestrator(session)
    before = _run(
        orchestrator,
        (_candidate(datasets[0].id),),
        model_family="temporal",
        cutoff=AS_OF,
    ).record
    at_boundary = _run(orchestrator, (), model_family="temporal", cutoff=boundary).record
    assert before.scoring_configuration_id == old.record.id
    assert at_boundary.scoring_configuration_id == new.record.id

    with pytest.raises(NoActiveScoringConfigurationError):
        _run(orchestrator, (), model_family="missing")


def test_audit_serializer_is_canonical_and_rejects_float_or_naive_time() -> None:
    first = {
        "decimal": Decimal("0.10"),
        "time": datetime(2027, 8, 1, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        "id": COMPANY_ID,
    }
    second = dict(reversed(tuple(first.items())))
    second["decimal"] = Decimal("0.1")

    assert audit_fingerprint_sha256(first) == audit_fingerprint_sha256(second)
    assert canonical_audit_value(first)["time"] == "2027-08-01T12:00:00.000000Z"  # type: ignore[index]
    with pytest.raises(TypeError, match="binary floats"):
        canonical_audit_value({"bad": 0.1})
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_audit_value({"bad": datetime(2027, 8, 1)})


def test_repository_detects_fingerprint_corruption_and_orders_history(session) -> None:
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    orchestrator = _orchestrator(session)
    older = _run(orchestrator, (_candidate(datasets[0].id),)).record
    newer_candidate = _candidate(datasets[0].id, mode="high")
    newer = _run(orchestrator, (newer_candidate,)).record
    history = ScoreSnapshotRepository(session).list_company_score_snapshots(COMPANY_ID)
    assert {item.id for item in history} == {older.id, newer.id}

    newer.fingerprint_payload_json = {"corrupted": True}
    session.flush()
    with pytest.raises(ScoreSnapshotIntegrityError):
        ScoreSnapshotRepository(session).get_score_snapshot(newer.id)


def test_database_unique_and_foreign_key_constraints(session) -> None:
    session.execute(text("PRAGMA foreign_keys=ON"))
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    record = _run(_orchestrator(session), (_candidate(datasets[0].id),)).record
    component = record.components[0]
    explanation = component.explanations[0]
    component_id = component.id
    factor_code = explanation.factor_code
    snapshot_values = {
        "company_id": record.company_id,
        "model_version_id": record.model_version_id,
        "scoring_configuration_id": record.scoring_configuration_id,
        "configuration_checksum_sha256": record.configuration_checksum_sha256,
        "as_of_date": record.as_of_date,
        "knowledge_cutoff": record.knowledge_cutoff,
        "ending_fiscal_year": record.ending_fiscal_year,
        "ending_fiscal_quarter": record.ending_fiscal_quarter,
        "selected_provider_dataset_id": record.selected_provider_dataset_id,
        "selected_filing_scope": record.selected_filing_scope,
        "snapshot_status": record.snapshot_status,
        "eligibility_eligible": record.eligibility_eligible,
        "eligibility_inputs_json": record.eligibility_inputs_json,
        "eligibility_reasons_json": record.eligibility_reasons_json,
        "eligibility_warnings_json": record.eligibility_warnings_json,
        "financial_core_coverage": record.financial_core_coverage,
        "confidence": record.confidence,
        "confidence_inputs_json": record.confidence_inputs_json,
        "confidence_details_json": record.confidence_details_json,
        "top_level_component_weight_coverage": (
            record.top_level_component_weight_coverage
        ),
        "available_component_codes_json": record.available_component_codes_json,
        "missing_component_codes_json": record.missing_component_codes_json,
        "context_resolution_json": record.context_resolution_json,
        "input_manifest_json": record.input_manifest_json,
        "fingerprint_payload_json": record.fingerprint_payload_json,
        "snapshot_fingerprint_sha256": record.snapshot_fingerprint_sha256,
        "final_score": None,
        "algorithm_version": record.algorithm_version,
    }
    session.commit()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(ScoreSnapshot(**snapshot_values))
            session.flush()

    invalid_snapshot_values = dict(snapshot_values)
    invalid_snapshot_values["model_version_id"] = uuid4()
    invalid_snapshot_values["snapshot_fingerprint_sha256"] = "f" * 64
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(ScoreSnapshot(**invalid_snapshot_values))
            session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                ScoreComponent(
                    score_snapshot_id=record.id,
                    component_code="financial_inflection",
                    score=Decimal("1"),
                    unit="score_0_100",
                    configured_top_level_weight=Decimal("0.25"),
                    subfactor_weight_coverage=Decimal("1"),
                    final_contribution=None,
                    available_at=AS_OF,
                    algorithm_version="test",
                    missing_subfactors_json=[],
                    warnings_json=[],
                    detail_json={},
                )
            )
            session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                ScoreExplanation(
                    score_snapshot_id=record.id,
                    score_component_id=component_id,
                    factor_code=factor_code,
                    rank=99,
                    raw_value=Decimal("1"),
                    raw_unit="ratio",
                    normalized_score=Decimal("1"),
                    configured_weight=Decimal("1"),
                    effective_weight=Decimal("1"),
                    component_contribution=Decimal("1"),
                    input_available_at=AS_OF,
                    evidence_type="test",
                    template_code="test",
                    direction=None,
                    evidence_manifest_json={},
                )
            )
            session.flush()

    invalid = ScoreComponent(
        score_snapshot_id=uuid4(),
        component_code="financial_inflection",
        score=None,
        unit="score_0_100",
        configured_top_level_weight=Decimal("0.25"),
        subfactor_weight_coverage=Decimal("0"),
        final_contribution=None,
        available_at=None,
        algorithm_version="test",
        missing_subfactors_json=[],
        warnings_json=[],
        detail_json={},
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(invalid)
            session.flush()


def test_policy_fixture_checksums_remain_stable() -> None:
    from inflector_core.scoring_policy import scoring_policy_checksum

    phase4a = json.loads(
        (FIXTURES / "inflection_model_v1_development.json").read_text(encoding="utf-8")
    )
    phase4b = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
    assert scoring_policy_checksum(scoring_policy_from_mapping(phase4a)) == (
        "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf"
    )
    assert scoring_policy_checksum(scoring_policy_from_mapping(phase4b)) == (
        "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9"
    )


def test_orchestrator_rejects_duplicate_context_cutoff_and_naive_time(session) -> None:
    _, datasets = _identities(session, 1)
    _persist_policy(session, (datasets[0].id,))
    orchestrator = _orchestrator(session)
    candidate = _candidate(datasets[0].id)
    with pytest.raises(ValueError, match="duplicate"):
        _run(orchestrator, (candidate, candidate))
    with pytest.raises(ValueError, match="confidence knowledge cutoff"):
        orchestrator.orchestrate_and_persist(
            model_family="inflection",
            company_id=COMPANY_ID,
            ending_fiscal_year=2027,
            ending_fiscal_quarter=2,
            knowledge_cutoff=AS_OF,
            eligibility_inputs=_eligibility(),
            confidence_inputs=_confidence(AS_OF + timedelta(seconds=1)),
            financial_inflection_candidates=(candidate,),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _run(orchestrator, (candidate,), cutoff=datetime(2027, 8, 1, 12))

    ist = timezone(timedelta(hours=5, minutes=30))
    offset_result = _run(
        orchestrator,
        (candidate,),
        cutoff=datetime(2027, 8, 1, 17, 30, tzinfo=ist),
    ).record
    assert offset_result.as_of_date.isoformat() == "2027-08-01"
    assert offset_result.fingerprint_payload_json["knowledge_cutoff"] == (
        "2027-08-01T12:00:00.000000Z"
    )


def test_real_pit_lineage_restatement_and_historical_idempotency(session, tmp_path: Path) -> None:
    universe = ProviderMetadata(
        "score_pit_universe", "csv", "universe", "synthetic-development-only"
    )
    financials = ProviderMetadata(
        "score_pit_financials", "csv", "financials", "synthetic-development-only"
    )
    retrieved = datetime(2030, 1, 1, 12, tzinfo=UTC)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", universe, retrieved)
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "financials_growth_history_synthetic.csv", financials, retrieved
        )
    )
    company_id = session.scalar(
        select(Company.id).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(DataProvider.code == "score_pit_financials")
    )
    assert company_id is not None and dataset_id is not None
    features = FinancialInflectionFeatures(
        FiscalQuarterNormalizer(PointInTimeFinancialReader(session))
    )
    mapping = _policy_mapping((dataset_id,), minimum_coverage="0.30")
    policy_repository = ScoringPolicyRepository(session)
    model = policy_repository.create_model_version(
        model_family="real_pit",
        semantic_version="1.0.0",
        git_sha="b" * 40,
        status="active",
    )
    policy_repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="real_pit",
        configuration_version="1",
        status="active",
        policy=scoring_policy_from_mapping(mapping),
    )
    orchestrator = _orchestrator(session)

    def score_at(cutoff: datetime):
        acceleration = features.growth_acceleration_as_of(
            provider_dataset_id=dataset_id,
            company_id=company_id,
            filing_scope="standalone",
            metric_code="revenue",
            fiscal_year=2026,
            fiscal_quarter=4,
            as_of=cutoff,
        )
        assert acceleration is not None
        selection = FinancialContextPolicyResolver().resolve(
            scoring_policy_from_mapping(mapping).financial_context,
            ((dataset_id, "standalone"),),
        )
        assert selection is not None
        evidence = FinancialInflectionEvidence(
            company_id=company_id,
            context=selection,
            fiscal_year=2026,
            fiscal_quarter=4,
            as_of=cutoff,
            revenue_acceleration=acceleration,
        )
        return orchestrator.orchestrate_and_persist(
            model_family="real_pit",
            company_id=company_id,
            ending_fiscal_year=2026,
            ending_fiscal_quarter=4,
            knowledge_cutoff=cutoff,
            eligibility_inputs=_eligibility(),
            confidence_inputs=_confidence(cutoff),
            financial_inflection_candidates=(evidence,),
        )

    before = score_at(datetime(2027, 5, 31, 12, tzinfo=UTC))
    restated = score_at(datetime(2027, 6, 1, 12, tzinfo=UTC))
    historical = score_at(datetime(2027, 5, 31, 12, tzinfo=UTC))

    assert before.created and restated.created and not historical.created
    assert historical.record.id == before.record.id
    assert before.record.snapshot_fingerprint_sha256 != (
        restated.record.snapshot_fingerprint_sha256
    )
    assert before.record.components[0].score == Decimal("70")
    assert restated.record.components[0].score == Decimal("80")
    assert before.record.components[0].score == historical.record.components[0].score
    before_components = cast(
        list[dict[str, object]], before.record.input_manifest_json["components"]
    )
    restated_components = cast(
        list[dict[str, object]], restated.record.input_manifest_json["components"]
    )
    before_facts = cast(list[dict[str, object]], before_components[0]["facts"])
    restated_facts = cast(list[dict[str, object]], restated_components[0]["facts"])
    assert before_facts and restated_facts and before_facts != restated_facts
    explanation = restated.record.components[0].explanations[0]
    assert explanation.evidence_manifest_json["facts"]
    facts = cast(list[dict[str, object]], explanation.evidence_manifest_json["facts"])
    fact = facts[0]
    assert all(item["metric_code"] == "revenue" for item in facts)
    assert fact["financial_fact_id"]
    assert fact["source_record_id"]
    assert fact["external_record_id"]
    assert fact["raw_object_key"]
    assert fact["raw_payload_reference"]
    history = ScoreSnapshotRepository(session).list_company_score_snapshots(company_id)
    assert [item.id for item in history] == [restated.record.id, before.record.id]
