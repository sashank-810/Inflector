"""Phase 5A read-only Opportunity Score API regressions."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from inflector_api.main import app
from inflector_api.opportunity_score_schemas import canonical_decimal_string
from inflector_core.score_audit import canonical_audit_value
from inflector_database.models import (
    ExchangeListing,
    ModelVersion,
    ScoreComponent,
    ScoreSnapshot,
    ScoringConfiguration,
    Security,
)
from inflector_database.score_repository import (
    V5_COMPONENT_ORDER,
    V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
    ScoreComponentWrite,
    ScoreExplanationWrite,
    ScoreSnapshotRepository,
    ScoreSnapshotWrite,
)
from inflector_database.session import get_db_session
from test_opportunity_score_activation import (
    AS_OF,
    COMPANY_ID,
    SECURITY_ID,
    _attention_bundle,
    _confidence,
    _eligibility,
    _run_v5,
    _setup_complete,
)

MODEL_FAMILY = "opportunity_v5"


@pytest.fixture()
def client(session: Session) -> Generator[TestClient, None, None]:
    def override() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db_session] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _complete_snapshot(session: Session) -> ScoreSnapshot:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    return _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    ).record


def _explanation_write(component: ScoreComponent) -> tuple[ScoreExplanationWrite, ...]:
    return tuple(
        ScoreExplanationWrite(
            factor_code=item.factor_code,
            rank=item.rank,
            raw_value=item.raw_value,
            raw_unit=item.raw_unit,
            normalized_score=item.normalized_score,
            configured_weight=item.configured_weight,
            effective_weight=item.effective_weight,
            component_contribution=item.component_contribution,
            input_available_at=_aware(item.input_available_at),
            evidence_type=item.evidence_type,
            template_code=item.template_code,
            direction=item.direction,
            evidence_manifest_json=item.evidence_manifest_json,
        )
        for item in component.explanations
    )


def _component_write(
    component: ScoreComponent,
    *,
    final_contribution: Decimal | None,
) -> ScoreComponentWrite:
    return ScoreComponentWrite(
        component_code=component.component_code,
        score=component.score,
        unit=component.unit,
        configured_top_level_weight=component.configured_top_level_weight,
        subfactor_weight_coverage=component.subfactor_weight_coverage,
        final_contribution=final_contribution,
        available_at=(None if component.available_at is None else _aware(component.available_at)),
        algorithm_version=component.algorithm_version,
        missing_subfactors_json=[str(item) for item in component.missing_subfactors_json],
        warnings_json=[str(item) for item in component.warnings_json],
        detail_json=component.detail_json,
        explanations=_explanation_write(component),
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clone_snapshot(
    session: Session,
    source: ScoreSnapshot,
    *,
    configuration: ScoringConfiguration | None = None,
    knowledge_cutoff: datetime | None = None,
    partial: bool = False,
    confidence: Decimal | None = None,
    selected_security_id: UUID | None = None,
) -> ScoreSnapshot:
    components = tuple(source.components[:-1] if partial else source.components)
    writes = tuple(
        _component_write(
            component,
            final_contribution=(None if partial else component.final_contribution),
        )
        for component in components
    )
    available = [component.component_code for component in components]
    missing = [code for code in V5_COMPONENT_ORDER if code not in set(available)]
    final_score = None if partial else source.final_score
    final_state = {
        "aggregation_version": V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION,
        "required_positive_weight_component_codes": list(V5_COMPONENT_ORDER),
        "components": canonical_audit_value(
            [
                {
                    "component_code": component.component_code,
                    "score": component.score,
                    "configured_top_level_weight": component.configured_top_level_weight,
                    "final_contribution": component.final_contribution,
                }
                for component in writes
            ]
        ),
        "final_score": canonical_audit_value(final_score),
    }
    cutoff = knowledge_cutoff or _aware(source.knowledge_cutoff)
    config = configuration
    payload = {
        "algorithm_version": "score_snapshot_v5",
        "opportunity_score_aggregation_version": (V5_OPPORTUNITY_SCORE_AGGREGATION_VERSION),
        "test_identity": str(uuid4()),
        "knowledge_cutoff": cutoff,
        "final_score_state": final_state,
    }
    return (
        ScoreSnapshotRepository(session)
        .persist_snapshot(
            ScoreSnapshotWrite(
                company_id=source.company_id,
                model_version_id=(
                    source.model_version_id if config is None else config.model_version_id
                ),
                scoring_configuration_id=(
                    source.scoring_configuration_id if config is None else config.id
                ),
                configuration_checksum_sha256=(
                    source.configuration_checksum_sha256
                    if config is None
                    else config.checksum_sha256
                ),
                as_of_date=cutoff.date(),
                knowledge_cutoff=cutoff,
                ending_fiscal_year=source.ending_fiscal_year,
                ending_fiscal_quarter=source.ending_fiscal_quarter,
                selected_provider_dataset_id=source.selected_provider_dataset_id,
                selected_filing_scope=source.selected_filing_scope,
                selected_security_id=(
                    source.selected_security_id
                    if selected_security_id is None
                    else selected_security_id
                ),
                snapshot_status=("partial_component_set" if partial else source.snapshot_status),
                eligibility_eligible=source.eligibility_eligible,
                eligibility_inputs_json=source.eligibility_inputs_json,
                eligibility_reasons_json=[str(item) for item in source.eligibility_reasons_json],
                eligibility_warnings_json=[str(item) for item in source.eligibility_warnings_json],
                financial_core_coverage=source.financial_core_coverage,
                confidence=source.confidence if confidence is None else confidence,
                confidence_inputs_json=source.confidence_inputs_json,
                confidence_details_json=source.confidence_details_json,
                top_level_component_weight_coverage=sum(
                    (item.configured_top_level_weight for item in writes), Decimal("0")
                ),
                available_component_codes_json=available,
                missing_component_codes_json=missing,
                context_resolution_json=source.context_resolution_json,
                input_manifest_json=source.input_manifest_json,
                fingerprint_payload_json=cast(dict[str, object], canonical_audit_value(payload)),
                final_score=final_score,
                algorithm_version="score_snapshot_v5",
                components=writes,
            )
        )
        .record
    )


def _additional_configuration(
    session: Session,
    source: ScoreSnapshot,
    *,
    checksum: str = "c" * 64,
) -> ScoringConfiguration:
    value = ScoringConfiguration(
        model_version_id=source.model_version_id,
        configuration_name="phase_5a_alternate",
        configuration_version=str(uuid4()),
        status="active",
        effective_from=AS_OF - timedelta(days=365),
        effective_to=None,
        configuration_json={"synthetic": True},
        checksum_sha256=checksum,
    )
    session.add(value)
    session.flush()
    return value


def test_canonical_decimal_string_never_uses_float_or_exponent() -> None:
    assert [
        canonical_decimal_string(value)
        for value in (
            Decimal("0"),
            Decimal("1.00"),
            Decimal("70.675"),
            Decimal("13.60"),
            Decimal("0.9500"),
        )
    ] == ["0", "1", "70.675", "13.6", "0.95"]


def test_queue_returns_complete_v5_with_exact_decimal_strings(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)
    session.add_all(
        [
            ExchangeListing(
                security_id=snapshot.selected_security_id,
                exchange="NSE",
                symbol="FICTIONAL",
                valid_from=AS_OF.date() - timedelta(days=10),
                valid_to=None,
                status="inactive",
            ),
            ExchangeListing(
                security_id=snapshot.selected_security_id,
                exchange="BSE",
                symbol="999999",
                valid_from=AS_OF.date() - timedelta(days=20),
                valid_to=None,
                status="active",
            ),
        ]
    )
    session.flush()

    response = client.get("/api/v1/opportunity-scores", params={"model_family": MODEL_FAMILY})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["snapshot_id"] == str(snapshot.id)
    assert item["algorithm_version"] == "score_snapshot_v5"
    assert item["snapshot_status"] == "final_score_available"
    assert item["final_score"] == "70.675"
    assert item["top_level_component_weight_coverage"] == "1"
    assert [listing["exchange"] for listing in item["security"]["listings"]] == [
        "BSE",
        "NSE",
    ]
    assert [component["component_code"] for component in item["components"]] == list(
        V5_COMPONENT_ORDER
    )
    assert [component["final_contribution"] for component in item["components"]] == [
        "16.6875",
        "13.6",
        "10.425",
        "7.3875",
        "6.7",
        "7.95",
        "4.05",
        "3.875",
    ]
    assert "detail" not in item["components"][0]


def test_detail_exposes_persisted_audit_without_internal_snapshot_blobs(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)

    response = client.get(f"/api/v1/opportunity-scores/{snapshot.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["final_score"] == "70.675"
    assert body["model_version"]["model_family"] == MODEL_FAMILY
    assert body["scoring_configuration"]["checksum_sha256"] == (
        snapshot.configuration_checksum_sha256
    )
    assert body["eligibility"]["eligible"] is True
    assert body["confidence_details"] == snapshot.confidence_details_json
    assert body["context_resolution"] == snapshot.context_resolution_json
    assert [component["component_code"] for component in body["components"]] == list(
        V5_COMPONENT_ORDER
    )
    catalyst = body["components"][1]
    attention = body["components"][-1]
    assert [item["template_code"] for item in catalyst["explanations"]] == [
        "business_catalyst_selected_event_v1"
    ]
    assert [item["factor_code"] for item in attention["explanations"]] == [
        "news_mentions_count",
        "analyst_coverage_count",
    ]
    assert {item["template_code"] for item in attention["explanations"]} == {
        "low_market_attention_subfactor_v1"
    }
    assert any(item["evidence_manifest"] for item in attention["explanations"])
    assert "input_manifest_json" not in body
    assert "fingerprint_payload_json" not in body


def test_latest_selection_never_prefers_an_older_final_score(
    session: Session,
    client: TestClient,
) -> None:
    older = _complete_snapshot(session)
    newer = _clone_snapshot(
        session,
        older,
        knowledge_cutoff=AS_OF + timedelta(days=1),
        partial=True,
    )

    response = client.get(
        "/api/v1/opportunity-scores",
        params={
            "model_family": MODEL_FAMILY,
            "sort": "opportunity_score_desc",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["snapshot_id"] == str(newer.id)
    assert body["items"][0]["snapshot_status"] == "partial_component_set"
    assert body["items"][0]["final_score"] is None
    assert body["items"][0]["top_level_component_weight_coverage"] == "0.95"
    assert body["items"][0]["missing_component_codes"] == ["low_market_attention"]
    assert all(
        component["final_contribution"] is None for component in body["items"][0]["components"]
    )


def test_configuration_filter_preserves_independent_contexts(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    alternate = _additional_configuration(session, original)
    clone = _clone_snapshot(session, original, configuration=alternate)

    all_response = client.get("/api/v1/opportunity-scores", params={"model_family": MODEL_FAMILY})
    filtered_response = client.get(
        "/api/v1/opportunity-scores",
        params={
            "model_family": MODEL_FAMILY,
            "configuration_checksum_sha256": alternate.checksum_sha256.upper(),
        },
    )

    assert all_response.status_code == 200
    assert all_response.json()["total"] == 2
    assert filtered_response.status_code == 200
    assert filtered_response.json()["total"] == 1
    assert filtered_response.json()["items"][0]["snapshot_id"] == str(clone.id)


def test_status_sort_and_pagination_apply_after_latest_selection(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    alternate = _additional_configuration(session, original)
    _clone_snapshot(
        session,
        original,
        configuration=alternate,
        partial=True,
        confidence=Decimal("0.99"),
    )

    final_response = client.get(
        "/api/v1/opportunity-scores",
        params={"model_family": MODEL_FAMILY, "status": "final_score_available"},
    )
    partial_response = client.get(
        "/api/v1/opportunity-scores",
        params={"model_family": MODEL_FAMILY, "status": "partial_component_set"},
    )
    paged_response = client.get(
        "/api/v1/opportunity-scores",
        params={
            "model_family": MODEL_FAMILY,
            "sort": "confidence_desc",
            "limit": 1,
            "offset": 0,
        },
    )
    second_page = client.get(
        "/api/v1/opportunity-scores",
        params={
            "model_family": MODEL_FAMILY,
            "sort": "confidence_desc",
            "limit": 1,
            "offset": 1,
        },
    )

    assert final_response.json()["total"] == 1
    assert partial_response.json()["total"] == 1
    assert paged_response.json()["total"] == 2
    assert len(paged_response.json()["items"]) == 1
    assert paged_response.json()["items"][0]["snapshot_status"] == ("partial_component_set")
    assert second_page.json()["total"] == 2
    assert (
        second_page.json()["items"][0]["snapshot_id"]
        != (paged_response.json()["items"][0]["snapshot_id"])
    )


def test_all_queue_sorts_are_persisted_field_sorts_after_context_selection(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    partial_configuration = _additional_configuration(session, original)
    partial = _clone_snapshot(
        session,
        original,
        configuration=partial_configuration,
        partial=True,
        confidence=Decimal("0.99"),
    )
    latest_configuration = _additional_configuration(session, original, checksum="f" * 64)
    latest = _clone_snapshot(
        session,
        original,
        configuration=latest_configuration,
        knowledge_cutoff=AS_OF + timedelta(days=2),
    )

    expected_exact_first = {
        "knowledge_cutoff_desc": str(latest.id),
        "opportunity_score_desc": None,
        "confidence_desc": str(partial.id),
        "coverage_desc": None,
        "company_name_asc": str(latest.id),
    }
    for sort, exact_id in expected_exact_first.items():
        response = client.get(
            "/api/v1/opportunity-scores",
            params={"model_family": MODEL_FAMILY, "sort": sort},
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 3
        if exact_id is not None:
            assert items[0]["snapshot_id"] == exact_id
        else:
            assert items[0]["snapshot_status"] == "final_score_available"
            assert items[-1]["snapshot_status"] == "partial_component_set"


def test_security_contexts_remain_separate_without_score_shopping(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    other_security = Security(
        company_id=original.company_id,
        isin="INE000V30002",
        security_type="equity",
        status="active",
    )
    session.add(other_security)
    session.flush()
    clone = _clone_snapshot(
        session,
        original,
        selected_security_id=other_security.id,
    )

    response = client.get("/api/v1/opportunity-scores", params={"model_family": MODEL_FAMILY})

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["snapshot_id"] for item in response.json()["items"]} == {
        str(original.id),
        str(clone.id),
    }


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"model_family": "   "},
        {"model_family": MODEL_FAMILY, "configuration_checksum_sha256": "bad"},
        {"model_family": MODEL_FAMILY, "status": "unknown"},
        {"model_family": MODEL_FAMILY, "sort": "ranking"},
        {"model_family": MODEL_FAMILY, "limit": 101},
        {"model_family": MODEL_FAMILY, "offset": -1},
    ],
)
def test_queue_query_validation_uses_standard_422(
    client: TestClient,
    params: dict[str, object],
) -> None:
    assert client.get("/api/v1/opportunity-scores", params=params).status_code == 422


def test_unknown_and_non_v5_snapshot_ids_are_not_resources(
    session: Session,
    client: TestClient,
) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    v4 = orchestrator.orchestrate_business_catalyst_cross_domain_and_persist(
        model_family=MODEL_FAMILY,
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

    for snapshot_id in (v4.id, uuid4()):
        response = client.get(f"/api/v1/opportunity-scores/{snapshot_id}")
        assert response.status_code == 404
        assert response.json() == {"detail": "Opportunity score snapshot not found"}


def test_integrity_failure_fails_closed_for_queue_and_detail(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)
    snapshot.final_score = Decimal("70")
    session.flush()

    queue = client.get("/api/v1/opportunity-scores", params={"model_family": MODEL_FAMILY})
    detail = client.get(f"/api/v1/opportunity-scores/{snapshot.id}")

    assert queue.status_code == 500
    assert detail.status_code == 500
    assert queue.json() == {"detail": "Opportunity score snapshot integrity check failed"}
    assert detail.json() == queue.json()


def test_zero_score_is_serialized_as_zero_not_null(
    session: Session,
    client: TestClient,
) -> None:
    orchestrator, financial, catalyst, market = _setup_complete(session)
    snapshot = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(news_count=80, analyst_count=30),
    ).record

    response = client.get(f"/api/v1/opportunity-scores/{snapshot.id}")

    assert response.status_code == 200
    body = response.json()
    attention = body["components"][-1]
    assert attention["score"] == "0"
    assert attention["final_contribution"] == "0"
    assert body["final_score"] == "66.8"


def test_new_read_modules_have_no_scoring_or_provider_dependencies() -> None:
    root = Path(__file__).parents[1]
    paths = (
        root / "packages/database/inflector_database/opportunity_score_read_repository.py",
        root / "apps/api/inflector_api/opportunity_score_service.py",
        root / "apps/api/inflector_api/routers/opportunity_scores.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "ScoreSnapshotOrchestrator",
        "ComponentScorer",
        "AttentionFeaturePrimitives",
        "DataProvider",
        "IngestionService",
    )
    assert all(name not in source for name in forbidden)


def test_model_family_filter_excludes_other_model_versions(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    other_model = ModelVersion(
        model_family="other_family",
        semantic_version="1.0.0",
        git_sha="d" * 40,
        status="active",
    )
    session.add(other_model)
    session.flush()
    other_config = ScoringConfiguration(
        model_version_id=other_model.id,
        configuration_name="other",
        configuration_version="1",
        status="active",
        effective_from=AS_OF - timedelta(days=1),
        effective_to=None,
        configuration_json={"synthetic": True},
        checksum_sha256="e" * 64,
    )
    session.add(other_config)
    session.flush()
    _clone_snapshot(session, original, configuration=other_config)

    response = client.get("/api/v1/opportunity-scores", params={"model_family": MODEL_FAMILY})

    assert response.status_code == 200
    assert response.json()["total"] == 1
