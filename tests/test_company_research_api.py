"""Phase 5B company context, history, change, and deep-audit regressions."""

from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from inflector_api.company_research_schemas import OpportunityAuditCategory
from inflector_api.main import app
from inflector_core.score_audit import audit_fingerprint_sha256, canonical_audit_value
from inflector_database.models import ScoreSnapshot, Security
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
from test_opportunity_score_api import (
    MODEL_FAMILY,
    _additional_configuration,
    _clone_snapshot,
    _complete_snapshot,
)


@pytest.fixture()
def client(session: Session) -> Generator[TestClient, None, None]:
    def override() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db_session] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _context_path(company_id=COMPANY_ID) -> str:
    return f"/api/v1/companies/{company_id}/research-contexts"


def _history_path(company_id=COMPANY_ID) -> str:
    return f"/api/v1/companies/{company_id}/opportunity-score-history"


def _history_params(snapshot: ScoreSnapshot, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "model_family": MODEL_FAMILY,
        "security_id": str(snapshot.selected_security_id),
        "scoring_configuration_id": str(snapshot.scoring_configuration_id),
    }
    values.update(overrides)
    return values


def _set_component_score(
    session: Session,
    snapshot: ScoreSnapshot,
    *,
    component_code: str,
    score: Decimal,
) -> None:
    component = next(item for item in snapshot.components if item.component_code == component_code)
    if snapshot.final_score is None or component.final_contribution is None:
        raise AssertionError("test helper requires a complete V5 snapshot")
    previous_final_score = snapshot.final_score
    previous_contribution = component.final_contribution
    component.score = score
    new_contribution = score * component.configured_top_level_weight
    component.final_contribution = new_contribution
    snapshot.final_score = previous_final_score + new_contribution - previous_contribution
    payload = deepcopy(snapshot.fingerprint_payload_json)
    state = cast(dict[str, object], payload["final_score_state"])
    state["components"] = canonical_audit_value(
        [
            {
                "component_code": item.component_code,
                "score": item.score,
                "configured_top_level_weight": item.configured_top_level_weight,
                "final_contribution": item.final_contribution,
            }
            for item in snapshot.components
        ]
    )
    state["final_score"] = canonical_audit_value(snapshot.final_score)
    snapshot.fingerprint_payload_json = payload
    snapshot.snapshot_fingerprint_sha256 = audit_fingerprint_sha256(payload)
    session.flush()


def test_context_discovery_preserves_security_and_configuration_contexts(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    alternate = _additional_configuration(session, original)
    config_clone = _clone_snapshot(session, original, configuration=alternate)
    other_security = Security(
        company_id=original.company_id,
        isin="INE000V30002",
        security_type="equity",
        status="inactive",
    )
    session.add(other_security)
    session.flush()
    security_clone = _clone_snapshot(session, original, selected_security_id=other_security.id)

    response = client.get(_context_path(), params={"model_family": MODEL_FAMILY})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert {
        (
            item["security"]["security_id"],
            item["scoring_configuration"]["id"],
        )
        for item in body["items"]
    } == {
        (str(original.selected_security_id), str(original.scoring_configuration_id)),
        (str(config_clone.selected_security_id), str(alternate.id)),
        (str(security_clone.selected_security_id), str(original.scoring_configuration_id)),
    }
    context_order = [
        (
            item["security"]["isin"],
            item["scoring_configuration"]["checksum_sha256"],
            item["scoring_configuration"]["id"],
            item["latest_snapshot"]["snapshot_fingerprint_sha256"],
        )
        for item in body["items"]
    ]
    assert context_order == sorted(context_order)


def test_context_latest_is_semantic_and_never_score_shops(
    session: Session,
    client: TestClient,
) -> None:
    older_final = _complete_snapshot(session)
    newer_partial = _clone_snapshot(
        session,
        older_final,
        knowledge_cutoff=AS_OF + timedelta(days=1),
        partial=True,
    )

    response = client.get(_context_path(), params={"model_family": MODEL_FAMILY})

    assert response.status_code == 200
    latest = response.json()["items"][0]["latest_snapshot"]
    assert latest["snapshot_id"] == str(newer_partial.id)
    assert latest["snapshot_status"] == "partial_component_set"
    assert latest["final_score"] is None


def test_context_empty_company_is_distinct_from_unknown_company(
    session: Session,
    client: TestClient,
) -> None:
    _complete_snapshot(session)

    empty = client.get(_context_path(), params={"model_family": "other_family"})
    unknown = client.get(_context_path(uuid4()), params={"model_family": MODEL_FAMILY})

    assert empty.status_code == 200
    assert empty.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Company not found"}


def test_context_pagination_and_query_validation(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    alternate = _additional_configuration(session, original)
    _clone_snapshot(session, original, configuration=alternate)

    page = client.get(
        _context_path(),
        params={"model_family": MODEL_FAMILY, "limit": 1, "offset": 1},
    )

    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert len(page.json()["items"]) == 1
    for params in ({}, {"model_family": "  "}, {"model_family": MODEL_FAMILY, "limit": 101}):
        assert client.get(_context_path(), params=params).status_code == 422


def test_history_uses_semantic_order_and_fingerprint_tie_break(
    session: Session,
    client: TestClient,
) -> None:
    original = _complete_snapshot(session)
    older_created_later = _clone_snapshot(
        session, original, knowledge_cutoff=AS_OF - timedelta(days=1)
    )
    tie_a = _clone_snapshot(session, original, knowledge_cutoff=AS_OF + timedelta(days=1))
    tie_b = _clone_snapshot(session, original, knowledge_cutoff=AS_OF + timedelta(days=1))
    original.created_at = AS_OF + timedelta(days=100)
    session.flush()

    response = client.get(_history_path(), params=_history_params(original))

    assert response.status_code == 200
    ids = [item["snapshot_id"] for item in response.json()["items"]]
    ties = sorted((tie_a, tie_b), key=lambda item: item.snapshot_fingerprint_sha256)
    assert ids == [
        str(ties[0].id),
        str(ties[1].id),
        str(original.id),
        str(older_created_later.id),
    ]


def test_history_exact_context_and_v5_isolation(
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
    original = _run_v5(
        orchestrator,
        cross_domain_candidates=(financial,),
        catalyst_candidates=(catalyst,),
        market_structure=market,
        attention=_attention_bundle(),
    ).record
    same_context = _clone_snapshot(session, original, knowledge_cutoff=AS_OF + timedelta(days=1))
    alternate = _additional_configuration(session, original)
    _clone_snapshot(session, original, configuration=alternate)
    other_security = Security(
        company_id=original.company_id,
        isin="INE000V30003",
        security_type="equity",
        status="inactive",
    )
    session.add(other_security)
    session.flush()
    _clone_snapshot(session, original, selected_security_id=other_security.id)

    response = client.get(_history_path(), params=_history_params(original))
    missing = client.get(
        _history_path(),
        params=_history_params(original, security_id=str(uuid4())),
    )
    wrong_family = client.get(
        _history_path(),
        params=_history_params(original, model_family="other_family"),
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["snapshot_id"] for item in response.json()["items"]} == {
        str(original.id),
        str(same_context.id),
    }
    assert str(v4.id) not in {item["snapshot_id"] for item in response.json()["items"]}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Opportunity score research context not found"}
    assert wrong_family.status_code == 404


def test_history_exact_decimal_change_reference_and_negative_values(
    session: Session,
    client: TestClient,
) -> None:
    comparison = _complete_snapshot(session)
    current = _clone_snapshot(
        session,
        comparison,
        knowledge_cutoff=AS_OF + timedelta(days=1),
        confidence=comparison.confidence - Decimal("0.05"),
    )
    _set_component_score(
        session,
        current,
        component_code="financial_inflection",
        score=Decimal("72.75"),
    )

    response = client.get(_history_path(), params=_history_params(current))

    assert response.status_code == 200
    change = response.json()["latest_change"]
    assert current.final_score == Decimal("72.175")
    assert change["final_score_delta"] == "1.5"
    assert change["confidence_delta"] == "-0.05"
    assert change["coverage_delta"] == "0"
    assert change["status_changed"] is False


def test_partial_change_and_pagination_independent_latest_comparison(
    session: Session,
    client: TestClient,
) -> None:
    comparison = _complete_snapshot(session)
    current = _clone_snapshot(
        session,
        comparison,
        knowledge_cutoff=AS_OF + timedelta(days=1),
        partial=True,
    )

    response = client.get(_history_path(), params=_history_params(current, limit=1, offset=1))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["snapshot_id"] == str(comparison.id)
    change = body["latest_change"]
    assert change["current_snapshot_id"] == str(current.id)
    assert change["comparison_snapshot_id"] == str(comparison.id)
    assert change["final_score_delta"] is None
    assert change["status_changed"] is True
    assert change["newly_missing_component_codes"] == ["low_market_attention"]
    lma = next(
        item
        for item in change["component_changes"]
        if item["component_code"] == "low_market_attention"
    )
    assert lma["availability_change"] == "removed"
    assert lma["current_score"] is None


def test_zero_component_remains_available_in_change_model(
    session: Session,
    client: TestClient,
) -> None:
    comparison = _complete_snapshot(session)
    _set_component_score(
        session,
        comparison,
        component_code="low_market_attention",
        score=Decimal("10"),
    )
    current = _clone_snapshot(session, comparison, knowledge_cutoff=AS_OF + timedelta(days=1))
    _set_component_score(
        session,
        current,
        component_code="low_market_attention",
        score=Decimal("0"),
    )

    response = client.get(_history_path(), params=_history_params(current))

    lma = next(
        item
        for item in response.json()["latest_change"]["component_changes"]
        if item["component_code"] == "low_market_attention"
    )
    assert lma == {
        "component_code": "low_market_attention",
        "current_score": "0",
        "comparison_score": "10",
        "score_delta": "-10",
        "current_available": True,
        "comparison_available": True,
        "availability_change": "unchanged",
    }


def test_first_history_item_has_no_synthetic_comparison(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)

    response = client.get(_history_path(), params=_history_params(snapshot))

    assert response.status_code == 200
    assert response.json()["latest_change"] is None


def test_audit_summary_reads_exact_persisted_union_and_algorithms(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)
    union = cast(dict[str, object], snapshot.input_manifest_json["union"])

    response = client.get(f"/api/v1/opportunity-scores/{snapshot.id}/audit")

    assert response.status_code == 200
    body = response.json()
    counts = {item["category"]: item["count"] for item in body["categories"]}
    assert list(counts) == [category.value for category in OpportunityAuditCategory]
    assert counts == {
        category.value: len(cast(list[object], union[category.value]))
        for category in OpportunityAuditCategory
    }
    for category in (
        "financial_facts",
        "market_bars",
        "business_events",
        "attention_observations",
        "attention_source_records",
    ):
        assert counts[category] > 0
    assert body["component_algorithm_versions"] == union["component_algorithm_versions"]
    assert body["phase3_algorithm_versions"] == union["phase3_algorithm_versions"]
    assert body["opportunity_score_aggregation_version"] == ("opportunity_score_weighted_sum_v1")


def test_audit_category_pages_persisted_order_without_live_reads(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)
    union = cast(dict[str, object], snapshot.input_manifest_json["union"])
    expected = cast(list[dict[str, object]], union["attention_observations"])

    first = client.get(
        f"/api/v1/opportunity-scores/{snapshot.id}/audit/attention_observations",
        params={"limit": 1, "offset": 0},
    )
    second = client.get(
        f"/api/v1/opportunity-scores/{snapshot.id}/audit/attention_observations",
        params={"limit": 1, "offset": 1},
    )

    assert first.status_code == 200
    assert first.json()["total"] == len(expected)
    assert first.json()["items"] == expected[:1]
    assert second.json()["items"] == expected[1:2]
    assert first.json()["items"] != second.json()["items"]
    assert (
        client.get(f"/api/v1/opportunity-scores/{snapshot.id}/audit/not-a-category").status_code
        == 422
    )


def test_audit_is_historical_when_a_later_snapshot_exists(
    session: Session,
    client: TestClient,
) -> None:
    historical = _complete_snapshot(session)
    original_union = deepcopy(historical.input_manifest_json["union"])
    later = _clone_snapshot(session, historical, knowledge_cutoff=AS_OF + timedelta(days=1))
    later_manifest = deepcopy(later.input_manifest_json)
    later_union = cast(dict[str, object], later_manifest["union"])
    later_union["attention_observations"] = []
    later.input_manifest_json = later_manifest
    session.flush()

    response = client.get(f"/api/v1/opportunity-scores/{historical.id}/audit")

    assert response.status_code == 200
    counts = {item["category"]: item["count"] for item in response.json()["categories"]}
    assert counts["attention_observations"] == len(
        cast(list[object], cast(dict[str, object], original_union)["attention_observations"])
    )


def test_non_v5_audit_is_not_a_phase_5b_resource(
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

    for suffix in ("audit", "audit/financial_facts"):
        response = client.get(f"/api/v1/opportunity-scores/{v4.id}/{suffix}")
        assert response.status_code == 404
        assert response.json() == {"detail": "Opportunity score snapshot not found"}


def test_corrupt_snapshot_fails_all_phase_5b_reads_closed(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)
    snapshot.final_score = Decimal("1")
    session.flush()
    expected = {"detail": "Opportunity score snapshot integrity check failed"}
    requests = (
        client.get(_context_path(), params={"model_family": MODEL_FAMILY}),
        client.get(_history_path(), params=_history_params(snapshot)),
        client.get(f"/api/v1/opportunity-scores/{snapshot.id}/audit"),
        client.get(f"/api/v1/opportunity-scores/{snapshot.id}/audit/financial_facts"),
    )
    assert all(response.status_code == 500 for response in requests)
    assert all(response.json() == expected for response in requests)


def test_malformed_audit_union_fails_closed_without_empty_fallback(
    session: Session,
    client: TestClient,
) -> None:
    snapshot = _complete_snapshot(session)
    manifest = deepcopy(snapshot.input_manifest_json)
    union = cast(dict[str, object], manifest["union"])
    union.pop("financial_facts")
    snapshot.input_manifest_json = manifest
    session.flush()

    response = client.get(f"/api/v1/opportunity-scores/{snapshot.id}/audit")

    assert response.status_code == 500
    assert response.json() == {"detail": "Opportunity score snapshot integrity check failed"}


def test_phase_5b_read_modules_have_no_scoring_provider_or_write_dependencies() -> None:
    root = Path(__file__).parents[1]
    paths = (
        root / "packages/database/inflector_database/opportunity_score_read_repository.py",
        root / "apps/api/inflector_api/opportunity_score_service.py",
        root / "apps/api/inflector_api/company_research_schemas.py",
        root / "apps/api/inflector_api/routers/companies.py",
        root / "apps/api/inflector_api/routers/opportunity_scores.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden = (
        "ScoreSnapshotOrchestrator",
        "ComponentScorer",
        "DataProvider",
        "IngestionService",
        "AttentionFeaturePrimitives",
        "Backtest",
        "Alert",
        ".add(",
        ".delete(",
        ".commit(",
        ".flush(",
    )
    assert all(name not in source for name in forbidden)
