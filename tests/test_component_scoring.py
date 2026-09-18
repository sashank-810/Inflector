"""Phase 4B financial-inflection component scoring acceptance tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from inflector_core.component_scoring import (
    FinancialInflectionComponentScorer,
    FinancialInflectionEvidence,
    RoceImprovementEvidence,
    score_piecewise_linear,
)
from inflector_core.scoring_policy import (
    ComponentWeights,
    FinancialContextSelection,
    InflectionScoringPolicy,
    PiecewiseLinearScoringCurve,
    ScoreBreakpoint,
    canonical_policy_json,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_data.capital_features import ReturnOnCapitalEmployedValue
from inflector_data.financial_features import (
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
from inflector_data.ttm import TrailingTwelveMonthValue
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
PHASE_4A_POLICY = FIXTURES / "inflection_model_v1_development.json"
PHASE_4B_POLICY = FIXTURES / "inflection_model_v1_scoring_development.json"
PHASE_4A_CHECKSUM = "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf"

PROVIDER_ID = UUID("11111111-1111-4111-8111-111111111111")
COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
AS_OF = datetime(2027, 8, 1, 12, tzinfo=UTC)


def _mapping(path: Path = PHASE_4B_POLICY) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _policy() -> InflectionScoringPolicy:
    return scoring_policy_from_mapping(_mapping())


def _context(
    provider_dataset_id: UUID = PROVIDER_ID,
    filing_scope: str = "consolidated",
) -> FinancialContextSelection:
    return FinancialContextSelection(
        provider_dataset_id=provider_dataset_id,
        filing_scope=filing_scope,
        provider_priority_index=0,
        scope_priority_index=0,
        fallback_used=False,
        selection_reason="preferred_provider_preferred_scope",
    )


def _acceleration(
    metric_code: str,
    acceleration: str,
    *,
    available_at: datetime = AS_OF - timedelta(days=1),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> GrowthAccelerationValue:
    growth = cast(QuarterGrowthValue, object())
    return GrowthAccelerationValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        metric_code=metric_code,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        current_yoy=growth,
        prior_yoys=(growth, growth, growth),
        prior_median=Decimal("0.1"),
        acceleration=Decimal(acceleration),
        operation="current_yoy_minus_prior3_median",
        as_of=as_of,
        available_at=available_at,
        algorithm_version="growth_acceleration_v1",
    )


def _margin(
    basis_points: str = "500",
    *,
    margin_code: str = "operating_margin",
    available_at: datetime = AS_OF - timedelta(days=2),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> MarginExpansionValue:
    margin = cast(QuarterMarginValue, object())
    change = Decimal(basis_points) / Decimal("10000")
    return MarginExpansionValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        margin_code=margin_code,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        current_margin=margin,
        prior_year_margin=margin,
        change=change,
        basis_points=Decimal(basis_points),
        operation="current_margin_minus_prior_year_margin",
        as_of=as_of,
        available_at=available_at,
        algorithm_version="margin_expansion_v1",
    )


def _roce(
    value: str | None,
    fiscal_year: int,
    *,
    fiscal_quarter: int = 2,
    available_at: datetime = AS_OF - timedelta(days=3),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    as_of: datetime = AS_OF,
) -> ReturnOnCapitalEmployedValue:
    return ReturnOnCapitalEmployedValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=Decimal(value) if value is not None else None,
        unit="ratio",
        ttm_ebit=cast(TrailingTwelveMonthValue, object()),
        beginning_snapshot=cast(InstantFinancialSnapshot, object()),
        ending_snapshot=cast(InstantFinancialSnapshot, object()),
        beginning_capital_employed=Decimal("100"),
        ending_capital_employed=Decimal("120"),
        average_capital_employed=Decimal("110"),
        warnings=() if value is not None else ("non_positive_capital_employed",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="roce_v1",
    )


def _consistency(
    share: str = "0.75",
    *,
    metric_code: str = "revenue",
    window_size: int = 4,
    available_at: datetime = AS_OF - timedelta(days=4),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> GrowthConsistencyValue:
    positive = int(Decimal(share) * Decimal(window_size))
    return GrowthConsistencyValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        metric_code=metric_code,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        window_size=window_size,
        positive_count=positive,
        non_positive_count=window_size - positive,
        positive_share=Decimal(share),
        unit="ratio",
        window=cast(ComparableGrowthWindow, object()),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="growth_consistency_v1",
    )


def _persistence(
    streak_count: int = 3,
    *,
    metric_code: str = "revenue",
    window_size: int = 4,
    threshold: str = "0.1",
    saturated: bool = False,
    available_at: datetime = AS_OF - timedelta(days=5),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> GrowthPersistenceValue:
    return GrowthPersistenceValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        metric_code=metric_code,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        window_size=window_size,
        threshold=Decimal(threshold),
        streak_count=streak_count,
        window_saturated=saturated,
        streak_observations=(),
        stopping_observation=None,
        window=cast(ComparableGrowthWindow, object()),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="growth_persistence_v1",
    )


def _full_evidence(**changes: object) -> FinancialInflectionEvidence:
    values: dict[str, object] = {
        "company_id": COMPANY_ID,
        "context": _context(),
        "fiscal_year": 2027,
        "fiscal_quarter": 2,
        "as_of": AS_OF,
        "revenue_acceleration": _acceleration("revenue", "0.2"),
        "pat_acceleration": _acceleration("pat", "0.1"),
        "margin_expansion": _margin(),
        "current_roce": _roce("0.20", 2027),
        "prior_year_roce": _roce("0.15", 2026),
        "growth_consistency": _consistency(),
        "growth_persistence": _persistence(),
    }
    values.update(changes)
    return FinancialInflectionEvidence(**values)  # type: ignore[arg-type]


def _curve(*pairs: tuple[str, str]) -> PiecewiseLinearScoringCurve:
    return PiecewiseLinearScoringCurve(
        breakpoints=tuple(
            ScoreBreakpoint(raw_value=Decimal(raw), score=Decimal(score))
            for raw, score in pairs
        )
    )


def test_phase4a_policy_checksum_and_canonical_shape_remain_unchanged() -> None:
    old_policy = scoring_policy_from_mapping(_mapping(PHASE_4A_POLICY))

    assert old_policy.financial_inflection.scoring is None
    assert "scoring" not in canonical_policy_json(old_policy)
    assert scoring_policy_checksum(old_policy) == PHASE_4A_CHECKSUM


def test_phase4a_persisted_configuration_still_round_trips(session) -> None:
    policy = scoring_policy_from_mapping(_mapping(PHASE_4A_POLICY))
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="phase4b_compatibility",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="draft",
    )
    created = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="legacy_phase4a",
        configuration_version="1",
        status="draft",
        policy=policy,
    )
    session.flush()
    session.expire_all()

    loaded = repository.get_scoring_configuration(created.record.id)
    assert loaded is not None and loaded.policy == policy
    assert loaded.record.checksum_sha256 == PHASE_4A_CHECKSUM


def test_phase4b_policy_round_trip_checksum_and_mapping_order_are_stable() -> None:
    mapping = _mapping()
    policy = scoring_policy_from_mapping(mapping)
    reversed_mapping = dict(reversed(tuple(mapping.items())))

    assert policy.financial_inflection.scoring is not None
    assert scoring_policy_from_mapping(json.loads(canonical_policy_json(policy))) == policy
    assert scoring_policy_checksum(scoring_policy_from_mapping(reversed_mapping)) == (
        scoring_policy_checksum(policy)
    )

    changed = _mapping()
    scoring = changed["financial_inflection"]["scoring"]  # type: ignore[index]
    scoring["revenue_acceleration_curve"]["breakpoints"][1]["score"] = "41"  # type: ignore[index]
    assert scoring_policy_checksum(scoring_policy_from_mapping(changed)) != (
        scoring_policy_checksum(policy)
    )


def test_piecewise_curve_interpolates_and_clamps_exact_decimals() -> None:
    curve = _curve(("0", "20"), ("10", "40"), ("20", "80"))

    assert score_piecewise_linear(Decimal("0"), curve) == Decimal("20")
    assert score_piecewise_linear(Decimal("10"), curve) == Decimal("40")
    assert score_piecewise_linear(Decimal("20"), curve) == Decimal("80")
    assert score_piecewise_linear(Decimal("5"), curve) == Decimal("30")
    assert score_piecewise_linear(Decimal("-100"), curve) == Decimal("20")
    assert score_piecewise_linear(Decimal("100"), curve) == Decimal("80")
    assert score_piecewise_linear(Decimal("1.23456789"), curve) == Decimal("22.46913578")
    with pytest.raises(TypeError, match="Decimal"):
        score_piecewise_linear(cast(Decimal, 5.0), curve)


@pytest.mark.parametrize(
    "breakpoints",
    [
        (("0", "0"),),
        (("0", "0"), ("0", "50")),
        (("1", "0"), ("0", "50")),
        (("0", "50"), ("1", "40")),
        (("0", "-1"), ("1", "50")),
        (("0", "0"), ("1", "101")),
    ],
)
def test_piecewise_curve_rejects_invalid_breakpoints(
    breakpoints: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValidationError):
        _curve(*breakpoints)


def test_piecewise_curve_rejects_binary_float_configuration() -> None:
    with pytest.raises(ValidationError, match="binary floats"):
        ScoreBreakpoint(raw_value=0.1, score=50)  # type: ignore[arg-type]


def test_financial_inflection_policy_validates_scoring_fields() -> None:
    cases = (
        ("minimum_weight_coverage", "0"),
        ("minimum_weight_coverage", "1.1"),
        ("margin_code", "net_margin"),
        ("growth_history_metric_code", ""),
    )
    for field, value in cases:
        mapping = _mapping()
        scoring = mapping["financial_inflection"]["scoring"]  # type: ignore[index]
        scoring[field] = value  # type: ignore[index]
        with pytest.raises(ValidationError):
            scoring_policy_from_mapping(mapping)


def test_financial_inflection_subfactor_weights_require_exact_nonnegative_sum() -> None:
    for field, value in (("revenue_acceleration", "-0.01"), ("growth_persistence", "0.06")):
        mapping = _mapping()
        weights = mapping["financial_inflection"]["scoring"]["subfactor_weights"]  # type: ignore[index]
        weights[field] = value  # type: ignore[index]
        with pytest.raises(ValidationError):
            scoring_policy_from_mapping(mapping)


def test_full_component_scores_six_subfactors_exactly() -> None:
    result = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(),
        policy=_policy(),
    )

    assert tuple(item.code for item in result.subfactors) == (
        "revenue_acceleration",
        "pat_acceleration",
        "margin_expansion",
        "roce_improvement",
        "growth_consistency",
        "growth_persistence",
    )
    assert tuple(item.raw_value for item in result.subfactors) == (
        Decimal("0.2"),
        Decimal("0.1"),
        Decimal("500"),
        Decimal("0.05"),
        Decimal("0.75"),
        Decimal("0.75"),
    )
    assert tuple(item.normalized_score for item in result.subfactors) == (
        Decimal("70"),
        Decimal("55"),
        Decimal("70"),
        Decimal("70"),
        Decimal("75"),
        Decimal("75"),
    )
    assert tuple(item.configured_weight for item in result.subfactors) == (
        Decimal("0.30"),
        Decimal("0.25"),
        Decimal("0.20"),
        Decimal("0.15"),
        Decimal("0.05"),
        Decimal("0.05"),
    )
    assert tuple(item.effective_weight for item in result.subfactors) == tuple(
        item.configured_weight for item in result.subfactors
    )
    assert tuple(item.contribution for item in result.subfactors) == (
        Decimal("21.0"),
        Decimal("13.75"),
        Decimal("14.0"),
        Decimal("10.50"),
        Decimal("3.75"),
        Decimal("3.75"),
    )
    assert result.weight_coverage == Decimal("1")
    assert result.score == Decimal("66.75")
    assert result.available_at == AS_OF - timedelta(days=1)
    assert result.missing_subfactors == () and result.warnings == ()


def test_partial_component_renormalizes_available_weight_without_zero_filling() -> None:
    result = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(growth_persistence=None),
        policy=_policy(),
    )

    assert result.score is not None
    assert result.weight_coverage == Decimal("0.95")
    assert result.missing_subfactors == ("growth_persistence",)
    assert sum((item.effective_weight for item in result.subfactors), Decimal("0")) == Decimal(
        "1"
    )
    assert result.score == sum(
        (item.contribution for item in result.subfactors), Decimal("0")
    )


def test_component_coverage_exact_boundary_passes_and_below_boundary_fails() -> None:
    evidence = _full_evidence(
        margin_expansion=None,
        growth_consistency=None,
        growth_persistence=None,
    )
    exact = FinancialInflectionComponentScorer().score(evidence=evidence, policy=_policy())
    mapping = _mapping()
    scoring = mapping["financial_inflection"]["scoring"]  # type: ignore[index]
    scoring["minimum_weight_coverage"] = "0.7001"  # type: ignore[index]
    below = FinancialInflectionComponentScorer().score(
        evidence=evidence,
        policy=scoring_policy_from_mapping(mapping),
    )

    assert exact.weight_coverage == Decimal("0.70") and exact.score is not None
    assert below.weight_coverage == Decimal("0.70") and below.score is None
    assert below.warnings == ("insufficient_subfactor_coverage",)
    assert all(item.effective_weight == 0 for item in below.subfactors)


def test_component_with_no_evidence_has_no_score_or_availability() -> None:
    result = FinancialInflectionComponentScorer().score(
        evidence=FinancialInflectionEvidence(
            company_id=COMPANY_ID,
            context=_context(),
            fiscal_year=2027,
            fiscal_quarter=2,
            as_of=AS_OF,
        ),
        policy=_policy(),
    )

    assert result.score is None and result.weight_coverage == Decimal("0")
    assert result.available_at is None
    assert result.missing_subfactors == (
        "revenue_acceleration",
        "pat_acceleration",
        "margin_expansion",
        "roce_improvement",
        "growth_consistency",
        "growth_persistence",
    )


def test_undefined_roce_and_missing_acceleration_are_unavailable_not_zero() -> None:
    result = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(
            revenue_acceleration=None,
            current_roce=_roce(None, 2027),
        ),
        policy=_policy(),
    )

    assert "revenue_acceleration" in result.missing_subfactors
    assert "roce_improvement" in result.missing_subfactors
    assert all(
        item.code not in {"revenue_acceleration", "roce_improvement"}
        for item in result.subfactors
    )


def test_roce_improvement_handles_negative_change_and_retains_both_inputs() -> None:
    result = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(
            current_roce=_roce(
                "0.10", 2027, available_at=AS_OF - timedelta(days=10)
            ),
            prior_year_roce=_roce(
                "0.15", 2026, available_at=AS_OF - timedelta(days=2)
            ),
        ),
        policy=_policy(),
    )
    subfactor = next(item for item in result.subfactors if item.code == "roce_improvement")

    assert subfactor.raw_value == Decimal("-0.05")
    assert subfactor.normalized_score == Decimal("0")
    assert subfactor.input_available_at == AS_OF - timedelta(days=2)
    assert isinstance(subfactor.evidence, RoceImprovementEvidence)


def test_roce_improvement_requires_available_same_context_pair() -> None:
    missing = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(prior_year_roce=None),
        policy=_policy(),
    )
    assert "roce_improvement" in missing.missing_subfactors

    with pytest.raises(ValueError, match="provider"):
        FinancialInflectionComponentScorer().score(
            evidence=_full_evidence(
                prior_year_roce=_roce("0.15", 2026, provider_dataset_id=uuid4())
            ),
            policy=_policy(),
        )


def test_extreme_values_clamp_and_saturated_persistence_has_no_bonus() -> None:
    high = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(
            revenue_acceleration=_acceleration("revenue", "20"),
            pat_acceleration=_acceleration("pat", "-20"),
            growth_persistence=_persistence(4, saturated=True),
        ),
        policy=_policy(),
    )
    scores = {item.code: item for item in high.subfactors}

    assert scores["revenue_acceleration"].normalized_score == Decimal("100")
    assert scores["pat_acceleration"].normalized_score == Decimal("0")
    assert scores["growth_persistence"].raw_value == Decimal("1")
    assert scores["growth_persistence"].normalized_score == Decimal("100")


def test_zero_weight_missing_subfactor_needs_no_evidence_or_warning() -> None:
    mapping = _mapping()
    weights = mapping["financial_inflection"]["scoring"]["subfactor_weights"]  # type: ignore[index]
    weights["growth_consistency"] = "0.10"  # type: ignore[index]
    weights["growth_persistence"] = "0"  # type: ignore[index]
    result = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(growth_persistence=None),
        policy=scoring_policy_from_mapping(mapping),
    )

    assert "growth_persistence" not in result.missing_subfactors
    assert result.weight_coverage == Decimal("1")


@pytest.mark.parametrize(
    "changes",
    [
        {"revenue_acceleration": _acceleration("revenue", "0.2", provider_dataset_id=uuid4())},
        {"revenue_acceleration": _acceleration("revenue", "0.2", filing_scope="standalone")},
        {"revenue_acceleration": _acceleration("revenue", "0.2", company_id=uuid4())},
    ],
)
def test_component_rejects_cross_context_evidence(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="provider|scope|company"):
        FinancialInflectionComponentScorer().score(
            evidence=_full_evidence(**changes),
            policy=_policy(),
        )


def test_component_rejects_current_and_prior_roce_endpoint_mismatches() -> None:
    for evidence in (
        _full_evidence(revenue_acceleration=_acceleration("revenue", "0.2", fiscal_quarter=1)),
        _full_evidence(prior_year_roce=_roce("0.15", 2026, fiscal_quarter=1)),
        _full_evidence(prior_year_roce=_roce("0.15", 2025)),
    ):
        with pytest.raises(ValueError, match="endpoint|prior ROCE"):
            FinancialInflectionComponentScorer().score(evidence=evidence, policy=_policy())


def test_component_validates_cutoff_and_normalizes_equivalent_offsets() -> None:
    with pytest.raises(ValueError, match="evidence as_of"):
        FinancialInflectionComponentScorer().score(
            evidence=_full_evidence(
                revenue_acceleration=_acceleration(
                    "revenue", "0.2", as_of=AS_OF + timedelta(days=1)
                )
            ),
            policy=_policy(),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        FinancialInflectionComponentScorer().score(
            evidence=_full_evidence(as_of=datetime(2027, 8, 1, 12)),
            policy=_policy(),
        )

    ist = timezone(timedelta(hours=5, minutes=30))
    offset_cutoff = datetime(2027, 8, 1, 17, 30, tzinfo=ist)
    accepted = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(as_of=offset_cutoff),
        policy=_policy(),
    )
    assert accepted.as_of == AS_OF


@pytest.mark.parametrize(
    "changes",
    [
        {"margin_expansion": _margin(margin_code="ebitda_margin")},
        {"growth_consistency": _consistency(metric_code="pat")},
        {"growth_consistency": _consistency(window_size=3)},
        {"growth_persistence": _persistence(metric_code="pat")},
        {"growth_persistence": _persistence(window_size=3)},
        {"growth_persistence": _persistence(threshold="0.2")},
    ],
)
def test_component_rejects_policy_incoherent_feature_inputs(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="configured|policy"):
        FinancialInflectionComponentScorer().score(
            evidence=_full_evidence(**changes),
            policy=_policy(),
        )


def test_component_order_is_canonical_regardless_of_evidence_construction() -> None:
    evidence = FinancialInflectionEvidence(
        growth_persistence=_persistence(),
        current_roce=_roce("0.20", 2027),
        company_id=COMPANY_ID,
        margin_expansion=_margin(),
        context=_context(),
        prior_year_roce=_roce("0.15", 2026),
        fiscal_year=2027,
        growth_consistency=_consistency(),
        fiscal_quarter=2,
        pat_acceleration=_acceleration("pat", "0.1"),
        as_of=AS_OF,
        revenue_acceleration=_acceleration("revenue", "0.2"),
    )
    result = FinancialInflectionComponentScorer().score(evidence=evidence, policy=_policy())

    assert tuple(item.code for item in result.subfactors) == (
        "revenue_acceleration",
        "pat_acceleration",
        "margin_expansion",
        "roce_improvement",
        "growth_consistency",
        "growth_persistence",
    )


def test_top_level_component_weights_and_confidence_do_not_change_component_score() -> None:
    policy = _policy()
    baseline = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(),
        policy=policy,
    )
    changed_weights = ComponentWeights(
        financial_inflection=Decimal("0.90"),
        business_catalyst=Decimal("0.10"),
        business_quality=Decimal("0"),
        cash_flow_quality=Decimal("0"),
        balance_sheet=Decimal("0"),
        valuation=Decimal("0"),
        market_structure=Decimal("0"),
        low_market_attention=Decimal("0"),
    )
    changed = policy.model_copy(update={"component_weights": changed_weights})
    rescored = FinancialInflectionComponentScorer().score(
        evidence=_full_evidence(),
        policy=changed,
    )

    assert baseline.score == rescored.score == Decimal("66.75")
    assert not hasattr(baseline, "confidence")
