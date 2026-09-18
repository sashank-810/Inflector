"""Phase 4A typed scoring policy and pure evaluator tests."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from inflector_core.scoring_policy import (
    ConfidenceEvaluator,
    ConfidenceInputs,
    EligibilityEvaluator,
    EligibilityInputs,
    FinancialContextPolicy,
    FinancialContextPolicyResolver,
    InflectionScoringPolicy,
    canonical_policy_json,
    policy_to_canonical_mapping,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)

POLICY_PATH = Path(__file__).parent / "fixtures" / "inflection_model_v1_development.json"


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _mapping() -> dict[str, object]:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _policy() -> InflectionScoringPolicy:
    return scoring_policy_from_mapping(_mapping())


def _eligibility_inputs() -> EligibilityInputs:
    return EligibilityInputs(
        security_type="equity",
        security_status="active",
        listing_status="active",
        comparable_yoy_observations=4,
        financial_core_available=3,
        financial_core_required=4,
        average_daily_traded_value_inr=Decimal("1000000"),
        has_critical_data_quality_issue=False,
        is_new_listing=False,
    )


def _confidence_inputs() -> ConfidenceInputs:
    cutoff = _at(2027, 1, 1)
    return ConfidenceInputs(
        required_feature_count=4,
        available_feature_count=4,
        source_reliability=Decimal("1"),
        freshest_required_evidence_at=cutoff,
        knowledge_cutoff=cutoff,
        comparable_history_observations=8,
        evidence_confidence=Decimal("1"),
    )


def test_policy_checksum_is_canonical_and_round_trips() -> None:
    mapping = _mapping()
    reordered = dict(reversed(tuple(mapping.items())))
    policy = scoring_policy_from_mapping(mapping)
    reordered_policy = scoring_policy_from_mapping(reordered)
    round_tripped = scoring_policy_from_mapping(policy_to_canonical_mapping(policy))

    assert scoring_policy_checksum(policy) == scoring_policy_checksum(reordered_policy)
    assert scoring_policy_checksum(policy) == scoring_policy_checksum(round_tripped)
    assert len(scoring_policy_checksum(policy)) == 64
    assert canonical_policy_json(policy) == canonical_policy_json(round_tripped)

    equivalent = deepcopy(mapping)
    equivalent["financial_inflection"]["persistence_threshold"] = "0.1"  # type: ignore[index]
    changed = deepcopy(mapping)
    changed["financial_inflection"]["persistence_threshold"] = "0.11"  # type: ignore[index]
    assert scoring_policy_checksum(policy) == scoring_policy_checksum(
        scoring_policy_from_mapping(equivalent)
    )
    assert scoring_policy_checksum(policy) != scoring_policy_checksum(
        scoring_policy_from_mapping(changed)
    )
    assert '"persistence_threshold":"0.1"' in canonical_policy_json(policy)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("financial_context", "provider_dataset_priority", []),
        (
            "financial_context",
            "provider_dataset_priority",
            [
                "11111111-1111-4111-8111-111111111111",
                "11111111-1111-4111-8111-111111111111",
            ],
        ),
        ("financial_context", "filing_scope_priority", []),
        ("financial_context", "filing_scope_priority", ["standalone", "standalone"]),
        ("financial_context", "filing_scope_priority", ["group"]),
        ("eligibility", "minimum_comparable_yoy_observations", 0),
        ("eligibility", "minimum_financial_core_coverage", "1.01"),
        ("eligibility", "minimum_financial_core_coverage", "-0.01"),
        ("eligibility", "minimum_average_daily_traded_value_inr", "-1"),
        ("confidence", "stale_after_days", 0),
        ("confidence", "maximum_history_observations", 0),
        ("financial_inflection", "consistency_window_size", 0),
        ("financial_inflection", "persistence_window_size", 0),
    ],
)
def test_policy_validation_rejects_invalid_contract_values(
    section: str,
    field: str,
    value: object,
) -> None:
    mapping = _mapping()
    section_mapping = mapping[section]
    assert isinstance(section_mapping, dict)
    section_mapping[field] = value

    with pytest.raises(ValidationError):
        scoring_policy_from_mapping(mapping)


def test_policy_validation_rejects_invalid_weight_vectors() -> None:
    for section, field, value in (
        ("confidence", "completeness_weight", "-0.1"),
        ("confidence", "completeness_weight", "0.20"),
        ("component_weights", "financial_inflection", "-0.1"),
        ("component_weights", "financial_inflection", "0.20"),
    ):
        mapping = _mapping()
        section_mapping = mapping[section]
        assert isinstance(section_mapping, dict)
        section_mapping[field] = value
        with pytest.raises(ValidationError):
            scoring_policy_from_mapping(mapping)


def test_policy_allows_an_explicit_negative_persistence_threshold() -> None:
    mapping = _mapping()
    section = mapping["financial_inflection"]
    assert isinstance(section, dict)
    section["persistence_threshold"] = "-0.2"

    policy = scoring_policy_from_mapping(mapping)

    assert policy.financial_inflection.persistence_threshold == Decimal("-0.2")


@pytest.mark.parametrize(
    ("candidates", "expected_provider", "expected_scope", "reason", "fallback"),
    [
        (
            "all",
            0,
            "consolidated",
            "preferred_provider_preferred_scope",
            False,
        ),
        (
            "p1_standalone_p2_consolidated",
            0,
            "standalone",
            "preferred_provider_scope_fallback",
            True,
        ),
        (
            "p2_both",
            1,
            "consolidated",
            "provider_fallback_preferred_scope",
            True,
        ),
        (
            "p2_standalone",
            1,
            "standalone",
            "provider_and_scope_fallback",
            True,
        ),
    ],
)
def test_context_resolver_uses_provider_first_lexicographic_priority(
    candidates: str,
    expected_provider: int,
    expected_scope: str,
    reason: str,
    fallback: bool,
) -> None:
    policy = _policy().financial_context
    p1, p2 = policy.provider_dataset_priority
    choices = {
        "all": [(p1, "consolidated"), (p1, "standalone"), (p2, "consolidated"), (p2, "standalone")],
        "p1_standalone_p2_consolidated": [(p1, "standalone"), (p2, "consolidated")],
        "p2_both": [(p2, "consolidated"), (p2, "standalone")],
        "p2_standalone": [(p2, "standalone")],
    }

    result = FinancialContextPolicyResolver().resolve(policy, choices[candidates])

    assert result is not None
    assert result.provider_dataset_id == policy.provider_dataset_priority[expected_provider]
    assert result.filing_scope == expected_scope
    assert result.selection_reason == reason and result.fallback_used is fallback


def test_context_resolver_ignores_unconfigured_and_unsupported_candidates() -> None:
    policy = _policy().financial_context
    result = FinancialContextPolicyResolver().resolve(
        policy,
        [(uuid4(), "consolidated"), (policy.provider_dataset_priority[0], "group")],
    )

    assert result is None


def test_eligibility_fully_eligible_and_exact_boundaries_pass() -> None:
    result = EligibilityEvaluator().evaluate(_eligibility_inputs(), _policy().eligibility)

    assert result.eligible
    assert result.reasons == () and result.warnings == ()
    assert result.financial_core_coverage == Decimal("0.75")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"security_type": "fund"}, "unsupported_security_type"),
        ({"security_status": "inactive"}, "inactive_security"),
        ({"listing_status": "delisted"}, "inactive_listing"),
        ({"has_critical_data_quality_issue": True}, "critical_data_quality_issue"),
        ({"comparable_yoy_observations": 3}, "insufficient_comparable_history"),
        ({"financial_core_available": 2}, "insufficient_financial_core_coverage"),
        ({"average_daily_traded_value_inr": Decimal("999999")}, "below_liquidity_floor"),
        ({"average_daily_traded_value_inr": None}, "missing_liquidity_evidence"),
    ],
)
def test_eligibility_retains_each_hard_failure(
    changes: dict[str, object],
    reason: str,
) -> None:
    result = EligibilityEvaluator().evaluate(
        replace(_eligibility_inputs(), **changes),
        _policy().eligibility,
    )

    assert not result.eligible and result.reasons == (reason,)


def test_eligibility_new_listing_exception_warns_without_history_failure() -> None:
    inputs = replace(
        _eligibility_inputs(),
        comparable_yoy_observations=0,
        is_new_listing=True,
    )
    enabled = EligibilityEvaluator().evaluate(inputs, _policy().eligibility)
    disabled_policy = _policy().eligibility.model_copy(
        update={"new_listing_exception_enabled": False}
    )
    disabled = EligibilityEvaluator().evaluate(inputs, disabled_policy)

    assert enabled.eligible and enabled.reasons == ()
    assert enabled.warnings == ("new_listing_history_exception",)
    assert disabled.reasons == ("insufficient_comparable_history",)


def test_eligibility_liquidity_can_be_disabled_and_reasons_are_deterministic() -> None:
    no_floor = _policy().eligibility.model_copy(
        update={"minimum_average_daily_traded_value_inr": None}
    )
    missing_liquidity = replace(
        _eligibility_inputs(), average_daily_traded_value_inr=None
    )
    result = EligibilityEvaluator().evaluate(missing_liquidity, no_floor)
    multiple = EligibilityEvaluator().evaluate(
        EligibilityInputs(
            security_type="fund",
            security_status="inactive",
            listing_status="delisted",
            comparable_yoy_observations=0,
            financial_core_available=0,
            financial_core_required=4,
            average_daily_traded_value_inr=None,
            has_critical_data_quality_issue=True,
            is_new_listing=False,
        ),
        _policy().eligibility,
    )

    assert result.eligible
    assert multiple.reasons == (
        "unsupported_security_type",
        "inactive_security",
        "inactive_listing",
        "critical_data_quality_issue",
        "insufficient_comparable_history",
        "insufficient_financial_core_coverage",
        "missing_liquidity_evidence",
    )


@pytest.mark.parametrize(
    ("available", "required"),
    [(-1, 4), (5, 4), (0, 0)],
)
def test_eligibility_rejects_structurally_invalid_core_counts(
    available: int,
    required: int,
) -> None:
    with pytest.raises(ValueError):
        EligibilityEvaluator().evaluate(
            replace(
                _eligibility_inputs(),
                financial_core_available=available,
                financial_core_required=required,
            ),
            _policy().eligibility,
        )


def test_confidence_perfect_and_partial_completeness_are_exact() -> None:
    evaluator = ConfidenceEvaluator()
    perfect = evaluator.evaluate(_confidence_inputs(), _policy().confidence)
    partial = evaluator.evaluate(
        replace(_confidence_inputs(), available_feature_count=2),
        _policy().confidence,
    )

    assert perfect.confidence == Decimal("1")
    assert partial.completeness == Decimal("0.5")
    assert partial.confidence == Decimal("0.875")


def test_confidence_zero_completeness_and_evidence_boundaries_are_exact() -> None:
    evaluator = ConfidenceEvaluator()
    zero = evaluator.evaluate(
        replace(
            _confidence_inputs(),
            available_feature_count=0,
            evidence_confidence=Decimal("0"),
        ),
        _policy().confidence,
    )
    one = evaluator.evaluate(
        replace(_confidence_inputs(), evidence_confidence=Decimal("1")),
        _policy().confidence,
    )

    assert zero.completeness == Decimal("0") and zero.evidence == Decimal("0")
    assert one.evidence == Decimal("1")


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), Decimal("1")),
        (timedelta(days=90), Decimal("0.5")),
        (timedelta(days=180), Decimal("0")),
        (timedelta(days=181), Decimal("0")),
    ],
)
def test_confidence_recency_uses_exact_linear_elapsed_time(
    age: timedelta,
    expected: Decimal,
) -> None:
    inputs = _confidence_inputs()
    result = ConfidenceEvaluator().evaluate(
        replace(inputs, freshest_required_evidence_at=inputs.knowledge_cutoff - age),
        _policy().confidence,
    )

    assert result.recency == expected


def test_confidence_missing_evidence_is_zero_with_warnings_not_ineligibility() -> None:
    confidence = ConfidenceEvaluator().evaluate(
        replace(
            _confidence_inputs(),
            freshest_required_evidence_at=None,
            evidence_confidence=None,
        ),
        _policy().confidence,
    )
    eligibility = EligibilityEvaluator().evaluate(_eligibility_inputs(), _policy().eligibility)

    assert confidence.recency == Decimal("0") and confidence.evidence == Decimal("0")
    assert confidence.warnings == (
        "missing_recency_evidence",
        "missing_evidence_confidence",
    )
    assert confidence.confidence == Decimal("0.65")
    assert eligibility.eligible


@pytest.mark.parametrize(
    ("history_count", "expected"),
    [(0, Decimal("0")), (4, Decimal("0.5")), (12, Decimal("1"))],
)
def test_confidence_history_is_exact_and_capped(
    history_count: int,
    expected: Decimal,
) -> None:
    result = ConfidenceEvaluator().evaluate(
        replace(_confidence_inputs(), comparable_history_observations=history_count),
        _policy().confidence,
    )

    assert result.history == expected


@pytest.mark.parametrize("field", ["source_reliability", "evidence_confidence"])
@pytest.mark.parametrize("value", [Decimal("-0.01"), Decimal("1.01")])
def test_confidence_rejects_invalid_unit_interval_inputs(field: str, value: Decimal) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        ConfidenceEvaluator().evaluate(
            replace(_confidence_inputs(), **{field: value}),
            _policy().confidence,
        )


def test_confidence_validates_time_and_normalizes_non_utc_offsets() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    result = ConfidenceEvaluator().evaluate(
        replace(
            _confidence_inputs(),
            knowledge_cutoff=datetime(2027, 1, 1, 17, 30, tzinfo=ist),
            freshest_required_evidence_at=datetime(2027, 1, 1, 17, 30, tzinfo=ist),
        ),
        _policy().confidence,
    )
    assert result.knowledge_cutoff == _at(2027, 1, 1)
    assert result.freshest_required_evidence_at == _at(2027, 1, 1)

    with pytest.raises(ValueError, match="knowledge_cutoff"):
        ConfidenceEvaluator().evaluate(
            replace(_confidence_inputs(), knowledge_cutoff=datetime(2027, 1, 1)),
            _policy().confidence,
        )
    with pytest.raises(ValueError, match="freshest_required_evidence_at"):
        ConfidenceEvaluator().evaluate(
            replace(
                _confidence_inputs(),
                freshest_required_evidence_at=datetime(2027, 1, 1),
            ),
            _policy().confidence,
        )
    with pytest.raises(ValueError, match="after knowledge cutoff"):
        ConfidenceEvaluator().evaluate(
            replace(
                _confidence_inputs(),
                freshest_required_evidence_at=_at(2027, 1, 2),
            ),
            _policy().confidence,
        )


def test_confidence_rejects_invalid_counts_and_accepts_reliability_boundaries() -> None:
    for reliability in (Decimal("0"), Decimal("1")):
        result = ConfidenceEvaluator().evaluate(
            replace(_confidence_inputs(), source_reliability=reliability),
            _policy().confidence,
        )
        assert result.source_reliability == reliability
    for inputs in (
        replace(_confidence_inputs(), required_feature_count=0),
        replace(_confidence_inputs(), available_feature_count=-1),
        replace(_confidence_inputs(), available_feature_count=5),
        replace(_confidence_inputs(), comparable_history_observations=-1),
    ):
        with pytest.raises(ValueError):
            ConfidenceEvaluator().evaluate(inputs, _policy().confidence)


def test_financial_context_policy_rejects_duplicate_provider_ids_directly() -> None:
    provider_id = UUID("11111111-1111-4111-8111-111111111111")
    with pytest.raises(ValidationError):
        FinancialContextPolicy(
            provider_dataset_priority=(provider_id, provider_id),
            filing_scope_priority=("standalone",),
        )
