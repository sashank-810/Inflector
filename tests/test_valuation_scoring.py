"""Phase 4D-E Valuation policy and pure component-scoring acceptance tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    canonical_policy_json,
    policy_to_canonical_mapping,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_core.valuation_scoring import (
    VALUATION_SUBFACTOR_ORDER,
    ValuationComponentScorer,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_snapshots import InstantFinancialSnapshotReader
from inflector_data.market_pit import PointInTimeMarketReader
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import (
    CSVFinancialsProvider,
    CSVMarketDataProvider,
    CSVUniverseProvider,
)
from inflector_data.service import IngestionService
from inflector_data.ttm import TrailingTwelveMonthNormalizer
from inflector_data.valuation_features import (
    SimplifiedEnterpriseValue,
    ValuationFeatureBundle,
    ValuationFeaturePrimitives,
    ValuationMultipleValue,
)
from inflector_database.models import Company, DataProvider, ProviderDataset, Security
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
PHASE_4A_POLICY = FIXTURES / "inflection_model_v1_development.json"
PHASE_4B_POLICY = FIXTURES / "inflection_model_v1_scoring_development.json"
PHASE_4D_A_POLICY = FIXTURES / "inflection_model_v1_business_quality_development.json"
PHASE_4D_B_POLICY = FIXTURES / "inflection_model_v1_cash_flow_quality_development.json"
PHASE_4D_C_POLICY = FIXTURES / "inflection_model_v1_balance_sheet_development.json"
VALUATION_POLICY = FIXTURES / "inflection_model_v1_valuation_development.json"

LEGACY_CHECKSUMS = (
    "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf",
    "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9",
    "c0a967e75aa30418ef030950032186ca6c0762f1fa67b5cf056ba06b8e8981c1",
    "d7e69165b3cba42dfebd01bd0117eba263eb76cd20bd66b5db4da3eeb37f2362",
    "d97590519f69dca71d26671a9266443c9beeee24e28d67d78424671d103979e6",
)
VALUATION_CHECKSUM = "fab4e33bdbcb3b108a36c7d1e4c9e7e92ccbdf0db0abe8eb085adfaff45e5632"

MARKET_ID = UUID("11111111-1111-4111-8111-111111111111")
FINANCIAL_ID = UUID("22222222-2222-4222-8222-222222222222")
COMPANY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SECURITY_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
AS_OF = datetime(2027, 8, 1, 12, tzinfo=UTC)
AVAILABLE = AS_OF - timedelta(days=1)


def _mapping(path: Path = VALUATION_POLICY) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _policy(mapping: dict[str, object] | None = None) -> InflectionScoringPolicy:
    return scoring_policy_from_mapping(mapping or _mapping())


_VALUES = {
    "market_cap_to_ttm_pat": "10",
    "market_cap_to_total_equity": "2.5",
    "market_cap_to_ttm_revenue": "2",
    "simplified_ev_to_ttm_ebitda": "5",
    "simplified_ev_to_ttm_revenue": "2.5",
}

_VERSIONS = {
    "market_cap_to_ttm_pat": "market_cap_to_ttm_pat_v1",
    "market_cap_to_total_equity": "market_cap_to_total_equity_v1",
    "market_cap_to_ttm_revenue": "market_cap_to_ttm_revenue_v1",
    "simplified_ev_to_ttm_ebitda": "simplified_ev_to_ttm_ebitda_v1",
    "simplified_ev_to_ttm_revenue": "simplified_ev_to_ttm_revenue_v1",
}


def _feature(
    code: str,
    value: str | None = None,
    *,
    warnings: tuple[str, ...] = (),
    as_of: datetime = AS_OF,
    available_at: datetime | None = AVAILABLE,
    unit: str = "ratio",
    algorithm_version: str | None = None,
) -> ValuationMultipleValue:
    raw = _VALUES[code] if value is None and not warnings else value
    return ValuationMultipleValue(
        code=code,
        value=None if raw is None else Decimal(raw),
        unit=unit,
        numerator_value=None,
        numerator_unit=None,
        denominator_value=None,
        denominator_unit=None,
        warnings=warnings,
        as_of=as_of,
        available_at=available_at,
        algorithm_version=algorithm_version or _VERSIONS[code],
        evidence=(),
    )


def _bundle(**changes: object) -> ValuationFeatureBundle:
    values: dict[str, object] = {
        "market_provider_dataset_id": MARKET_ID,
        "financial_provider_dataset_id": FINANCIAL_ID,
        "company_id": COMPANY_ID,
        "security_id": SECURITY_ID,
        "filing_scope": "standalone",
        "ending_fiscal_year": 2026,
        "ending_fiscal_quarter": 4,
        "market_on_or_before": None,
        "as_of": AS_OF,
        "market_bar": None,
        "ttm_revenue": None,
        "ttm_pat": None,
        "ttm_ebitda": None,
        "equity_snapshot": None,
        "net_debt_snapshot": None,
        "simplified_enterprise_value": SimplifiedEnterpriseValue(
            value=Decimal("1250"),
            unit="INR",
            market_cap=Decimal("1000"),
            total_debt=Decimal("300"),
            cash_and_equivalents=Decimal("50"),
            net_debt=Decimal("250"),
            warnings=(),
            as_of=AS_OF,
            available_at=AVAILABLE,
            algorithm_version="simplified_enterprise_value_v1",
            market_bar=None,
            balance_sheet_snapshot=None,
        ),
        "market_cap_to_ttm_pat": _feature("market_cap_to_ttm_pat"),
        "market_cap_to_total_equity": _feature("market_cap_to_total_equity"),
        "market_cap_to_ttm_revenue": _feature("market_cap_to_ttm_revenue"),
        "simplified_ev_to_ttm_ebitda": _feature("simplified_ev_to_ttm_ebitda"),
        "simplified_ev_to_ttm_revenue": _feature("simplified_ev_to_ttm_revenue"),
        "algorithm_version": "valuation_feature_bundle_v1",
    }
    values.update(changes)
    return ValuationFeatureBundle(**values)  # type: ignore[arg-type]


def _score(
    evidence: ValuationFeatureBundle | None = None,
    policy: InflectionScoringPolicy | None = None,
):
    return ValuationComponentScorer().score(
        evidence=evidence or _bundle(),
        policy=policy or _policy(),
    )


def _subfactor(result, code: str):
    return next(item for item in result.subfactors if item.code == code)


def test_valuation_policy_preserves_legacy_checksums_and_omits_null() -> None:
    paths = (
        PHASE_4A_POLICY,
        PHASE_4B_POLICY,
        PHASE_4D_A_POLICY,
        PHASE_4D_B_POLICY,
        PHASE_4D_C_POLICY,
    )
    for path, checksum in zip(paths, LEGACY_CHECKSUMS, strict=True):
        policy = scoring_policy_from_mapping(_mapping(path))
        assert scoring_policy_checksum(policy) == checksum
        assert "valuation" not in policy_to_canonical_mapping(policy)
        assert '"valuation":null' not in canonical_policy_json(policy)


@pytest.mark.parametrize(
    ("path", "checksum", "version"),
    [
        (PHASE_4A_POLICY, LEGACY_CHECKSUMS[0], "4a"),
        (PHASE_4B_POLICY, LEGACY_CHECKSUMS[1], "4b"),
        (PHASE_4D_A_POLICY, LEGACY_CHECKSUMS[2], "4da"),
        (PHASE_4D_B_POLICY, LEGACY_CHECKSUMS[3], "4db"),
        (PHASE_4D_C_POLICY, LEGACY_CHECKSUMS[4], "4dc"),
    ],
)
def test_legacy_persisted_policies_remain_readable(
    session, path: Path, checksum: str, version: str
) -> None:
    policy = scoring_policy_from_mapping(_mapping(path))
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family=f"legacy-valuation-{version}",
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
    assert loaded.policy.valuation is None


def test_valuation_policy_roundtrip_order_checksum_and_breakpoint_sensitivity(session) -> None:
    mapping = _mapping()
    policy = _policy(mapping)
    reordered = scoring_policy_from_mapping(dict(reversed(tuple(mapping.items()))))
    changed = json.loads(json.dumps(mapping))
    changed["valuation"]["market_cap_to_ttm_pat_signal_curve"]["breakpoints"][6]["score"] = "81"
    assert scoring_policy_checksum(policy) == VALUATION_CHECKSUM
    assert scoring_policy_checksum(reordered) == VALUATION_CHECKSUM
    assert scoring_policy_checksum(scoring_policy_from_mapping(changed)) != VALUATION_CHECKSUM
    assert scoring_policy_from_mapping(policy_to_canonical_mapping(policy)) == policy

    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="valuation-policy",
        semantic_version="1.0.0",
        git_sha="valuation",
        status="active",
    )
    persisted = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="active",
        policy=policy,
    )
    loaded = repository.get_scoring_configuration(persisted.record.id)
    assert loaded is not None and loaded.policy == policy
    assert loaded.record.checksum_sha256 == VALUATION_CHECKSUM


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "subfactor_weights": {
                **{code: "0.20" for code in VALUATION_SUBFACTOR_ORDER},
                "market_cap_to_ttm_pat": "-0.10",
            }
        },
        {"subfactor_weights": {code: "0.10" for code in VALUATION_SUBFACTOR_ORDER}},
        {"minimum_weight_coverage": "0"},
        {"minimum_weight_coverage": "1.01"},
    ],
)
def test_valuation_policy_rejects_invalid_weights_and_coverage(mutation) -> None:
    mapping = _mapping()
    cast(dict[str, object], mapping["valuation"]).update(mutation)
    with pytest.raises(ValidationError):
        scoring_policy_from_mapping(mapping)


def test_valuation_policy_rejects_binary_float_breakpoint() -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["valuation"])
    curve = cast(dict[str, object], section["market_cap_to_ttm_pat_signal_curve"])
    breakpoints = cast(list[dict[str, object]], curve["breakpoints"])
    breakpoints[0]["raw_value"] = -75.0
    with pytest.raises(ValidationError):
        scoring_policy_from_mapping(mapping)


def test_full_component_has_exact_transforms_contributions_and_score() -> None:
    result = _score()
    assert result.weight_coverage == Decimal("1")
    assert result.score == Decimal("79.50")
    assert [item.code for item in result.subfactors] == list(VALUATION_SUBFACTOR_ORDER)
    assert [item.raw_value for item in result.subfactors] == [
        Decimal("10"),
        Decimal("2.5"),
        Decimal("2"),
        Decimal("5"),
        Decimal("2.5"),
    ]
    assert [item.scoring_value for item in result.subfactors] == [
        Decimal("-10"),
        Decimal("-2.5"),
        Decimal("-2"),
        Decimal("-5"),
        Decimal("-2.5"),
    ]
    assert [item.normalized_score for item in result.subfactors] == [
        Decimal("80"),
        Decimal("65"),
        Decimal("75"),
        Decimal("90"),
        Decimal("75"),
    ]
    assert [item.configured_weight for item in result.subfactors] == [
        Decimal("0.30"),
        Decimal("0.15"),
        Decimal("0.10"),
        Decimal("0.30"),
        Decimal("0.15"),
    ]
    assert [item.contribution for item in result.subfactors] == [
        Decimal("24"),
        Decimal("9.75"),
        Decimal("7.5"),
        Decimal("27"),
        Decimal("11.25"),
    ]
    assert [item.transform_code for item in result.subfactors] == [
        "negate_market_cap_to_ttm_pat",
        "negate_market_cap_to_total_equity",
        "negate_market_cap_to_ttm_revenue",
        "negate_simplified_ev_to_ttm_ebitda",
        "negate_simplified_ev_to_ttm_revenue",
    ]


@pytest.mark.parametrize(
    ("missing", "coverage"),
    [("market_cap_to_ttm_pat", "0.70"), ("simplified_ev_to_ttm_ebitda", "0.70")],
)
def test_exact_minimum_coverage_scores_with_renormalization(missing: str, coverage: str) -> None:
    original = _bundle()
    unavailable = _feature(missing, None, warnings=(f"missing_{missing}",))
    result = _score(replace(original, **{missing: unavailable}))
    assert result.weight_coverage == Decimal(coverage)
    assert result.score is not None
    assert result.missing_subfactors == (missing,)
    assert result.unavailable_subfactors[0].warnings == (f"missing_{missing}",)
    assert sum((item.effective_weight for item in result.subfactors), Decimal("0")) == Decimal("1")


def test_missing_pat_and_ebitda_fails_coverage_without_zero_fill() -> None:
    bundle = replace(
        _bundle(),
        market_cap_to_ttm_pat=_feature(
            "market_cap_to_ttm_pat", None, warnings=("non_positive_ttm_pat",)
        ),
        simplified_ev_to_ttm_ebitda=_feature(
            "simplified_ev_to_ttm_ebitda", None, warnings=("non_positive_ttm_ebitda",)
        ),
    )
    result = _score(bundle)
    assert result.weight_coverage == Decimal("0.40")
    assert result.score is None
    assert result.warnings == ("insufficient_subfactor_coverage",)
    assert result.missing_subfactors == (
        "market_cap_to_ttm_pat",
        "simplified_ev_to_ttm_ebitda",
    )
    assert all(item.effective_weight == 0 and item.contribution == 0 for item in result.subfactors)


def test_missing_market_cap_and_non_positive_simplified_ev_semantics() -> None:
    all_missing = {
        code: _feature(code, None, warnings=("missing_market_cap",))
        for code in VALUATION_SUBFACTOR_ORDER
    }
    missing_result = _score(replace(_bundle(), **all_missing))
    assert missing_result.weight_coverage == 0
    assert missing_result.score is None
    assert missing_result.available_at is None
    assert [item.warnings for item in missing_result.unavailable_subfactors] == [
        ("missing_market_cap",)
    ] * 5

    non_positive_ev = replace(
        _bundle(),
        simplified_ev_to_ttm_ebitda=_feature(
            "simplified_ev_to_ttm_ebitda",
            None,
            warnings=("non_positive_simplified_enterprise_value",),
        ),
        simplified_ev_to_ttm_revenue=_feature(
            "simplified_ev_to_ttm_revenue",
            None,
            warnings=("non_positive_simplified_enterprise_value",),
        ),
    )
    ev_result = _score(non_positive_ev)
    assert ev_result.weight_coverage == Decimal("0.55")
    assert ev_result.score is None


def test_lower_multiples_rank_higher_and_extremes_clamp() -> None:
    cases = {
        "market_cap_to_ttm_pat": ("8", "18"),
        "market_cap_to_total_equity": ("1", "6"),
        "market_cap_to_ttm_revenue": ("1", "10"),
        "simplified_ev_to_ttm_ebitda": ("5", "15"),
        "simplified_ev_to_ttm_revenue": ("1.5", "10"),
    }
    for code, (cheap, expensive) in cases.items():
        cheap_score = _subfactor(_score(replace(_bundle(), **{code: _feature(code, cheap)})), code)
        expensive_score = _subfactor(
            _score(replace(_bundle(), **{code: _feature(code, expensive)})), code
        )
        assert cheap_score.normalized_score >= expensive_score.normalized_score
        upper = _subfactor(_score(replace(_bundle(), **{code: _feature(code, "0.01")})), code)
        lower = _subfactor(_score(replace(_bundle(), **{code: _feature(code, "1000")})), code)
        assert upper.normalized_score == Decimal("100")
        assert lower.normalized_score == Decimal("0")


def test_zero_weight_exclusion_and_top_level_weight_separation() -> None:
    mapping = _mapping()
    section = cast(dict[str, object], mapping["valuation"])
    weights = cast(dict[str, str], section["subfactor_weights"])
    weights["market_cap_to_ttm_pat"] = "0"
    weights["market_cap_to_total_equity"] = "0.45"
    zero_policy = _policy(mapping)
    late_pat = _feature("market_cap_to_ttm_pat", available_at=AS_OF)
    zero_result = _score(replace(_bundle(), market_cap_to_ttm_pat=late_pat), zero_policy)
    assert "market_cap_to_ttm_pat" not in [item.code for item in zero_result.subfactors]
    assert "market_cap_to_ttm_pat" not in zero_result.missing_subfactors
    assert zero_result.available_at == AVAILABLE

    changed = _mapping()
    component_weights = cast(dict[str, str], changed["component_weights"])
    component_weights["valuation"] = "0.20"
    component_weights["business_catalyst"] = "0.10"
    assert _score().score == _score(policy=_policy(changed)).score == Decimal("79.50")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("algorithm_version", "valuation_feature_bundle_v2", "bundle version"),
        ("market_provider_dataset_id", uuid4(), "market evidence"),
        ("financial_provider_dataset_id", uuid4(), "TTM evidence"),
        ("company_id", uuid4(), "TTM evidence"),
        ("security_id", uuid4(), "market evidence"),
        ("filing_scope", "consolidated", "TTM evidence"),
        ("ending_fiscal_year", 2025, "TTM evidence"),
        ("ending_fiscal_quarter", 3, "TTM evidence"),
        ("as_of", AS_OF + timedelta(hours=1), "TTM as_of"),
    ],
)
def test_real_bundle_context_tampering_is_rejected(
    session, tmp_path, field, value, message
) -> None:
    bundle = _real_bundle(session, tmp_path, datetime(2027, 4, 30, tzinfo=UTC))
    with pytest.raises(ValueError, match=message):
        _score(replace(bundle, **{field: value}))


def test_feature_code_version_unit_warning_and_time_integrity() -> None:
    code = "market_cap_to_ttm_pat"
    with pytest.raises(ValueError, match="wrong code"):
        _score(replace(_bundle(), **{code: replace(_feature(code), code="pe")}))
    with pytest.raises(ValueError, match="feature version"):
        _score(
            replace(
                _bundle(),
                **{code: replace(_feature(code), algorithm_version="market_cap_to_ttm_pat_v2")},
            )
        )
    with pytest.raises(ValueError, match="unit must be ratio"):
        _score(replace(_bundle(), **{code: replace(_feature(code), unit="multiple")}))
    with pytest.raises(ValueError, match="must not carry warnings"):
        _score(replace(_bundle(), **{code: replace(_feature(code), warnings=("contradiction",))}))
    with pytest.raises(ValueError, match="defined value requires available_at"):
        _score(replace(_bundle(), **{code: replace(_feature(code), available_at=None)}))
    with pytest.raises(ValueError, match="exceeds bundle cutoff"):
        _score(
            replace(
                _bundle(),
                **{code: replace(_feature(code), available_at=AS_OF + timedelta(seconds=1))},
            )
        )
    with pytest.raises(ValueError, match="as_of does not match"):
        _score(replace(_bundle(), **{code: replace(_feature(code), as_of=AS_OF - timedelta(1))}))
    with pytest.raises(ValueError, match="timezone-aware"):
        _score(replace(_bundle(), as_of=AS_OF.replace(tzinfo=None)))
    equivalent = replace(
        _bundle(), as_of=AS_OF.astimezone(timezone(timedelta(hours=5, minutes=30)))
    )
    assert _score(equivalent).as_of == AS_OF


RETRIEVED_AT = datetime(2027, 6, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("valuation_score_universe", "csv", "universe", "synthetic")
MARKET = ProviderMetadata("valuation_score_market", "csv", "market_daily", "synthetic")
FINANCIAL = ProviderMetadata("valuation_score_financial", "csv", "financials", "synthetic")


def _dataset_id(session, provider: str, dataset: str) -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(DataProvider.code == provider, ProviderDataset.code == dataset)
    )
    assert value is not None
    return value


def _real_setup(session, tmp_path: Path, *, corrections: bool = False):
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    service.ingest_market_data(
        CSVMarketDataProvider(FIXTURES / "valuation_market_synthetic.csv", MARKET, RETRIEVED_AT)
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "valuation_financials_synthetic.csv", FINANCIAL, RETRIEVED_AT
        )
    )
    if corrections:
        service.ingest_market_data(
            CSVMarketDataProvider(
                FIXTURES / "valuation_market_correction_synthetic.csv", MARKET, RETRIEVED_AT
            )
        )
        service.ingest_financials(
            CSVFinancialsProvider(
                FIXTURES / "valuation_financials_pat_restatement_synthetic.csv",
                FINANCIAL,
                RETRIEVED_AT,
            )
        )
    security = session.scalar(select(Security).where(Security.isin == "INF0AUR01018"))
    company = session.scalar(
        select(Company).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    assert security is not None and company is not None
    financial_reader = PointInTimeFinancialReader(session)
    primitives = ValuationFeaturePrimitives(
        session,
        PointInTimeMarketReader(session),
        TrailingTwelveMonthNormalizer(FiscalQuarterNormalizer(financial_reader)),
        InstantFinancialSnapshotReader(financial_reader),
    )
    arguments = {
        "market_provider_dataset_id": _dataset_id(
            session, "valuation_score_market", "market_daily"
        ),
        "financial_provider_dataset_id": _dataset_id(
            session, "valuation_score_financial", "financials"
        ),
        "security_id": security.id,
        "company_id": company.id,
        "filing_scope": "standalone",
        "ending_fiscal_year": 2026,
        "ending_fiscal_quarter": 4,
        "interval": "1d",
    }
    return primitives, arguments


def _real_bundle(session, tmp_path: Path, as_of: datetime, *, corrections: bool = False):
    primitives, arguments = _real_setup(session, tmp_path, corrections=corrections)
    return primitives.features_as_of(as_of=as_of, **arguments)


def test_real_phase_3ha_bundle_scores_exact_full_case_and_retains_lineage(
    session, tmp_path
) -> None:
    bundle = _real_bundle(session, tmp_path, datetime(2027, 4, 30, tzinfo=UTC))
    result = _score(bundle)
    assert result.score == Decimal("79.5")
    assert result.weight_coverage == Decimal("1")
    assert result.market_provider_dataset_id != result.financial_provider_dataset_id
    assert all(item.evidence is getattr(bundle, item.code) for item in result.subfactors)
    assert bundle.market_bar is not None
    assert bundle.market_bar.source_record.validation_status == "accepted"
    assert bundle.ttm_pat is not None and len(bundle.ttm_pat.lineage) == 4


def test_market_correction_and_pat_restatement_propagate_without_cross_factor_changes(
    session, tmp_path
) -> None:
    primitives, arguments = _real_setup(session, tmp_path, corrections=True)
    before_market = primitives.features_as_of(
        as_of=datetime(2027, 5, 10, 17, 59, tzinfo=UTC), **arguments
    )
    at_market = primitives.features_as_of(as_of=datetime(2027, 5, 10, 18, tzinfo=UTC), **arguments)
    before_restatement = primitives.features_as_of(
        as_of=datetime(2027, 5, 15, 11, 59, tzinfo=UTC), **arguments
    )
    at_restatement = primitives.features_as_of(
        as_of=datetime(2027, 5, 15, 12, tzinfo=UTC), **arguments
    )
    before_market_score = _score(before_market)
    at_market_score = _score(at_market)
    before_restatement_score = _score(before_restatement)
    at_restatement_score = _score(at_restatement)
    assert at_market_score.score is not None and before_market_score.score is not None
    assert before_market_score.score == Decimal("79.5")
    assert at_market_score.score == Decimal("73.95000000000000000000000000")
    assert before_restatement_score.score == Decimal("73.95000000000000000000000000")
    assert at_restatement_score.score == Decimal("71.70000000000000000000000000")
    assert _score(
        primitives.features_as_of(as_of=datetime(2027, 4, 30, tzinfo=UTC), **arguments)
    ).score == Decimal("79.5")
    assert _subfactor(at_restatement_score, "market_cap_to_ttm_pat").raw_value == Decimal("15")
    assert (
        _subfactor(at_restatement_score, "market_cap_to_ttm_pat").normalized_score
        <= _subfactor(before_restatement_score, "market_cap_to_ttm_pat").normalized_score
    )
    for code in VALUATION_SUBFACTOR_ORDER[1:]:
        before = _subfactor(before_restatement_score, code)
        after = _subfactor(at_restatement_score, code)
        assert (before.raw_value, before.scoring_value, before.normalized_score) == (
            after.raw_value,
            after.scoring_value,
            after.normalized_score,
        )
