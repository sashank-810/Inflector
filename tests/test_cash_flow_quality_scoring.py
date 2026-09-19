"""Phase 4D-B Cash-Flow Quality policy and scoring acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from inflector_core.cash_flow_quality_scoring import (
    CashFlowQualityComponentScorer,
    CashFlowQualityEvidence,
)
from inflector_core.scoring_policy import (
    ComponentWeights,
    FinancialContextSelection,
    InflectionScoringPolicy,
    canonical_policy_json,
    policy_to_canonical_mapping,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_data.cash_flow_features import (
    CashFlowConversionValue,
    CashFlowToEbitdaValue,
    ReceivableDaysValue,
    TradeWorkingCapitalChangeValue,
)
from inflector_data.financial_snapshots import InstantFinancialSnapshot
from inflector_data.ttm import TrailingTwelveMonthValue
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
PHASE_4A_POLICY = FIXTURES / "inflection_model_v1_development.json"
PHASE_4B_POLICY = FIXTURES / "inflection_model_v1_scoring_development.json"
PHASE_4D_A_POLICY = FIXTURES / "inflection_model_v1_business_quality_development.json"
CASH_FLOW_POLICY = FIXTURES / "inflection_model_v1_cash_flow_quality_development.json"

LEGACY_CHECKSUMS = (
    "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf",
    "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9",
    "c0a967e75aa30418ef030950032186ca6c0762f1fa67b5cf056ba06b8e8981c1",
)
CASH_FLOW_QUALITY_CHECKSUM = "d7e69165b3cba42dfebd01bd0117eba263eb76cd20bd66b5db4da3eeb37f2362"

PROVIDER_ID = UUID("11111111-1111-4111-8111-111111111111")
COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
AS_OF = datetime(2027, 8, 1, 12, tzinfo=UTC)


def _mapping(path: Path = CASH_FLOW_POLICY) -> dict[str, object]:
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


def _ttm(value: str, metric_code: str = "revenue") -> TrailingTwelveMonthValue:
    return TrailingTwelveMonthValue(
        provider_dataset_id=PROVIDER_ID,
        company_id=COMPANY_ID,
        filing_scope="consolidated",
        metric_code=metric_code,
        ending_fiscal_year=2027,
        ending_fiscal_quarter=2,
        period_start=date(2026, 7, 1),
        period_end=date(2027, 6, 30),
        value=Decimal(value),
        unit="INR",
        construction_kind="four_quarter_sum",
        operation="sum_four_consecutive_quarters",
        algorithm_version="ttm_v1",
        as_of=AS_OF,
        available_at=AS_OF - timedelta(days=5),
        lineage=(),
    )


def _conversion(
    value: str | None = "1.00",
    *,
    available_at: datetime = AS_OF - timedelta(days=4),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> CashFlowConversionValue:
    pat = Decimal("100") if value is not None else Decimal("0")
    cfo = Decimal(value) * pat if value is not None else Decimal("100")
    return CashFlowConversionValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="ratio",
        cfo=cfo,
        pat=pat,
        cash_minus_pat=cfo - pat,
        warnings=() if value is not None else ("non_positive_pat",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="cfo_conversion_v1",
        ttm_cfo=_ttm(str(cfo), "cash_flow_from_operations"),
        ttm_pat=_ttm(str(pat), "pat"),
    )


def _cfo_ebitda(
    value: str | None = "0.70",
    *,
    available_at: datetime = AS_OF - timedelta(days=3),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> CashFlowToEbitdaValue:
    ebitda = Decimal("100") if value is not None else Decimal("0")
    cfo = Decimal(value) * ebitda if value is not None else Decimal("70")
    return CashFlowToEbitdaValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="ratio",
        cfo=cfo,
        ebitda=ebitda,
        warnings=() if value is not None else ("non_positive_ebitda",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="cfo_ebitda_v1",
        ttm_cfo=_ttm(str(cfo), "cash_flow_from_operations"),
        ttm_ebitda=_ttm(str(ebitda), "ebitda_reported"),
    )


def _receivable_days(
    value: str | None = "60",
    *,
    available_at: datetime = AS_OF - timedelta(days=1),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> ReceivableDaysValue:
    snapshot = cast(InstantFinancialSnapshot, object())
    return ReceivableDaysValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="days",
        beginning_receivables=Decimal("100"),
        ending_receivables=Decimal("100"),
        average_receivables=Decimal("100") if value is not None else None,
        warnings=() if value is not None else ("non_positive_revenue",),
        ttm_revenue=_ttm("1000"),
        beginning_snapshot=snapshot,
        ending_snapshot=snapshot,
        as_of=as_of,
        available_at=available_at,
        algorithm_version="receivable_days_v1",
    )


def _twc(
    value: str | None = "50",
    *,
    revenue: str = "1000",
    available_at: datetime = AS_OF - timedelta(days=2),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> TradeWorkingCapitalChangeValue:
    snapshot = cast(InstantFinancialSnapshot, object())
    return TradeWorkingCapitalChangeValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="INR",
        beginning_receivables=Decimal("100"),
        beginning_inventory=Decimal("100"),
        beginning_payables=Decimal("100"),
        beginning_trade_working_capital=Decimal("100") if value is not None else None,
        ending_receivables=Decimal("150"),
        ending_inventory=Decimal("100"),
        ending_payables=Decimal("100"),
        ending_trade_working_capital=(
            Decimal("100") + Decimal(value) if value is not None else None
        ),
        warnings=() if value is not None else ("negative_working_capital_component",),
        ttm_revenue=_ttm(revenue),
        beginning_snapshot=snapshot,
        ending_snapshot=snapshot,
        as_of=as_of,
        available_at=available_at,
        algorithm_version="trade_working_capital_change_v1",
    )


def _evidence(**changes: object) -> CashFlowQualityEvidence:
    values: dict[str, object] = {
        "company_id": COMPANY_ID,
        "context": _context(),
        "fiscal_year": 2027,
        "fiscal_quarter": 2,
        "as_of": AS_OF,
        "cfo_conversion": _conversion(),
        "cfo_to_ebitda": _cfo_ebitda(),
        "receivable_days": _receivable_days(),
        "trade_working_capital_change": _twc(),
    }
    values.update(changes)
    return CashFlowQualityEvidence(**values)  # type: ignore[arg-type]


def _score(
    evidence: CashFlowQualityEvidence | None = None,
    policy: InflectionScoringPolicy | None = None,
):
    return CashFlowQualityComponentScorer().score(
        evidence=evidence or _evidence(),
        policy=policy or _policy(),
    )


def _subfactor(result, code: str):
    return next(item for item in result.subfactors if item.code == code)


def test_cash_flow_policy_preserves_all_legacy_checksums_and_null_omission() -> None:
    paths = (PHASE_4A_POLICY, PHASE_4B_POLICY, PHASE_4D_A_POLICY)
    for path, checksum in zip(paths, LEGACY_CHECKSUMS, strict=True):
        policy = scoring_policy_from_mapping(_mapping(path))
        assert scoring_policy_checksum(policy) == checksum
        assert "cash_flow_quality" not in policy_to_canonical_mapping(policy)
        assert '"cash_flow_quality":null' not in canonical_policy_json(policy)


@pytest.mark.parametrize(
    ("path", "checksum", "version"),
    [
        (PHASE_4A_POLICY, LEGACY_CHECKSUMS[0], "4a"),
        (PHASE_4B_POLICY, LEGACY_CHECKSUMS[1], "4b"),
        (PHASE_4D_A_POLICY, LEGACY_CHECKSUMS[2], "4da"),
    ],
)
def test_cash_flow_legacy_persisted_policies_remain_readable(
    session,
    path: Path,
    checksum: str,
    version: str,
) -> None:
    policy = scoring_policy_from_mapping(_mapping(path))
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family=f"legacy-cash-flow-{version}",
        semantic_version="1.0.0",
        git_sha=version,
        status="active",
    )
    persisted = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="legacy",
        configuration_version="1",
        status="active",
        policy=policy,
    )
    loaded = repository.get_scoring_configuration(persisted.record.id)
    assert loaded is not None
    assert loaded.record.checksum_sha256 == checksum
    assert loaded.policy == policy
    assert loaded.policy.cash_flow_quality is None


def test_cash_flow_policy_checksum_is_stable_order_independent_and_sensitive() -> None:
    mapping = _mapping()
    original = scoring_policy_from_mapping(mapping)
    reordered = scoring_policy_from_mapping(dict(reversed(tuple(mapping.items()))))
    changed = json.loads(json.dumps(mapping))
    changed["cash_flow_quality"]["cfo_conversion_curve"]["breakpoints"][4]["score"] = "91"

    assert scoring_policy_checksum(original) == CASH_FLOW_QUALITY_CHECKSUM
    assert scoring_policy_checksum(reordered) == CASH_FLOW_QUALITY_CHECKSUM
    assert scoring_policy_checksum(original) != scoring_policy_checksum(
        scoring_policy_from_mapping(changed)
    )
    assert scoring_policy_from_mapping(policy_to_canonical_mapping(original)) == original


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "subfactor_weights": {
                "cfo_conversion": "0.39",
                "cfo_to_ebitda": "0.30",
                "receivable_days": "0.15",
                "trade_working_capital_burden": "0.15",
            }
        },
        {
            "subfactor_weights": {
                "cfo_conversion": "-0.10",
                "cfo_to_ebitda": "0.50",
                "receivable_days": "0.30",
                "trade_working_capital_burden": "0.30",
            }
        },
        {"minimum_weight_coverage": "0"},
        {"minimum_weight_coverage": "1.01"},
    ],
)
def test_cash_flow_policy_rejects_invalid_configuration(mutation: dict[str, object]) -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["cash_flow_quality"])
    section.update(mutation)
    with pytest.raises(ValidationError):
        scoring_policy_from_mapping(mapping)


def test_cash_flow_quality_full_component_preserves_transforms_and_exact_score() -> None:
    result = _score()
    assert result.weight_coverage == Decimal("1")
    assert result.score == Decimal("73.875")
    assert result.available_at == AS_OF - timedelta(days=1)
    assert [item.code for item in result.subfactors] == [
        "cfo_conversion",
        "cfo_to_ebitda",
        "receivable_days",
        "trade_working_capital_burden",
    ]
    assert [item.raw_value for item in result.subfactors] == [
        Decimal("1.00"),
        Decimal("0.70"),
        Decimal("60"),
        Decimal("50"),
    ]
    assert [item.scoring_value for item in result.subfactors] == [
        Decimal("1.00"),
        Decimal("0.70"),
        Decimal("-60"),
        Decimal("-0.05"),
    ]
    assert [item.normalized_score for item in result.subfactors] == [
        Decimal("90"),
        Decimal("70"),
        Decimal("70"),
        Decimal("42.5"),
    ]
    assert [item.contribution for item in result.subfactors] == [
        Decimal("36"),
        Decimal("21"),
        Decimal("10.5"),
        Decimal("6.375"),
    ]
    conversion = _subfactor(result, "cfo_conversion")
    receivables = _subfactor(result, "receivable_days")
    assert conversion.transform_code == "identity"
    assert conversion.normalized_raw_value is None
    assert receivables.transform_code == "negate_receivable_days"
    assert receivables.scoring_unit == "negative_days"
    twc = _subfactor(result, "trade_working_capital_burden")
    assert twc.raw_unit == "INR"
    assert twc.normalized_raw_value == Decimal("0.05")
    assert twc.normalized_raw_unit == "ratio"
    assert twc.transform_code == "negate_twc_change_over_ttm_revenue"


def test_receivable_days_negation_rewards_lower_days() -> None:
    scores = []
    signals = []
    for days in ("30", "60", "120"):
        item = _subfactor(
            _score(_evidence(receivable_days=_receivable_days(days))), "receivable_days"
        )
        scores.append(item.normalized_score)
        signals.append(item.scoring_value)
    assert signals == [Decimal("-30"), Decimal("-60"), Decimal("-120")]
    assert scores == [Decimal("90"), Decimal("70"), Decimal("20")]
    assert scores[0] > scores[1] > scores[2]


def test_twc_build_release_and_size_normalization_are_explicit() -> None:
    build_a = _subfactor(
        _score(_evidence(trade_working_capital_change=_twc("50", revenue="1000"))),
        "trade_working_capital_burden",
    )
    build_b = _subfactor(
        _score(_evidence(trade_working_capital_change=_twc("500", revenue="10000"))),
        "trade_working_capital_burden",
    )
    release = _subfactor(
        _score(_evidence(trade_working_capital_change=_twc("-50", revenue="1000"))),
        "trade_working_capital_burden",
    )

    assert build_a.normalized_raw_value == build_b.normalized_raw_value == Decimal("0.05")
    assert build_a.scoring_value == build_b.scoring_value == Decimal("-0.05")
    assert build_a.normalized_score == build_b.normalized_score == Decimal("42.5")
    assert release.raw_value == Decimal("-50")
    assert release.normalized_raw_value == Decimal("-0.05")
    assert release.scoring_value == Decimal("0.05")
    assert release.normalized_score == Decimal("80")


@pytest.mark.parametrize("revenue", ["0", "-1000"])
def test_twc_non_positive_revenue_makes_only_that_factor_unavailable(revenue: str) -> None:
    result = _score(_evidence(trade_working_capital_change=_twc("50", revenue=revenue)))
    assert result.weight_coverage == Decimal("0.85")
    assert result.score is not None
    assert result.missing_subfactors == ("trade_working_capital_burden",)
    assert all(item.code != "trade_working_capital_burden" for item in result.subfactors)


def test_negative_cfo_ratios_are_valid_curve_inputs() -> None:
    result = _score(
        _evidence(cfo_conversion=_conversion("-0.25"), cfo_to_ebitda=_cfo_ebitda("-0.25"))
    )
    conversion = _subfactor(result, "cfo_conversion")
    ebitda = _subfactor(result, "cfo_to_ebitda")
    assert conversion.scoring_value == Decimal("-0.25")
    assert ebitda.scoring_value == Decimal("-0.25")
    assert conversion.normalized_score == Decimal("5")
    assert ebitda.normalized_score == Decimal("5")


def test_cash_flow_undefined_factors_are_unavailable_not_zero() -> None:
    undefined = _score(
        _evidence(
            cfo_conversion=_conversion(None),
            cfo_to_ebitda=_cfo_ebitda(None),
            receivable_days=_receivable_days(None),
            trade_working_capital_change=_twc(None),
        )
    )
    assert undefined.weight_coverage == 0
    assert undefined.score is None
    assert undefined.available_at is None
    assert undefined.subfactors == ()
    assert undefined.missing_subfactors == (
        "cfo_conversion",
        "cfo_to_ebitda",
        "receivable_days",
        "trade_working_capital_burden",
    )


@pytest.mark.parametrize(
    ("change", "missing_code", "remaining_weight"),
    [
        ({"cfo_conversion": _conversion(None)}, "cfo_conversion", Decimal("0.60")),
        ({"cfo_to_ebitda": _cfo_ebitda(None)}, "cfo_to_ebitda", Decimal("0.70")),
        ({"receivable_days": _receivable_days(None)}, "receivable_days", Decimal("0.85")),
        (
            {"trade_working_capital_change": _twc(None)},
            "trade_working_capital_burden",
            Decimal("0.85"),
        ),
    ],
)
def test_each_undefined_cash_flow_factor_is_omitted_not_scored_as_zero(
    change: dict[str, object],
    missing_code: str,
    remaining_weight: Decimal,
) -> None:
    result = _score(_evidence(**change))
    assert result.weight_coverage == remaining_weight
    assert (result.score is not None) == (remaining_weight >= Decimal("0.70"))
    assert missing_code in result.missing_subfactors
    assert all(item.code != missing_code for item in result.subfactors)


def test_cash_flow_partial_coverage_renormalizes_at_exact_boundary() -> None:
    exact = _score(_evidence(receivable_days=None, trade_working_capital_change=None))
    assert exact.weight_coverage == Decimal("0.70")
    assert exact.score == (
        Decimal("90") * (Decimal("0.40") / Decimal("0.70"))
        + Decimal("70") * (Decimal("0.30") / Decimal("0.70"))
    )
    assert exact.missing_subfactors == (
        "receivable_days",
        "trade_working_capital_burden",
    )

    mapping = _mapping()
    section = cast(dict[str, object], mapping["cash_flow_quality"])
    section["minimum_weight_coverage"] = "0.7000000000000000000000000001"
    below = _score(
        _evidence(receivable_days=None, trade_working_capital_change=None),
        scoring_policy_from_mapping(mapping),
    )
    assert below.score is None
    assert below.warnings == ("insufficient_subfactor_coverage",)
    assert all(item.effective_weight == 0 for item in below.subfactors)


def test_cash_flow_one_missing_factor_renormalizes_without_zero_score() -> None:
    result = _score(_evidence(receivable_days=None))
    assert result.weight_coverage == Decimal("0.85")
    assert result.score is not None
    assert result.missing_subfactors == ("receivable_days",)
    assert all(item.code != "receivable_days" for item in result.subfactors)
    assert sum((item.effective_weight for item in result.subfactors), Decimal("0")) == 1


def test_cash_flow_extreme_values_clamp_without_extrapolation() -> None:
    upper = _score(
        _evidence(
            cfo_conversion=_conversion("100"),
            cfo_to_ebitda=_cfo_ebitda("100"),
            receivable_days=_receivable_days("0"),
            trade_working_capital_change=_twc("-10000", revenue="1000"),
        )
    )
    lower = _score(
        _evidence(
            cfo_conversion=_conversion("-100"),
            cfo_to_ebitda=_cfo_ebitda("-100"),
            receivable_days=_receivable_days("10000"),
            trade_working_capital_change=_twc("10000", revenue="1000"),
        )
    )
    assert upper.score == Decimal("100")
    assert lower.score == Decimal("0")


def test_cash_flow_rejects_structurally_negative_receivable_days() -> None:
    with pytest.raises(ValueError, match="receivable days"):
        _score(_evidence(receivable_days=_receivable_days("-1")))


def test_cash_flow_zero_weight_evidence_is_excluded_from_availability() -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["cash_flow_quality"])
    section["subfactor_weights"] = {
        "cfo_conversion": "0.40",
        "cfo_to_ebitda": "0.30",
        "receivable_days": "0",
        "trade_working_capital_burden": "0.30",
    }
    section["minimum_weight_coverage"] = "1"
    result = _score(
        _evidence(receivable_days=_receivable_days(available_at=AS_OF)),
        scoring_policy_from_mapping(mapping),
    )
    assert result.weight_coverage == 1
    assert all(item.code != "receivable_days" for item in result.subfactors)
    assert "receivable_days" not in result.missing_subfactors
    assert result.available_at == AS_OF - timedelta(days=2)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"cfo_conversion": _conversion(provider_dataset_id=uuid4())}, "provider"),
        ({"cfo_to_ebitda": _cfo_ebitda(filing_scope="standalone")}, "filing scope"),
        ({"receivable_days": _receivable_days(company_id=uuid4())}, "company"),
    ],
)
def test_cash_flow_rejects_context_mismatches(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _score(_evidence(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"cfo_conversion": _conversion(fiscal_quarter=1)},
        {"cfo_to_ebitda": _cfo_ebitda(fiscal_year=2026)},
        {"receivable_days": _receivable_days(fiscal_quarter=3)},
        {"trade_working_capital_change": _twc(fiscal_year=2026)},
    ],
)
def test_cash_flow_rejects_endpoint_mismatches(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        _score(_evidence(**change))


def test_cash_flow_enforces_cutoff_coherence_and_normalizes_utc() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _score(_evidence(cfo_conversion=_conversion(as_of=AS_OF + timedelta(days=1))))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(replace(_evidence(), as_of=AS_OF.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(_evidence(cfo_conversion=_conversion(as_of=AS_OF.replace(tzinfo=None))))

    india = timezone(timedelta(hours=5, minutes=30))
    result = _score(replace(_evidence(), as_of=AS_OF.astimezone(india)))
    assert result.as_of == AS_OF
    assert result.as_of.tzinfo is UTC


def test_cash_flow_top_level_weight_and_confidence_are_not_applied() -> None:
    policy = _policy()
    changed = policy.model_copy(
        update={
            "component_weights": ComponentWeights(
                financial_inflection=Decimal("0.10"),
                business_catalyst=Decimal("0"),
                business_quality=Decimal("0"),
                cash_flow_quality=Decimal("0.90"),
                balance_sheet=Decimal("0"),
                valuation=Decimal("0"),
                market_structure=Decimal("0"),
                low_market_attention=Decimal("0"),
            )
        }
    )
    original = _score(policy=policy)
    reweighted = _score(policy=changed)
    assert original.score == reweighted.score == Decimal("73.875")
    assert not hasattr(original, "confidence")
