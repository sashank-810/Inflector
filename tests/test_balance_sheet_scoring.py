"""Phase 4D-C Balance Sheet policy and scoring acceptance tests."""

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

from inflector_core.balance_sheet_scoring import (
    BalanceSheetComponentScorer,
    BalanceSheetEvidence,
    NetDebtToEbitdaEvidence,
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
from inflector_data.capital_features import (
    DebtToEquityValue,
    InterestCoverageValue,
    NetDebtValue,
)
from inflector_data.financial_snapshots import InstantFinancialSnapshot
from inflector_data.pit import FinancialPeriodView
from inflector_data.ttm import TrailingTwelveMonthValue
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
PHASE_4A_POLICY = FIXTURES / "inflection_model_v1_development.json"
PHASE_4B_POLICY = FIXTURES / "inflection_model_v1_scoring_development.json"
PHASE_4D_A_POLICY = FIXTURES / "inflection_model_v1_business_quality_development.json"
PHASE_4D_B_POLICY = FIXTURES / "inflection_model_v1_cash_flow_quality_development.json"
BALANCE_SHEET_POLICY = FIXTURES / "inflection_model_v1_balance_sheet_development.json"

LEGACY_CHECKSUMS = (
    "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf",
    "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9",
    "c0a967e75aa30418ef030950032186ca6c0762f1fa67b5cf056ba06b8e8981c1",
    "d7e69165b3cba42dfebd01bd0117eba263eb76cd20bd66b5db4da3eeb37f2362",
)
BALANCE_SHEET_CHECKSUM = "d97590519f69dca71d26671a9266443c9beeee24e28d67d78424671d103979e6"

PROVIDER_ID = UUID("11111111-1111-4111-8111-111111111111")
COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PERIOD_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
AS_OF = datetime(2027, 8, 1, 12, tzinfo=UTC)
PERIOD_END = date(2027, 6, 30)


def _mapping(path: Path = BALANCE_SHEET_POLICY) -> dict[str, object]:
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


def _period(
    *,
    period_id: UUID = PERIOD_ID,
    fiscal_year: int = 2027,
    fiscal_quarter: int | None = 2,
    period_end: date = PERIOD_END,
) -> FinancialPeriodView:
    return FinancialPeriodView(
        id=period_id,
        period_kind="quarter",
        period_start=date(2027, 4, 1),
        period_end=period_end,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        is_ytd=False,
    )


def _snapshot(
    *,
    period: FinancialPeriodView | None = None,
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    as_of: datetime = AS_OF,
    available_at: datetime = AS_OF - timedelta(days=4),
) -> InstantFinancialSnapshot:
    return InstantFinancialSnapshot(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        fiscal_period=period or _period(),
        components=(),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="instant_snapshot_v1",
    )


def _ttm(
    value: str = "100",
    *,
    metric_code: str = "ebitda_reported",
    unit: str = "INR",
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    period_end: date = PERIOD_END,
    as_of: datetime = AS_OF,
    available_at: datetime = AS_OF - timedelta(days=3),
) -> TrailingTwelveMonthValue:
    return TrailingTwelveMonthValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        metric_code=metric_code,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        period_start=date(2026, 7, 1),
        period_end=period_end,
        value=Decimal(value),
        unit=unit,
        construction_kind="four_quarter_sum",
        operation="sum_four_consecutive_quarters",
        algorithm_version="ttm_v1",
        as_of=as_of,
        available_at=available_at,
        lineage=(),
    )


def _net_debt(
    value: str = "200",
    *,
    snapshot: InstantFinancialSnapshot | None = None,
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    as_of: datetime = AS_OF,
    available_at: datetime = AS_OF - timedelta(days=4),
) -> NetDebtValue:
    return NetDebtValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        value=Decimal(value),
        unit="INR",
        as_of=as_of,
        available_at=available_at,
        algorithm_version="net_debt_v1",
        snapshot=snapshot or _snapshot(),
    )


def _debt_equity(
    value: str | None = "0.5",
    *,
    debt: str = "50",
    equity: str = "100",
    snapshot: InstantFinancialSnapshot | None = None,
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    as_of: datetime = AS_OF,
    available_at: datetime = AS_OF - timedelta(days=2),
) -> DebtToEquityValue:
    return DebtToEquityValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        value=None if value is None else Decimal(value),
        unit="ratio",
        debt=Decimal(debt),
        equity=Decimal(equity),
        warnings=() if value is not None else ("non_positive_equity",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="debt_equity_v1",
        snapshot=snapshot or _snapshot(),
    )


def _interest_coverage(
    value: str | None = "4",
    *,
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    period_end: date = PERIOD_END,
    as_of: datetime = AS_OF,
    available_at: datetime = AS_OF - timedelta(days=1),
) -> InterestCoverageValue:
    return InterestCoverageValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="ratio",
        ttm_ebit=_ttm(
            "400",
            metric_code="ebit",
            period_end=period_end,
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=as_of,
        ),
        ttm_finance_cost=_ttm(
            "100",
            metric_code="finance_cost",
            period_end=period_end,
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            as_of=as_of,
        ),
        warnings=() if value is not None else ("non_positive_finance_cost",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="interest_coverage_v1",
    )


def _evidence(**changes: object) -> BalanceSheetEvidence:
    values: dict[str, object] = {
        "company_id": COMPANY_ID,
        "context": _context(),
        "fiscal_year": 2027,
        "fiscal_quarter": 2,
        "as_of": AS_OF,
        "net_debt": _net_debt(),
        "ttm_ebitda": _ttm(),
        "debt_to_equity": _debt_equity(),
        "interest_coverage": _interest_coverage(),
    }
    values.update(changes)
    return BalanceSheetEvidence(**values)  # type: ignore[arg-type]


def _score(
    evidence: BalanceSheetEvidence | None = None,
    policy: InflectionScoringPolicy | None = None,
):
    return BalanceSheetComponentScorer().score(
        evidence=evidence or _evidence(),
        policy=policy or _policy(),
    )


def _subfactor(result, code: str):
    return next(item for item in result.subfactors if item.code == code)


def test_balance_sheet_policy_preserves_all_legacy_checksums_and_null_omission() -> None:
    paths = (PHASE_4A_POLICY, PHASE_4B_POLICY, PHASE_4D_A_POLICY, PHASE_4D_B_POLICY)
    for path, checksum in zip(paths, LEGACY_CHECKSUMS, strict=True):
        policy = scoring_policy_from_mapping(_mapping(path))
        assert scoring_policy_checksum(policy) == checksum
        assert "balance_sheet" not in policy_to_canonical_mapping(policy)
        assert '"balance_sheet":null' not in canonical_policy_json(policy)


@pytest.mark.parametrize(
    ("path", "checksum", "version"),
    [
        (PHASE_4A_POLICY, LEGACY_CHECKSUMS[0], "4a"),
        (PHASE_4B_POLICY, LEGACY_CHECKSUMS[1], "4b"),
        (PHASE_4D_A_POLICY, LEGACY_CHECKSUMS[2], "4da"),
        (PHASE_4D_B_POLICY, LEGACY_CHECKSUMS[3], "4db"),
    ],
)
def test_balance_sheet_legacy_persisted_policies_remain_readable(
    session,
    path: Path,
    checksum: str,
    version: str,
) -> None:
    policy = scoring_policy_from_mapping(_mapping(path))
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family=f"legacy-balance-sheet-{version}",
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
    assert loaded.policy.balance_sheet is None


def test_balance_sheet_policy_checksum_is_stable_order_independent_and_sensitive() -> None:
    mapping = _mapping()
    original = scoring_policy_from_mapping(mapping)
    reordered = scoring_policy_from_mapping(dict(reversed(tuple(mapping.items()))))
    changed = json.loads(json.dumps(mapping))
    changed["balance_sheet"]["interest_coverage_curve"]["breakpoints"][4]["score"] = "76"
    assert scoring_policy_checksum(original) == BALANCE_SHEET_CHECKSUM
    assert scoring_policy_checksum(reordered) == BALANCE_SHEET_CHECKSUM
    assert scoring_policy_checksum(original) != scoring_policy_checksum(
        scoring_policy_from_mapping(changed)
    )
    assert scoring_policy_from_mapping(policy_to_canonical_mapping(original)) == original


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "subfactor_weights": {
                "net_debt_to_ebitda": "0.39",
                "debt_to_equity": "0.30",
                "interest_coverage": "0.30",
            }
        },
        {
            "subfactor_weights": {
                "net_debt_to_ebitda": "-0.10",
                "debt_to_equity": "0.50",
                "interest_coverage": "0.60",
            }
        },
        {"minimum_weight_coverage": "0"},
        {"minimum_weight_coverage": "1.01"},
    ],
)
def test_balance_sheet_policy_rejects_invalid_configuration(
    mutation: dict[str, object],
) -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["balance_sheet"])
    section.update(mutation)
    with pytest.raises(ValidationError):
        scoring_policy_from_mapping(mapping)


def test_balance_sheet_full_component_has_exact_transforms_and_score() -> None:
    result = _score()
    assert result.weight_coverage == Decimal("1")
    assert result.score == Decimal("67")
    assert result.available_at == AS_OF - timedelta(days=1)
    assert [item.code for item in result.subfactors] == [
        "net_debt_to_ebitda",
        "debt_to_equity",
        "interest_coverage",
    ]
    assert [item.raw_value for item in result.subfactors] == [
        Decimal("200"),
        Decimal("0.5"),
        Decimal("4"),
    ]
    assert [item.scoring_value for item in result.subfactors] == [
        Decimal("-2"),
        Decimal("-0.5"),
        Decimal("4"),
    ]
    assert [item.normalized_score for item in result.subfactors] == [
        Decimal("55"),
        Decimal("75"),
        Decimal("75"),
    ]
    assert [item.contribution for item in result.subfactors] == [
        Decimal("22"),
        Decimal("22.5"),
        Decimal("22.5"),
    ]
    leverage = _subfactor(result, "net_debt_to_ebitda")
    assert leverage.normalized_raw_value == Decimal("2")
    assert leverage.transform_code == "negate_net_debt_over_ttm_ebitda"
    assert isinstance(leverage.evidence, NetDebtToEbitdaEvidence)


def test_net_debt_scoring_is_size_neutral_and_net_cash_is_valid() -> None:
    smaller = _subfactor(_score(), "net_debt_to_ebitda")
    larger = _subfactor(
        _score(_evidence(net_debt=_net_debt("2000"), ttm_ebitda=_ttm("1000"))),
        "net_debt_to_ebitda",
    )
    net_cash = _subfactor(
        _score(_evidence(net_debt=_net_debt("-100"), ttm_ebitda=_ttm("100"))),
        "net_debt_to_ebitda",
    )
    assert smaller.normalized_raw_value == larger.normalized_raw_value == Decimal("2")
    assert smaller.scoring_value == larger.scoring_value == Decimal("-2")
    assert smaller.normalized_score == larger.normalized_score == Decimal("55")
    assert net_cash.normalized_raw_value == Decimal("-1")
    assert net_cash.scoring_value == Decimal("1")
    assert net_cash.normalized_score == Decimal("100")


@pytest.mark.parametrize("ebitda", ["0", "-100"])
def test_non_positive_ebitda_makes_only_leverage_factor_unavailable(ebitda: str) -> None:
    result = _score(_evidence(ttm_ebitda=_ttm(ebitda)))
    assert result.weight_coverage == Decimal("0.60")
    assert result.score is None
    assert result.missing_subfactors == ("net_debt_to_ebitda",)
    assert all(item.code != "net_debt_to_ebitda" for item in result.subfactors)


def test_debt_to_equity_negation_rewards_lower_leverage() -> None:
    scores = []
    for value in ("0", "0.5", "1", "2"):
        item = _subfactor(_score(_evidence(debt_to_equity=_debt_equity(value))), "debt_to_equity")
        scores.append(item.normalized_score)
    assert scores == [Decimal("100"), Decimal("75"), Decimal("50"), Decimal("20")]
    assert scores[0] > scores[1] > scores[2] > scores[3]


def test_undefined_debt_equity_and_interest_coverage_are_unavailable() -> None:
    no_de = _score(_evidence(debt_to_equity=_debt_equity(None, equity="0")))
    no_coverage = _score(_evidence(interest_coverage=_interest_coverage(None)))
    assert no_de.weight_coverage == Decimal("0.70")
    assert no_de.score is not None
    assert no_de.missing_subfactors == ("debt_to_equity",)
    assert no_coverage.weight_coverage == Decimal("0.70")
    assert no_coverage.score is not None
    assert no_coverage.missing_subfactors == ("interest_coverage",)


def test_interest_coverage_identity_supports_negative_and_ordered_values() -> None:
    expected = {
        "-1": Decimal("5"),
        "0": Decimal("10"),
        "2": Decimal("50"),
        "4": Decimal("75"),
        "8": Decimal("90"),
        "100": Decimal("100"),
    }
    for value, score in expected.items():
        item = _subfactor(
            _score(_evidence(interest_coverage=_interest_coverage(value))),
            "interest_coverage",
        )
        assert item.scoring_value == Decimal(value)
        assert item.normalized_score == score
        assert item.transform_code == "identity"


def test_balance_sheet_partial_coverage_and_exact_boundary() -> None:
    no_interest = _score(_evidence(interest_coverage=None))
    no_de = _score(_evidence(debt_to_equity=None))
    no_leverage = _score(_evidence(net_debt=None, ttm_ebitda=None))
    assert no_interest.weight_coverage == Decimal("0.70")
    assert no_interest.score is not None
    assert no_de.weight_coverage == Decimal("0.70")
    assert no_de.score is not None
    assert no_leverage.weight_coverage == Decimal("0.60")
    assert no_leverage.score is None

    mapping = _mapping()
    section = cast(dict[str, object], mapping["balance_sheet"])
    section["minimum_weight_coverage"] = "0.7001"
    below = _score(_evidence(interest_coverage=None), scoring_policy_from_mapping(mapping))
    assert below.score is None
    assert below.warnings == ("insufficient_subfactor_coverage",)

    unavailable = _score(
        _evidence(
            net_debt=None,
            ttm_ebitda=None,
            debt_to_equity=_debt_equity(None, equity="0"),
            interest_coverage=_interest_coverage(None),
        )
    )
    assert unavailable.weight_coverage == 0
    assert unavailable.score is None
    assert unavailable.available_at is None


def test_balance_sheet_extreme_values_clamp_without_extrapolation() -> None:
    stressed = _score(
        _evidence(
            net_debt=_net_debt("10000"),
            ttm_ebitda=_ttm("100"),
            debt_to_equity=_debt_equity("100", debt="10000"),
            interest_coverage=_interest_coverage("-100"),
        )
    )
    strong = _score(
        _evidence(
            net_debt=_net_debt("-10000"),
            ttm_ebitda=_ttm("100"),
            debt_to_equity=_debt_equity("0", debt="0"),
            interest_coverage=_interest_coverage("100"),
        )
    )
    assert stressed.score == Decimal("0")
    assert strong.score == Decimal("100")


@pytest.mark.parametrize(
    "item",
    [
        _debt_equity("0.5", debt="-1"),
        _debt_equity("0.5", equity="0"),
        _debt_equity("-0.5", debt="50", equity="100"),
    ],
)
def test_defined_debt_equity_rejects_structural_sign_inconsistency(
    item: DebtToEquityValue,
) -> None:
    with pytest.raises(ValueError, match="structurally inconsistent"):
        _score(_evidence(debt_to_equity=item))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"net_debt": _net_debt(provider_dataset_id=uuid4())}, "provider"),
        ({"ttm_ebitda": _ttm(filing_scope="standalone")}, "filing scope"),
        ({"debt_to_equity": _debt_equity(company_id=uuid4())}, "company"),
        ({"interest_coverage": _interest_coverage(provider_dataset_id=uuid4())}, "provider"),
    ],
)
def test_balance_sheet_rejects_context_mismatches(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _score(_evidence(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"net_debt": _net_debt(snapshot=_snapshot(period=_period(fiscal_quarter=1)))},
        {"debt_to_equity": _debt_equity(snapshot=_snapshot(period=_period(fiscal_year=2026)))},
        {"ttm_ebitda": _ttm(fiscal_quarter=3)},
        {"interest_coverage": _interest_coverage(fiscal_quarter=1)},
    ],
)
def test_balance_sheet_rejects_endpoint_mismatches(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        _score(_evidence(**change))


def test_balance_sheet_rejects_period_end_and_stock_period_id_mismatches() -> None:
    with pytest.raises(ValueError, match="period_end"):
        _score(_evidence(ttm_ebitda=_ttm(period_end=date(2027, 6, 29))))

    alternate = _snapshot(period=_period(period_id=uuid4()))
    with pytest.raises(ValueError, match="period id"):
        _score(_evidence(debt_to_equity=_debt_equity(snapshot=alternate)))


@pytest.mark.parametrize(
    "change",
    [
        {"ttm_ebitda": _ttm(metric_code="revenue")},
        {"ttm_ebitda": _ttm(unit="USD")},
    ],
)
def test_balance_sheet_rejects_invalid_ttm_ebitda_contract(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="TTM EBITDA"):
        _score(_evidence(**change))


def test_balance_sheet_enforces_cutoff_coherence_and_normalizes_utc() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _score(_evidence(ttm_ebitda=_ttm(as_of=AS_OF + timedelta(days=1))))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(replace(_evidence(), as_of=AS_OF.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(_evidence(interest_coverage=_interest_coverage(as_of=AS_OF.replace(tzinfo=None))))

    india = timezone(timedelta(hours=5, minutes=30))
    result = _score(replace(_evidence(), as_of=AS_OF.astimezone(india)))
    assert result.as_of == AS_OF
    assert result.as_of.tzinfo is UTC


def test_balance_sheet_available_at_includes_denominator_and_participating_factors() -> None:
    result = _score(
        _evidence(
            net_debt=_net_debt(available_at=AS_OF - timedelta(days=5)),
            ttm_ebitda=_ttm(available_at=AS_OF - timedelta(days=2)),
            debt_to_equity=_debt_equity(available_at=AS_OF - timedelta(days=3)),
            interest_coverage=_interest_coverage(available_at=AS_OF - timedelta(days=1)),
        )
    )
    leverage = _subfactor(result, "net_debt_to_ebitda")
    assert leverage.input_available_at == AS_OF - timedelta(days=2)
    assert result.available_at == AS_OF - timedelta(days=1)

    mapping = _mapping()
    section = cast(dict[str, object], mapping["balance_sheet"])
    section["subfactor_weights"] = {
        "net_debt_to_ebitda": "0.50",
        "debt_to_equity": "0.50",
        "interest_coverage": "0",
    }
    section["minimum_weight_coverage"] = "1"
    zero_interest = _score(_evidence(), scoring_policy_from_mapping(mapping))
    assert all(item.code != "interest_coverage" for item in zero_interest.subfactors)
    assert zero_interest.available_at == AS_OF - timedelta(days=2)


def test_zero_weight_net_debt_pair_is_excluded_from_score_and_availability() -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["balance_sheet"])
    section["subfactor_weights"] = {
        "net_debt_to_ebitda": "0",
        "debt_to_equity": "0.50",
        "interest_coverage": "0.50",
    }
    section["minimum_weight_coverage"] = "1"
    result = _score(
        _evidence(
            net_debt=_net_debt(available_at=AS_OF),
            ttm_ebitda=_ttm(available_at=AS_OF),
        ),
        scoring_policy_from_mapping(mapping),
    )
    assert result.weight_coverage == 1
    assert all(item.code != "net_debt_to_ebitda" for item in result.subfactors)
    assert "net_debt_to_ebitda" not in result.missing_subfactors
    assert result.available_at == AS_OF - timedelta(days=1)


def test_balance_sheet_top_level_weight_and_confidence_are_not_applied() -> None:
    policy = _policy()
    changed = policy.model_copy(
        update={
            "component_weights": ComponentWeights(
                financial_inflection=Decimal("0.10"),
                business_catalyst=Decimal("0"),
                business_quality=Decimal("0"),
                cash_flow_quality=Decimal("0"),
                balance_sheet=Decimal("0.90"),
                valuation=Decimal("0"),
                market_structure=Decimal("0"),
                low_market_attention=Decimal("0"),
            )
        }
    )
    original = _score(policy=policy)
    reweighted = _score(policy=changed)
    assert original.score == reweighted.score == Decimal("67")
    assert not hasattr(original, "confidence")
