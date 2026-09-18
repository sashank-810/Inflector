"""Phase 4D-A Business Quality policy and component-scoring acceptance tests."""

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

from inflector_core.business_quality_scoring import (
    BusinessQualityComponentScorer,
    BusinessQualityEvidence,
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
    ReturnOnCapitalEmployedValue,
    ReturnOnEquityValue,
)
from inflector_data.financial_features import QuarterMarginValue
from inflector_data.financial_snapshots import InstantFinancialSnapshot
from inflector_data.period_normalization import QuarterizedFinancialValue
from inflector_data.ttm import TrailingTwelveMonthValue
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
PHASE_4A_POLICY = FIXTURES / "inflection_model_v1_development.json"
PHASE_4B_POLICY = FIXTURES / "inflection_model_v1_scoring_development.json"
BUSINESS_QUALITY_POLICY = FIXTURES / "inflection_model_v1_business_quality_development.json"
PHASE_4A_CHECKSUM = "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf"
PHASE_4B_CHECKSUM = "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9"
BUSINESS_QUALITY_CHECKSUM = "c0a967e75aa30418ef030950032186ca6c0762f1fa67b5cf056ba06b8e8981c1"

PROVIDER_ID = UUID("11111111-1111-4111-8111-111111111111")
COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
AS_OF = datetime(2027, 8, 1, 12, tzinfo=UTC)


def _mapping(path: Path = BUSINESS_QUALITY_POLICY) -> dict[str, object]:
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


def _roce(
    value: str | None = "0.20",
    *,
    available_at: datetime = AS_OF - timedelta(days=3),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> ReturnOnCapitalEmployedValue:
    return ReturnOnCapitalEmployedValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="ratio",
        ttm_ebit=cast(TrailingTwelveMonthValue, object()),
        beginning_snapshot=cast(InstantFinancialSnapshot, object()),
        ending_snapshot=cast(InstantFinancialSnapshot, object()),
        beginning_capital_employed=Decimal("100"),
        ending_capital_employed=Decimal("120"),
        average_capital_employed=Decimal("110") if value is not None else None,
        warnings=() if value is not None else ("non_positive_capital_employed",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="roce_v1",
    )


def _roe(
    value: str | None = "0.15",
    *,
    available_at: datetime = AS_OF - timedelta(days=1),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> ReturnOnEquityValue:
    return ReturnOnEquityValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        value=None if value is None else Decimal(value),
        unit="ratio",
        ttm_pat=cast(TrailingTwelveMonthValue, object()),
        beginning_snapshot=cast(InstantFinancialSnapshot, object()),
        ending_snapshot=cast(InstantFinancialSnapshot, object()),
        beginning_equity=Decimal("100"),
        ending_equity=Decimal("100"),
        average_equity=Decimal("100") if value is not None else None,
        warnings=() if value is not None else ("non_positive_equity",),
        as_of=as_of,
        available_at=available_at,
        algorithm_version="roe_v1",
    )


def _margin(
    value: str = "0.10",
    *,
    margin_code: str = "operating_margin",
    available_at: datetime = AS_OF - timedelta(days=2),
    provider_dataset_id: UUID = PROVIDER_ID,
    company_id: UUID = COMPANY_ID,
    filing_scope: str = "consolidated",
    fiscal_year: int = 2027,
    fiscal_quarter: int = 2,
    as_of: datetime = AS_OF,
) -> QuarterMarginValue:
    quarter = cast(QuarterizedFinancialValue, object())
    return QuarterMarginValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope=filing_scope,
        margin_code=margin_code,
        ending_fiscal_year=fiscal_year,
        ending_fiscal_quarter=fiscal_quarter,
        period_start=date(2027, 7, 1),
        period_end=date(2027, 9, 30),
        numerator_metric_code="ebit",
        denominator_metric_code="revenue",
        value=Decimal(value),
        unit="ratio",
        operation="numerator_divided_by_revenue",
        as_of=as_of,
        available_at=available_at,
        algorithm_version="margin_v1",
        numerator_quarter=quarter,
        denominator_quarter=quarter,
    )


def _evidence(**changes: object) -> BusinessQualityEvidence:
    values: dict[str, object] = {
        "company_id": COMPANY_ID,
        "context": _context(),
        "fiscal_year": 2027,
        "fiscal_quarter": 2,
        "as_of": AS_OF,
        "roce": _roce(),
        "roe": _roe(),
        "margin": _margin(),
    }
    values.update(changes)
    return BusinessQualityEvidence(**values)  # type: ignore[arg-type]


def _score(
    evidence: BusinessQualityEvidence | None = None,
    policy: InflectionScoringPolicy | None = None,
):
    return BusinessQualityComponentScorer().score(
        evidence=evidence or _evidence(),
        policy=policy or _policy(),
    )


def test_business_quality_policy_preserves_legacy_checksums_and_null_omission() -> None:
    phase4a = scoring_policy_from_mapping(_mapping(PHASE_4A_POLICY))
    phase4b = scoring_policy_from_mapping(_mapping(PHASE_4B_POLICY))

    assert scoring_policy_checksum(phase4a) == PHASE_4A_CHECKSUM
    assert scoring_policy_checksum(phase4b) == PHASE_4B_CHECKSUM
    assert "business_quality" not in policy_to_canonical_mapping(phase4a)
    assert "business_quality" not in policy_to_canonical_mapping(phase4b)
    assert '"business_quality":null' not in canonical_policy_json(phase4b)


@pytest.mark.parametrize(
    ("path", "checksum", "version"),
    [
        (PHASE_4A_POLICY, PHASE_4A_CHECKSUM, "4a"),
        (PHASE_4B_POLICY, PHASE_4B_CHECKSUM, "4b"),
    ],
)
def test_legacy_persisted_policy_remains_readable(
    session,
    path: Path,
    checksum: str,
    version: str,
) -> None:
    policy = scoring_policy_from_mapping(_mapping(path))
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family=f"legacy-business-quality-regression-{version}",
        semantic_version="1.0.0",
        git_sha=f"phase{version}",
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
    assert loaded.policy.business_quality is None


def test_business_quality_policy_checksum_is_stable_and_sensitive() -> None:
    mapping = _mapping()
    original = scoring_policy_from_mapping(mapping)
    reordered = scoring_policy_from_mapping(dict(reversed(tuple(mapping.items()))))
    changed = json.loads(json.dumps(mapping))
    changed["business_quality"]["roce_level_curve"]["breakpoints"][3]["score"] = "81"

    assert scoring_policy_checksum(original) == BUSINESS_QUALITY_CHECKSUM
    assert scoring_policy_checksum(reordered) == BUSINESS_QUALITY_CHECKSUM
    assert scoring_policy_checksum(original) != scoring_policy_checksum(
        scoring_policy_from_mapping(changed)
    )
    assert scoring_policy_from_mapping(policy_to_canonical_mapping(original)) == original


@pytest.mark.parametrize(
    "mutation",
    [
        {"subfactor_weights": {"roce_level": "0.4", "roe_level": "0.3", "margin_level": "0.2"}},
        {"subfactor_weights": {"roce_level": "-0.1", "roe_level": "0.6", "margin_level": "0.5"}},
        {"minimum_weight_coverage": "0"},
        {"minimum_weight_coverage": "1.01"},
        {"margin_code": "net_margin"},
    ],
)
def test_business_quality_policy_rejects_invalid_configuration(
    mutation: dict[str, object],
) -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["business_quality"])
    section.update(mutation)
    with pytest.raises(ValidationError):
        scoring_policy_from_mapping(mapping)


def test_business_quality_full_component_has_exact_development_result() -> None:
    result = _score()

    assert result.weight_coverage == Decimal("1")
    assert result.available_weight == Decimal("1")
    assert result.score == Decimal("69.5")
    assert result.available_at == AS_OF - timedelta(days=1)
    assert [item.code for item in result.subfactors] == [
        "roce_level",
        "roe_level",
        "margin_level",
    ]
    assert [item.raw_value for item in result.subfactors] == [
        Decimal("0.20"),
        Decimal("0.15"),
        Decimal("0.10"),
    ]
    assert [item.normalized_score for item in result.subfactors] == [
        Decimal("80"),
        Decimal("65"),
        Decimal("50"),
    ]
    assert [item.configured_weight for item in result.subfactors] == [
        Decimal("0.50"),
        Decimal("0.30"),
        Decimal("0.20"),
    ]
    assert [item.effective_weight for item in result.subfactors] == [
        Decimal("0.50"),
        Decimal("0.30"),
        Decimal("0.20"),
    ]
    assert [item.contribution for item in result.subfactors] == [
        Decimal("40"),
        Decimal("19.5"),
        Decimal("10"),
    ]
    assert all(item.raw_unit == "ratio" for item in result.subfactors)


def test_business_quality_partial_evidence_renormalizes_and_obeys_coverage_boundary() -> None:
    exact = _score(_evidence(roe=None))
    assert exact.weight_coverage == Decimal("0.70")
    assert exact.score == (
        Decimal("80") * (Decimal("0.50") / Decimal("0.70"))
        + Decimal("50") * (Decimal("0.20") / Decimal("0.70"))
    )
    assert exact.missing_subfactors == ("roe_level",)
    assert [item.effective_weight for item in exact.subfactors] == [
        Decimal("0.50") / Decimal("0.70"),
        Decimal("0.20") / Decimal("0.70"),
    ]

    policy_mapping = _mapping()
    section = cast(dict[str, object], policy_mapping["business_quality"])
    section["minimum_weight_coverage"] = "0.7000000000000000000000000001"
    below = _score(
        _evidence(roe=None),
        scoring_policy_from_mapping(policy_mapping),
    )
    assert below.score is None
    assert below.warnings == ("insufficient_subfactor_coverage",)
    assert all(item.effective_weight == 0 for item in below.subfactors)


def test_business_quality_missing_and_undefined_factors_are_unavailable_not_zero() -> None:
    missing_roce = _score(_evidence(roce=None))
    missing_margin = _score(_evidence(margin=None))
    undefined_roce = _score(_evidence(roce=_roce(None)))
    undefined_roe = _score(_evidence(roe=_roe(None)))
    unavailable = _score(_evidence(roce=None, roe=None, margin=None))

    assert missing_roce.weight_coverage == Decimal("0.50")
    assert missing_roce.score is None
    assert missing_roce.missing_subfactors == ("roce_level",)
    assert missing_margin.weight_coverage == Decimal("0.80")
    assert missing_margin.score == Decimal("74.375")
    assert missing_margin.missing_subfactors == ("margin_level",)
    assert undefined_roce.score is None
    assert undefined_roce.missing_subfactors == ("roce_level",)
    assert undefined_roe.weight_coverage == Decimal("0.70")
    assert undefined_roe.score is not None
    assert undefined_roe.missing_subfactors == ("roe_level",)
    assert not any(item.normalized_score == 0 for item in undefined_roe.subfactors)
    assert unavailable.weight_coverage == 0
    assert unavailable.score is None
    assert unavailable.available_at is None
    assert unavailable.subfactors == ()


def test_business_quality_negative_values_and_extremes_use_curve_clamps() -> None:
    negative = _score(_evidence(roce=_roce("-0.05"), roe=_roe("-0.05"), margin=_margin("-0.05")))
    upper = _score(_evidence(roce=_roce("100"), roe=_roe("100"), margin=_margin("100")))
    lower = _score(_evidence(roce=_roce("-100"), roe=_roe("-100"), margin=_margin("-100")))

    assert [item.normalized_score for item in negative.subfactors] == [
        Decimal("10"),
        Decimal("10"),
        Decimal("10"),
    ]
    assert negative.score == Decimal("10")
    assert upper.score == Decimal("100")
    assert lower.score == Decimal("0")


def test_business_quality_zero_weight_evidence_is_excluded_from_score_and_availability() -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["business_quality"])
    section["subfactor_weights"] = {
        "roce_level": "0.50",
        "roe_level": "0",
        "margin_level": "0.50",
    }
    section["minimum_weight_coverage"] = "1"
    latest_roe = _roe(available_at=AS_OF)
    result = _score(
        _evidence(roe=latest_roe),
        scoring_policy_from_mapping(mapping),
    )

    assert [item.code for item in result.subfactors] == ["roce_level", "margin_level"]
    assert result.missing_subfactors == ()
    assert result.score == Decimal("65")
    assert result.available_at == AS_OF - timedelta(days=2)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"roce": _roce(provider_dataset_id=uuid4())}, "provider"),
        ({"roe": _roe(filing_scope="standalone")}, "filing scope"),
        ({"margin": _margin(company_id=uuid4())}, "company"),
    ],
)
def test_business_quality_rejects_context_mismatches(
    change: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _score(_evidence(**change))


@pytest.mark.parametrize(
    "change",
    [
        {"roce": _roce(fiscal_quarter=1)},
        {"roe": _roe(fiscal_year=2026)},
        {"margin": _margin(fiscal_quarter=3)},
    ],
)
def test_business_quality_rejects_endpoint_mismatches(change: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        _score(_evidence(**change))


def test_business_quality_enforces_cutoff_coherence_and_normalizes_utc() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _score(_evidence(roe=_roe(as_of=AS_OF + timedelta(days=1))))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(replace(_evidence(), as_of=AS_OF.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(_evidence(roe=_roe(as_of=AS_OF.replace(tzinfo=None))))

    india = timezone(timedelta(hours=5, minutes=30))
    result = _score(replace(_evidence(), as_of=AS_OF.astimezone(india)))
    assert result.as_of == AS_OF
    assert result.as_of.tzinfo is UTC


def test_business_quality_enforces_configured_margin_and_accepts_ebitda() -> None:
    with pytest.raises(ValueError, match="margin_code"):
        _score(_evidence(margin=_margin(margin_code="ebitda_margin")))

    mapping = _mapping()
    section = cast(dict[str, object], mapping["business_quality"])
    section["margin_code"] = "ebitda_margin"
    result = _score(
        _evidence(margin=_margin(margin_code="ebitda_margin")),
        scoring_policy_from_mapping(mapping),
    )
    assert result.score == Decimal("69.5")


def test_business_quality_top_level_weight_and_confidence_are_not_applied() -> None:
    policy = _policy()
    changed = policy.model_copy(
        update={
            "component_weights": ComponentWeights(
                financial_inflection=Decimal("0.10"),
                business_catalyst=Decimal("0"),
                business_quality=Decimal("0.90"),
                cash_flow_quality=Decimal("0"),
                balance_sheet=Decimal("0"),
                valuation=Decimal("0"),
                market_structure=Decimal("0"),
                low_market_attention=Decimal("0"),
            )
        }
    )

    original = _score(policy=policy)
    reweighted = _score(policy=changed)
    assert original.score == reweighted.score == Decimal("69.5")
    assert not hasattr(original, "confidence")
