"""Phase 3G-C deterministic Market Structure feature tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.market_structure_scoring import (
    MARKET_STRUCTURE_COMPONENT_VERSION,
    MARKET_STRUCTURE_SUBFACTOR_ORDER,
    MarketStructureComponentScorer,
)
from inflector_core.providers import (
    BenchmarkBarRecord,
    CorporateActionRecord,
    IngestionEnvelope,
    MarketBarRecord,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    canonical_policy_json,
    policy_to_canonical_mapping,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_data.archive import LocalRawObjectStore
from inflector_data.corporate_action_pit import PointInTimeCorporateActionReader
from inflector_data.market_adjustments import (
    ADJUSTED_MARKET_PRICE_VERSION,
    AdjustedMarketBar,
    AdjustedMarketSeries,
    MarketAdjustmentPrimitives,
)
from inflector_data.market_pit import (
    BenchmarkSeriesView,
    PointInTimeBenchmarkBar,
    PointInTimeMarketBar,
    PointInTimeMarketReader,
)
from inflector_data.market_structure_features import (
    MarketStructureFeaturePrimitives,
    MovingAverageEvidence,
    RelativeStrengthEvidence,
    VolatilityRatioEvidence,
)
from inflector_data.pit import SourceRecordView
from inflector_data.providers import (
    CSVUniverseProvider,
    MockBenchmarkDataProvider,
    MockCorporateActionProvider,
    MockMarketDataProvider,
)
from inflector_data.service import IngestionService
from inflector_database.models import DataProvider, ProviderDataset, Security
from inflector_database.scoring_repository import ScoringPolicyRepository

FIXTURES = Path(__file__).parent / "fixtures"
MARKET_STRUCTURE_POLICY = FIXTURES / "inflection_model_v1_market_structure_development.json"
MARKET_STRUCTURE_POLICY_CHECKSUM = (
    "9235f7d9cf09066edc0c27269776b958240c0a0ec52a36353da47dba4e9df381"
)
AS_OF = datetime(2026, 4, 1, 12, tzinfo=UTC)
START = date(2026, 1, 1)
MARKET_ID = UUID(int=101)
ACTION_ID = UUID(int=102)
BENCHMARK_ID = UUID(int=103)
SECURITY_ID = UUID(int=104)


def _source(identifier: int) -> SourceRecordView:
    return SourceRecordView(
        id=UUID(int=identifier),
        external_record_id=f"record-{identifier}",
        source_uri="fixture://market-structure",
        raw_object_key=f"raw/{identifier}",
        raw_payload_reference=f"row-{identifier}",
        content_sha256=f"{identifier:064x}",
        validation_status="accepted",
    )


def _raw_bar(
    index: int,
    close: Decimal,
    *,
    volume: int = 200,
    delivery: Decimal | None = Decimal("0.4"),
    available_at: datetime | None = None,
) -> PointInTimeMarketBar:
    timestamp = available_at or datetime(2026, 3, 1, tzinfo=UTC)
    return PointInTimeMarketBar(
        id=UUID(int=1000 + index),
        provider_dataset_id=MARKET_ID,
        security_id=SECURITY_ID,
        trading_date=START + timedelta(days=index * 2),
        interval="1d",
        open_price=close,
        high_price=close + Decimal("1"),
        low_price=max(Decimal("0"), close - Decimal("1")),
        close_price=close,
        volume=volume,
        market_cap=Decimal("1000000"),
        delivery_quantity=80,
        delivery_percentage=delivery,
        available_at=timestamp,
        revision_at=None,
        ingested_at=timestamp + timedelta(days=10),
        source_record=_source(1000 + index),
    )


def _series(
    closes: Sequence[Decimal],
    *,
    raw_closes: Sequence[Decimal] | None = None,
    volumes: Sequence[int] | None = None,
    deliveries: Sequence[Decimal | None] | None = None,
    adjusted_available_at: datetime | None = None,
) -> AdjustedMarketSeries:
    raw_values = raw_closes or closes
    volume_values = volumes or [200] * len(closes)
    delivery_values = deliveries or [Decimal("0.4")] * len(closes)
    bars: list[AdjustedMarketBar] = []
    basis_date = START + timedelta(days=(len(closes) - 1) * 2) if closes else None
    for index, adjusted_close in enumerate(closes):
        raw = _raw_bar(
            index,
            raw_values[index],
            volume=volume_values[index],
            delivery=delivery_values[index],
        )
        bars.append(
            AdjustedMarketBar(
                raw_bar=raw,
                adjusted_open=adjusted_close,
                adjusted_high=adjusted_close + Decimal("1"),
                adjusted_low=max(Decimal("0"), adjusted_close - Decimal("1")),
                adjusted_close=adjusted_close,
                cumulative_price_factor=Decimal("1"),
                applied_adjustments=(),
                adjustment_basis_date=basis_date or raw.trading_date,
                as_of=AS_OF,
                available_at=adjusted_available_at or raw.available_at,
                algorithm_version=ADJUSTED_MARKET_PRICE_VERSION,
            )
        )
    return AdjustedMarketSeries(
        market_provider_dataset_id=MARKET_ID,
        corporate_action_provider_dataset_id=ACTION_ID,
        security_id=SECURITY_ID,
        interval="1d",
        adjustment_basis_date=basis_date,
        bars=tuple(bars),
        selected_actions=(),
        applicable_adjustments=(),
        as_of=AS_OF,
        algorithm_version=ADJUSTED_MARKET_PRICE_VERSION,
    )


def _benchmark_bar(
    trading_date: date,
    close: Decimal,
    *,
    provider_id: UUID = BENCHMARK_ID,
    available_at: datetime | None = None,
) -> PointInTimeBenchmarkBar:
    timestamp = available_at or datetime(2026, 3, 2, tzinfo=UTC)
    return PointInTimeBenchmarkBar(
        id=UUID(int=5000 + trading_date.toordinal()),
        benchmark_series=BenchmarkSeriesView(
            id=UUID(int=6000 + provider_id.int),
            provider_dataset_id=provider_id,
            code="NIFTY50",
            display_name="Nifty 50",
            currency="INR",
        ),
        trading_date=trading_date,
        interval="1d",
        open_value=close,
        high_value=close,
        low_value=close,
        close_value=close,
        available_at=timestamp,
        revision_at=None,
        ingested_at=timestamp,
        source_record=_source(5000 + trading_date.toordinal()),
    )


class _StaticMarketReader(PointInTimeMarketReader):
    def __init__(self, bars: dict[tuple[UUID, date], PointInTimeBenchmarkBar]) -> None:
        self._bars = bars

    def benchmark_bar_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        benchmark_code: str,
        trading_date: date,
        interval: str,
        as_of: datetime,
    ) -> PointInTimeBenchmarkBar | None:
        assert benchmark_code == "NIFTY50"
        assert interval == "1d"
        assert as_of == AS_OF
        return self._bars.get((provider_dataset_id, trading_date))


class _StaticAdjustments(MarketAdjustmentPrimitives):
    def __init__(self, series: AdjustedMarketSeries) -> None:
        self._series = series

    def adjusted_market_series_as_of(
        self,
        *,
        market_provider_dataset_id: UUID,
        corporate_action_provider_dataset_id: UUID,
        security_id: UUID,
        interval: str,
        as_of: datetime,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> AdjustedMarketSeries:
        assert market_provider_dataset_id == MARKET_ID
        assert corporate_action_provider_dataset_id == ACTION_ID
        assert security_id == SECURITY_ID
        assert interval == "1d"
        assert as_of == AS_OF
        assert start_date is None
        bars = tuple(
            bar
            for bar in self._series.bars
            if end_date is None or bar.raw_bar.trading_date <= end_date
        )
        return AdjustedMarketSeries(
            market_provider_dataset_id=MARKET_ID,
            corporate_action_provider_dataset_id=ACTION_ID,
            security_id=SECURITY_ID,
            interval="1d",
            adjustment_basis_date=bars[-1].raw_bar.trading_date if bars else None,
            bars=bars,
            selected_actions=self._series.selected_actions,
            applicable_adjustments=self._series.applicable_adjustments,
            as_of=AS_OF,
            algorithm_version=ADJUSTED_MARKET_PRICE_VERSION,
        )


def _features(
    series: AdjustedMarketSeries,
    benchmark_bars: dict[tuple[UUID, date], PointInTimeBenchmarkBar] | None = None,
    *,
    benchmark_provider_id: UUID = BENCHMARK_ID,
    market_on_or_before: date | None = None,
):
    engine = MarketStructureFeaturePrimitives(
        _StaticMarketReader(benchmark_bars or {}), _StaticAdjustments(series)
    )
    return engine.features_as_of(
        market_provider_dataset_id=MARKET_ID,
        corporate_action_provider_dataset_id=ACTION_ID,
        benchmark_provider_dataset_id=benchmark_provider_id,
        benchmark_code="NIFTY50",
        security_id=SECURITY_ID,
        interval="1d",
        as_of=AS_OF,
        market_on_or_before=market_on_or_before,
    )


def _market_structure_policy(
    mapping: dict[str, object] | None = None,
) -> InflectionScoringPolicy:
    value = mapping or json.loads(MARKET_STRUCTURE_POLICY.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return scoring_policy_from_mapping(value)


def _score_market_structure(bundle, policy: InflectionScoringPolicy | None = None):
    return MarketStructureComponentScorer().score(
        evidence=bundle,
        policy=policy or _market_structure_policy(),
    )


def _complete_bundle():
    closes = [Decimal("100") + Decimal(index % 3) for index in range(61)]
    series = _series(closes)
    start = series.bars[0].raw_bar.trading_date
    end = series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert all(getattr(bundle, code).value is not None for code in MARKET_STRUCTURE_SUBFACTOR_ORDER)
    return bundle


def _with_scored_values(bundle, values: dict[str, str | None], warning: str = "missing"):
    changes = {}
    for code, raw in values.items():
        feature = getattr(bundle, code)
        changes[code] = replace(
            feature,
            value=None if raw is None else Decimal(raw),
            warnings=(warning,) if raw is None else (),
        )
    return replace(bundle, **changes)


def test_market_structure_policy_checksum_roundtrip_and_legacy_compatibility(
    session: Session,
) -> None:
    paths_and_checksums = (
        (
            "inflection_model_v1_development.json",
            "ecd87b498d403b6bfd61397283e52954e309543900f33aa1649389676eac08bf",
        ),
        (
            "inflection_model_v1_scoring_development.json",
            "838bd138f6c236d35ed0d33ade5c15418f1919539f7f54b8f79ad44051530bc9",
        ),
        (
            "inflection_model_v1_business_quality_development.json",
            "c0a967e75aa30418ef030950032186ca6c0762f1fa67b5cf056ba06b8e8981c1",
        ),
        (
            "inflection_model_v1_cash_flow_quality_development.json",
            "d7e69165b3cba42dfebd01bd0117eba263eb76cd20bd66b5db4da3eeb37f2362",
        ),
        (
            "inflection_model_v1_balance_sheet_development.json",
            "d97590519f69dca71d26671a9266443c9beeee24e28d67d78424671d103979e6",
        ),
        (
            "inflection_model_v1_valuation_development.json",
            "fab4e33bdbcb3b108a36c7d1e4c9e7e92ccbdf0db0abe8eb085adfaff45e5632",
        ),
    )
    for filename, checksum in paths_and_checksums:
        mapping = json.loads((FIXTURES / filename).read_text(encoding="utf-8"))
        policy = scoring_policy_from_mapping(mapping)
        assert scoring_policy_checksum(policy) == checksum
        assert policy.market_structure is None
        assert "market_structure" not in policy_to_canonical_mapping(policy)
        assert '"market_structure":null' not in canonical_policy_json(policy)

    mapping = json.loads(MARKET_STRUCTURE_POLICY.read_text(encoding="utf-8"))
    policy = _market_structure_policy(mapping)
    reordered = scoring_policy_from_mapping(dict(reversed(tuple(mapping.items()))))
    changed = json.loads(json.dumps(mapping))
    changed["market_structure"]["relative_strength_60_to_benchmark_curve"]["breakpoints"][4][
        "score"
    ] = "81"
    assert scoring_policy_checksum(policy) == MARKET_STRUCTURE_POLICY_CHECKSUM
    assert scoring_policy_checksum(reordered) == MARKET_STRUCTURE_POLICY_CHECKSUM
    assert scoring_policy_checksum(scoring_policy_from_mapping(changed)) != (
        MARKET_STRUCTURE_POLICY_CHECKSUM
    )
    assert scoring_policy_from_mapping(policy_to_canonical_mapping(policy)) == policy
    assert policy.market_structure is not None
    assert policy.market_structure.minimum_weight_coverage == Decimal("0.70")
    assert policy.market_structure.subfactor_weights.relative_strength_60_to_benchmark == (
        Decimal("0.30")
    )

    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="market-structure-policy",
        semantic_version="1.0.0",
        git_sha="market-structure",
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
    assert loaded is not None
    assert loaded.policy == policy
    assert loaded.record.checksum_sha256 == MARKET_STRUCTURE_POLICY_CHECKSUM
    assert scoring_policy_checksum(loaded.policy) == MARKET_STRUCTURE_POLICY_CHECKSUM


def test_market_structure_exact_full_case() -> None:
    bundle = _with_scored_values(
        _complete_bundle(),
        {
            "relative_strength_60_to_benchmark": "0.20",
            "close_to_sma20": "0.05",
            "sma20_to_sma60": "0.10",
            "volatility_ratio_20_to_60": "0.60",
            "consolidation_range_20": "0.08",
            "close_times_volume_ratio_20_to_60": "1.50",
            "average_delivery_percentage_20": "0.60",
        },
    )
    result = _score_market_structure(bundle)
    assert result.algorithm_version == MARKET_STRUCTURE_COMPONENT_VERSION
    assert result.score == Decimal("81.0")
    assert result.weight_coverage == result.available_weight == Decimal("1")
    assert [item.code for item in result.subfactors] == list(MARKET_STRUCTURE_SUBFACTOR_ORDER)
    assert [item.raw_value for item in result.subfactors] == [
        Decimal("0.20"),
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.60"),
        Decimal("0.08"),
        Decimal("1.50"),
        Decimal("0.60"),
    ]
    assert [item.scoring_value for item in result.subfactors] == [
        Decimal("0.20"),
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("-0.60"),
        Decimal("-0.08"),
        Decimal("1.50"),
        Decimal("0.60"),
    ]
    assert [item.normalized_score for item in result.subfactors] == [
        Decimal("80"),
        Decimal("70"),
        Decimal("85"),
        Decimal("85"),
        Decimal("85"),
        Decimal("85"),
        Decimal("75"),
    ]
    assert [item.configured_weight for item in result.subfactors] == [
        Decimal("0.30"),
        Decimal("0.10"),
        Decimal("0.15"),
        Decimal("0.15"),
        Decimal("0.10"),
        Decimal("0.10"),
        Decimal("0.10"),
    ]
    assert [item.contribution for item in result.subfactors] == [
        Decimal("24"),
        Decimal("7"),
        Decimal("12.75"),
        Decimal("12.75"),
        Decimal("8.5"),
        Decimal("8.5"),
        Decimal("7.5"),
    ]
    assert [item.transform_code for item in result.subfactors] == [
        "identity",
        "identity",
        "identity",
        "negate_volatility_ratio_20_to_60",
        "negate_consolidation_range_20",
        "identity",
        "identity",
    ]
    assert all(item.normalized_raw_value is None for item in result.subfactors)
    assert all(item.normalized_raw_unit is None for item in result.subfactors)


@pytest.mark.parametrize(
    ("missing", "coverage", "scores"),
    [
        (("relative_strength_60_to_benchmark",), "0.70", True),
        (("average_delivery_percentage_20",), "0.90", True),
        (
            (
                "relative_strength_60_to_benchmark",
                "average_delivery_percentage_20",
            ),
            "0.60",
            False,
        ),
    ],
)
def test_market_structure_partial_coverage_and_warning_retention(
    missing: tuple[str, ...], coverage: str, scores: bool
) -> None:
    bundle = _complete_bundle()
    changes = {
        code: replace(
            getattr(bundle, code),
            value=None,
            warnings=(
                "missing_benchmark_start_bar"
                if code == "relative_strength_60_to_benchmark"
                else "incomplete_delivery_window",
            ),
        )
        for code in missing
    }
    result = _score_market_structure(replace(bundle, **changes))
    assert result.weight_coverage == Decimal(coverage)
    assert (result.score is not None) is scores
    assert result.missing_subfactors == missing
    assert [item.warnings for item in result.unavailable_subfactors] == [
        getattr(changes[code], "warnings") for code in missing
    ]
    if scores:
        assert sum((item.effective_weight for item in result.subfactors), Decimal("0")) == Decimal(
            "1"
        )
    else:
        assert result.warnings == ("insufficient_subfactor_coverage",)
        assert all(item.effective_weight == 0 for item in result.subfactors)


@pytest.mark.parametrize(
    ("count", "coverage", "eligible"),
    [(20, "0.30", False), (60, "0.55", False), (61, "1.00", True)],
)
def test_market_structure_real_feature_boundaries(
    count: int, coverage: str, eligible: bool
) -> None:
    series = _series([Decimal("100") + Decimal(index % 3) for index in range(count)])
    benchmarks = {}
    if count == 61:
        start = series.bars[0].raw_bar.trading_date
        end = series.bars[-1].raw_bar.trading_date
        benchmarks = {
            (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
            (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
        }
    result = _score_market_structure(_features(series, benchmarks))
    assert result.weight_coverage == Decimal(coverage)
    assert (result.score is not None) is eligible


def test_market_structure_direction_clamping_and_unscored_evidence_isolation() -> None:
    bundle = _complete_bundle()

    comparisons = (
        ("relative_strength_60_to_benchmark", "0.20", "0.00", True),
        ("sma20_to_sma60", "0.10", "0.00", True),
        ("close_times_volume_ratio_20_to_60", "1.50", "1.00", True),
        ("average_delivery_percentage_20", "0.60", "0.40", True),
        ("volatility_ratio_20_to_60", "0.60", "1.00", True),
        ("consolidation_range_20", "0.08", "0.20", True),
    )
    for code, better, worse, expected in comparisons:
        better_result = _score_market_structure(_with_scored_values(bundle, {code: better}))
        worse_result = _score_market_structure(_with_scored_values(bundle, {code: worse}))
        better_score = next(
            item.normalized_score for item in better_result.subfactors if item.code == code
        )
        worse_score = next(
            item.normalized_score for item in worse_result.subfactors if item.code == code
        )
        assert (better_score > worse_score) is expected

    high = _score_market_structure(
        _with_scored_values(bundle, {"relative_strength_60_to_benchmark": "100"})
    )
    low = _score_market_structure(
        _with_scored_values(bundle, {"relative_strength_60_to_benchmark": "-1"})
    )
    assert next(
        item.normalized_score for item in high.subfactors if item.code.startswith("relative")
    ) == Decimal("100")
    assert next(
        item.normalized_score for item in low.subfactors if item.code.startswith("relative")
    ) == Decimal("0")

    baseline = _score_market_structure(bundle)
    changed_supporting = replace(
        bundle,
        average_close_times_volume_20_inr=replace(
            bundle.average_close_times_volume_20_inr,
            value=Decimal("999999999999"),
        ),
        return_volatility_20=replace(bundle.return_volatility_20, value=Decimal("99")),
        return_volatility_60=replace(bundle.return_volatility_60, value=Decimal("0.0001")),
    )
    changed_result = _score_market_structure(changed_supporting)
    assert changed_result.score == baseline.score
    assert changed_result.weight_coverage == baseline.weight_coverage
    assert changed_result.available_at == baseline.available_at
    assert changed_result.missing_subfactors == baseline.missing_subfactors


def test_market_structure_zero_weight_and_top_level_weight_separation() -> None:
    mapping = json.loads(MARKET_STRUCTURE_POLICY.read_text(encoding="utf-8"))
    weights = mapping["market_structure"]["subfactor_weights"]
    weights["relative_strength_60_to_benchmark"] = "0"
    weights["close_to_sma20"] = "0.40"
    zero_policy = _market_structure_policy(mapping)
    bundle = _complete_bundle()
    late = replace(bundle.relative_strength_60_to_benchmark, available_at=AS_OF)
    result = _score_market_structure(
        replace(bundle, relative_strength_60_to_benchmark=late), zero_policy
    )
    assert "relative_strength_60_to_benchmark" not in {item.code for item in result.subfactors}
    assert "relative_strength_60_to_benchmark" not in result.missing_subfactors
    assert result.available_at is not None
    assert result.available_at < AS_OF

    changed = json.loads(MARKET_STRUCTURE_POLICY.read_text(encoding="utf-8"))
    changed["component_weights"]["market_structure"] = "0.20"
    changed["component_weights"]["business_catalyst"] = "0.05"
    assert (
        _score_market_structure(bundle).score
        == _score_market_structure(bundle, _market_structure_policy(changed)).score
    )


def test_market_structure_feature_contracts_and_domains() -> None:
    bundle = _complete_bundle()
    code = "relative_strength_60_to_benchmark"
    feature = getattr(bundle, code)
    mutations = (
        (replace(feature, code="close_to_sma20"), "wrong code"),
        (replace(feature, algorithm_version="relative_strength_v2"), "feature version"),
        (replace(feature, unit="percent"), "unit must be ratio"),
        (replace(feature, value=Decimal("-1.01")), "semantic domain"),
        (replace(feature, warnings=("contradiction",)), "must not carry warnings"),
        (replace(feature, available_at=None), "requires available_at"),
        (
            replace(feature, available_at=AS_OF + timedelta(seconds=1)),
            "exceeds bundle cutoff",
        ),
        (replace(feature, as_of=AS_OF - timedelta(seconds=1)), "does not match"),
        (replace(feature, value=None, warnings=()), "requires warnings"),
    )
    for mutation, message in mutations:
        with pytest.raises(ValueError, match=message):
            _score_market_structure(replace(bundle, **{code: mutation}))

    delivery = bundle.average_delivery_percentage_20
    with pytest.raises(ValueError, match="semantic domain"):
        _score_market_structure(
            replace(
                bundle,
                average_delivery_percentage_20=replace(delivery, value=Decimal("1.01")),
            )
        )
    with pytest.raises(ValueError, match="bundle version"):
        _score_market_structure(
            replace(bundle, algorithm_version="market_structure_feature_bundle_v2")
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        _score_market_structure(replace(bundle, as_of=AS_OF.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="benchmark_code"):
        _score_market_structure(replace(bundle, benchmark_code=" "))
    with pytest.raises(ValueError, match="interval"):
        _score_market_structure(replace(bundle, interval=""))
    with pytest.raises(ValueError, match="basis_date exceeds"):
        assert bundle.basis_date is not None
        _score_market_structure(
            replace(bundle, market_on_or_before=bundle.basis_date - timedelta(days=1))
        )

    equivalent = replace(
        bundle,
        as_of=bundle.as_of.astimezone(timezone(timedelta(hours=5, minutes=30))),
    )
    assert _score_market_structure(equivalent).as_of == bundle.as_of


def test_market_structure_nested_identity_cutoff_basis_and_lineage_validation() -> None:
    bundle = _complete_bundle()

    relative = bundle.relative_strength_60_to_benchmark
    relative_evidence = relative.evidence
    assert isinstance(relative_evidence, RelativeStrengthEvidence)
    benchmark_end = relative_evidence.benchmark_end
    assert benchmark_end is not None
    wrong_series = replace(benchmark_end.benchmark_series, provider_dataset_id=UUID(int=999))
    wrong_benchmark = replace(benchmark_end, benchmark_series=wrong_series)
    with pytest.raises(ValueError, match="benchmark evidence"):
        _score_market_structure(
            replace(
                bundle,
                relative_strength_60_to_benchmark=replace(
                    relative,
                    evidence=replace(relative_evidence, benchmark_end=wrong_benchmark),
                ),
            )
        )

    close_feature = bundle.close_to_sma20
    moving = close_feature.evidence
    assert isinstance(moving, MovingAverageEvidence)
    last_adjusted = moving.bars[-1]
    wrong_raw = replace(last_adjusted.raw_bar, provider_dataset_id=UUID(int=998))
    wrong_adjusted = replace(last_adjusted, raw_bar=wrong_raw)
    with pytest.raises(ValueError, match="market evidence"):
        _score_market_structure(
            replace(
                bundle,
                close_to_sma20=replace(
                    close_feature,
                    evidence=replace(moving, bars=(*moving.bars[:-1], wrong_adjusted)),
                ),
            )
        )

    wrong_source = replace(last_adjusted.raw_bar.source_record, validation_status="rejected")
    rejected_raw = replace(last_adjusted.raw_bar, source_record=wrong_source)
    rejected_adjusted = replace(last_adjusted, raw_bar=rejected_raw)
    with pytest.raises(ValueError, match="source lineage"):
        _score_market_structure(
            replace(
                bundle,
                close_to_sma20=replace(
                    close_feature,
                    evidence=replace(moving, bars=(*moving.bars[:-1], rejected_adjusted)),
                ),
            )
        )

    volatility = bundle.volatility_ratio_20_to_60
    ratio_evidence = volatility.evidence
    assert isinstance(ratio_evidence, VolatilityRatioEvidence)
    wrong_short = replace(ratio_evidence.short_volatility, code="return_volatility_60")
    with pytest.raises(ValueError, match="unsupported semantics"):
        _score_market_structure(
            replace(
                bundle,
                volatility_ratio_20_to_60=replace(
                    volatility,
                    evidence=replace(ratio_evidence, short_volatility=wrong_short),
                ),
            )
        )

    with pytest.raises(ValueError, match="basis_date"):
        assert bundle.basis_date is not None
        _score_market_structure(
            replace(
                bundle,
                close_to_sma20=replace(
                    close_feature,
                    evidence=replace(
                        moving,
                        basis_date=bundle.basis_date - timedelta(days=1),
                    ),
                ),
            )
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"minimum_weight_coverage": "0"},
        {"minimum_weight_coverage": "1.01"},
        {"minimum_weight_coverage": 0.7},
        {
            "subfactor_weights": {
                "relative_strength_60_to_benchmark": "-0.10",
                "close_to_sma20": "0.40",
                "sma20_to_sma60": "0.15",
                "volatility_ratio_20_to_60": "0.15",
                "consolidation_range_20": "0.10",
                "close_times_volume_ratio_20_to_60": "0.10",
                "average_delivery_percentage_20": "0.10",
            }
        },
        {"subfactor_weights": {code: "0.10" for code in MARKET_STRUCTURE_SUBFACTOR_ORDER}},
    ],
)
def test_market_structure_policy_rejects_invalid_exact_decimal_contracts(
    mutation: dict[str, object],
) -> None:
    mapping = json.loads(MARKET_STRUCTURE_POLICY.read_text(encoding="utf-8"))
    mapping["market_structure"].update(mutation)
    with pytest.raises(ValueError):
        scoring_policy_from_mapping(mapping)


@pytest.mark.parametrize(
    ("count", "available_codes"),
    [
        (0, set()),
        (1, set()),
        (19, set()),
        (
            20,
            {
                "close_to_sma20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "average_delivery_percentage_20",
            },
        ),
        (
            21,
            {
                "close_to_sma20",
                "return_volatility_20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "average_delivery_percentage_20",
            },
        ),
        (
            59,
            {
                "close_to_sma20",
                "return_volatility_20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "average_delivery_percentage_20",
            },
        ),
        (
            60,
            {
                "close_to_sma20",
                "sma20_to_sma60",
                "return_volatility_20",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "close_times_volume_ratio_20_to_60",
                "average_delivery_percentage_20",
            },
        ),
        (
            61,
            {
                "relative_strength_60_to_benchmark",
                "close_to_sma20",
                "sma20_to_sma60",
                "return_volatility_20",
                "return_volatility_60",
                "consolidation_range_20",
                "average_close_times_volume_20_inr",
                "close_times_volume_ratio_20_to_60",
                "average_delivery_percentage_20",
            },
        ),
    ],
)
def test_observation_window_thresholds(count: int, available_codes: set[str]) -> None:
    series = _series([Decimal("100")] * count)
    benchmarks: dict[tuple[UUID, date], PointInTimeBenchmarkBar] = {}
    if count >= 61:
        start, end = series.bars[-61], series.bars[-1]
        benchmarks = {
            (BENCHMARK_ID, start.raw_bar.trading_date): _benchmark_bar(
                start.raw_bar.trading_date, Decimal("100")
            ),
            (BENCHMARK_ID, end.raw_bar.trading_date): _benchmark_bar(
                end.raw_bar.trading_date, Decimal("100")
            ),
        }
    bundle = _features(series, benchmarks)
    values = (
        bundle.relative_strength_60_to_benchmark,
        bundle.close_to_sma20,
        bundle.sma20_to_sma60,
        bundle.return_volatility_20,
        bundle.return_volatility_60,
        bundle.volatility_ratio_20_to_60,
        bundle.consolidation_range_20,
        bundle.average_close_times_volume_20_inr,
        bundle.close_times_volume_ratio_20_to_60,
        bundle.average_delivery_percentage_20,
    )
    assert {item.code for item in values if item.value is not None} == available_codes
    assert bundle.basis_date == (series.bars[-1].raw_bar.trading_date if count else None)


def test_trend_consolidation_activity_delivery_and_missing_dates_are_exact() -> None:
    closes = [Decimal(index) for index in range(1, 62)]
    bundle = _features(_series(closes))

    assert bundle.close_to_sma20.value == Decimal("61") / Decimal("51.5") - Decimal("1")
    assert bundle.sma20_to_sma60.value == Decimal("51.5") / Decimal("31.5") - Decimal("1")
    expected_range = Decimal("21") / Decimal("51.5")
    assert bundle.consolidation_range_20.value == expected_range
    assert bundle.average_delivery_percentage_20.value == Decimal("0.4")
    moving_average_evidence = bundle.close_to_sma20.evidence
    assert isinstance(moving_average_evidence, MovingAverageEvidence)
    assert all(
        later.raw_bar.trading_date - earlier.raw_bar.trading_date == timedelta(days=2)
        for earlier, later in zip(
            moving_average_evidence.bars,
            moving_average_evidence.bars[1:],
            strict=False,
        )
    )


def test_volatility_uses_population_decimal_stddev_and_invalid_returns_fail_closed() -> None:
    closes = [Decimal("1") if index % 2 == 0 else Decimal("2") for index in range(61)]
    bundle = _features(_series(closes))
    assert bundle.return_volatility_20.value == Decimal("0.75")
    assert bundle.return_volatility_60.value == Decimal("0.75")
    assert bundle.volatility_ratio_20_to_60.value == Decimal("1")
    assert _features(_series(closes)).return_volatility_60.value == Decimal("0.75")

    invalid = closes.copy()
    invalid[-10] = Decimal("0")
    invalid_bundle = _features(_series(invalid))
    assert invalid_bundle.return_volatility_20.value is None
    assert invalid_bundle.return_volatility_20.warnings == ("invalid_return_in_window",)


def test_flat_series_relative_strength_and_zero_volatility_ratio() -> None:
    series = _series([Decimal("100")] * 61)
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert bundle.relative_strength_60_to_benchmark.value == Decimal("0")
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value == Decimal("0")
    assert bundle.return_volatility_20.value == Decimal("0")
    assert bundle.return_volatility_60.value == Decimal("0")
    assert bundle.volatility_ratio_20_to_60.value is None
    assert bundle.volatility_ratio_20_to_60.warnings == ("zero_medium_volatility",)
    assert bundle.consolidation_range_20.value == Decimal("0.02")


def test_relative_strength_uses_exact_endpoints_and_explicit_provider() -> None:
    series = _series([Decimal("100")] * 60 + [Decimal("110")])
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    other_provider = UUID(int=999)
    bars = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("105")),
        (other_provider, start): _benchmark_bar(start, Decimal("100"), provider_id=other_provider),
        (other_provider, end): _benchmark_bar(end, Decimal("110"), provider_id=other_provider),
    }
    expected = Decimal("1.1") / Decimal("1.05") - Decimal("1")
    assert _features(series, bars).relative_strength_60_to_benchmark.value == expected
    assert _features(
        series, bars, benchmark_provider_id=other_provider
    ).relative_strength_60_to_benchmark.value == Decimal("0")
    missing_start = {(BENCHMARK_ID, end): bars[(BENCHMARK_ID, end)]}
    missing_end = {(BENCHMARK_ID, start): bars[(BENCHMARK_ID, start)]}
    assert _features(series, missing_start).relative_strength_60_to_benchmark.warnings == (
        "missing_benchmark_start_bar",
    )
    assert _features(series, missing_end).relative_strength_60_to_benchmark.warnings == (
        "missing_benchmark_end_bar",
    )
    underperforming = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("120")),
    }
    underperformance = _features(series, underperforming).relative_strength_60_to_benchmark.value
    assert underperformance is not None and underperformance < 0


def test_zero_price_boundaries_fail_closed_only_when_used_as_denominators() -> None:
    closes = [Decimal("1")] * 60 + [Decimal("0")]
    series = _series(closes)
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert bundle.close_to_sma20.value == Decimal("-1")
    assert bundle.relative_strength_60_to_benchmark.value == Decimal("-1")

    all_zero = _features(_series([Decimal("0")] * 61), benchmarks)
    assert all_zero.close_to_sma20.warnings == ("non_positive_sma20",)
    assert all_zero.sma20_to_sma60.warnings == ("non_positive_sma60",)
    assert all_zero.consolidation_range_20.warnings == ("non_positive_consolidation_mean_close",)
    assert all_zero.close_times_volume_ratio_20_to_60.warnings == ("zero_medium_activity_proxy",)


def test_bonus_neutralized_series_does_not_create_false_price_structure() -> None:
    adjusted = [Decimal("100")] * 61
    raw = [Decimal("150")] * 30 + [Decimal("100")] * 31
    series = _series(adjusted, raw_closes=raw)
    start, end = series.bars[0].raw_bar.trading_date, series.bars[-1].raw_bar.trading_date
    benchmarks = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    bundle = _features(series, benchmarks)
    assert bundle.relative_strength_60_to_benchmark.value == Decimal("0")
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value == Decimal("0")
    assert bundle.return_volatility_60.value == Decimal("0")


def test_activity_uses_raw_close_times_raw_volume_and_delivery_requires_complete_window() -> None:
    adjusted = [Decimal("100")] * 60
    raw = [Decimal("200")] * 30 + [Decimal("100")] * 30
    volumes = [100] * 30 + [200] * 30
    bundle = _features(_series(adjusted, raw_closes=raw, volumes=volumes))
    assert bundle.average_close_times_volume_20_inr.value == Decimal("20000")
    assert bundle.close_times_volume_ratio_20_to_60.value == Decimal("1")

    deliveries: list[Decimal | None] = [Decimal("0.4")] * 60
    deliveries[-1] = None
    missing = _features(_series(adjusted, deliveries=deliveries))
    assert missing.average_delivery_percentage_20.value is None
    assert missing.average_delivery_percentage_20.warnings == ("incomplete_delivery_window",)


def test_action_and_benchmark_corrections_only_move_dependent_features() -> None:
    raw = [Decimal("100")] * 61
    base = _series(raw)
    start, end = base.bars[0].raw_bar.trading_date, base.bars[-1].raw_bar.trading_date
    benchmark_a = {
        (BENCHMARK_ID, start): _benchmark_bar(start, Decimal("100")),
        (BENCHMARK_ID, end): _benchmark_bar(end, Decimal("100")),
    }
    benchmark_b = {
        **benchmark_a,
        (BENCHMARK_ID, end): _benchmark_bar(
            end, Decimal("110"), available_at=datetime(2026, 3, 15, tzinfo=UTC)
        ),
    }
    first = _features(base, benchmark_a)
    benchmark_corrected = _features(base, benchmark_b)
    assert first.relative_strength_60_to_benchmark.value != (
        benchmark_corrected.relative_strength_60_to_benchmark.value
    )
    assert first.close_to_sma20 == benchmark_corrected.close_to_sma20
    assert first.average_close_times_volume_20_inr == (
        benchmark_corrected.average_close_times_volume_20_inr
    )

    adjusted_change = [Decimal("50")] * 30 + [Decimal("100")] * 31
    action_corrected = _features(
        _series(
            adjusted_change,
            raw_closes=raw,
            adjusted_available_at=datetime(2026, 3, 20, tzinfo=UTC),
        ),
        benchmark_a,
    )
    assert action_corrected.sma20_to_sma60.value != first.sma20_to_sma60.value
    assert action_corrected.return_volatility_60.value != first.return_volatility_60.value
    assert action_corrected.average_close_times_volume_20_inr.value == (
        first.average_close_times_volume_20_inr.value
    )
    assert action_corrected.average_delivery_percentage_20.value == (
        first.average_delivery_percentage_20.value
    )
    assert action_corrected.sma20_to_sma60.available_at == datetime(2026, 3, 20, tzinfo=UTC)
    assert action_corrected.average_close_times_volume_20_inr.available_at == datetime(
        2026, 3, 1, tzinfo=UTC
    )


def test_market_on_or_before_uses_historical_economic_basis_and_utc_cutoff() -> None:
    series = _series([Decimal("100")] * 61)
    bound = series.bars[19].raw_bar.trading_date
    bundle = _features(series, market_on_or_before=bound)
    assert bundle.basis_date == bound
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value is None

    engine = MarketStructureFeaturePrimitives(_StaticMarketReader({}), _StaticAdjustments(series))
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.features_as_of(
            market_provider_dataset_id=MARKET_ID,
            corporate_action_provider_dataset_id=ACTION_ID,
            benchmark_provider_dataset_id=BENCHMARK_ID,
            benchmark_code="NIFTY50",
            security_id=SECURITY_ID,
            interval="1d",
            as_of=datetime(2026, 4, 1, 12),
        )


UNIVERSE = ProviderMetadata("structure_universe", "csv", "universe", "synthetic")
MARKET = ProviderMetadata("structure_market", "mock", "market_daily", "synthetic")
ACTIONS = ProviderMetadata("structure_actions", "mock", "corporate_actions", "synthetic")
BENCHMARK = ProviderMetadata("structure_benchmark", "mock", "benchmark_daily", "synthetic")
ISIN = "INF0AUR01018"


def _batch[TRecord: (MarketBarRecord, BenchmarkBarRecord, CorporateActionRecord)](
    records: Sequence[TRecord],
    metadata: ProviderMetadata,
    prefix: str,
    *,
    available_at: datetime = datetime(2026, 3, 1, tzinfo=UTC),
    external_ids: Sequence[str] | None = None,
) -> ProviderBatch[TRecord]:
    rows = [f"{prefix}-{index}:{record!r}" for index, record in enumerate(records)]
    envelopes = tuple(
        IngestionEnvelope(
            provider=metadata,
            external_record_id=(
                external_ids[index] if external_ids is not None else f"{prefix}-{index}"
            ),
            source_uri=f"fixture://{prefix}",
            raw_payload_reference=f"row-{index}",
            content_sha256=sha256(rows[index].encode()).hexdigest(),
            retrieved_at=datetime(2026, 3, 1, tzinfo=UTC),
            record=record,
            available_at=available_at,
        )
        for index, record in enumerate(records)
    )
    return ProviderBatch(
        provider=metadata,
        source_uri=f"fixture://{prefix}",
        raw_payload="\n".join(rows).encode(),
        retrieved_at=datetime(2026, 3, 1, tzinfo=UTC),
        records=envelopes,
    )


def _dataset_id(session: Session, metadata: ProviderMetadata) -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(
            DataProvider.code == metadata.provider_code,
            ProviderDataset.code == metadata.dataset_code,
        )
    )
    assert value is not None
    return value


def test_full_real_ingestion_split_neutralized_feature_lineage(
    session: Session, tmp_path: Path
) -> None:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_synthetic.csv",
            UNIVERSE,
            datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    security_id = session.scalar(select(Security.id).where(Security.isin == ISIN))
    assert security_id is not None
    market_records: list[MarketBarRecord] = []
    benchmark_records: list[BenchmarkBarRecord] = []
    for index in range(61):
        trading_date = START + timedelta(days=index)
        pre_split = index < 30
        price = Decimal("200") if pre_split else Decimal("100")
        volume = 100 if pre_split else 200
        market_records.append(
            MarketBarRecord(
                security_isin=ISIN,
                trading_date=trading_date,
                interval="1d",
                open_price=price,
                high_price=price,
                low_price=price,
                close_price=price,
                volume=volume,
                market_cap=None,
                delivery_quantity=None,
                delivery_percentage=Decimal("0.4"),
            )
        )
        benchmark_records.append(
            BenchmarkBarRecord(
                benchmark_code="NIFTY50",
                benchmark_display_name="Nifty 50",
                currency="INR",
                trading_date=trading_date,
                interval="1d",
                open_value=Decimal("100"),
                high_value=Decimal("100"),
                low_value=Decimal("100"),
                close_value=Decimal("100"),
            )
        )
    action = CorporateActionRecord(
        security_isin=ISIN,
        action_type="split",
        announcement_date=date(2026, 1, 20),
        ex_date=None,
        record_date=None,
        effective_date=START + timedelta(days=30),
        ratio_numerator=2,
        ratio_denominator=1,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
        subscription_price=None,
        subscription_currency=None,
        exchange=None,
        old_symbol=None,
        new_symbol=None,
        successor_isin=None,
    )
    service.ingest_market_data(MockMarketDataProvider(_batch(market_records, MARKET, "market")))
    service.ingest_corporate_actions(
        MockCorporateActionProvider(_batch([action], ACTIONS, "actions"))
    )
    service.ingest_benchmark_data(
        MockBenchmarkDataProvider(_batch(benchmark_records, BENCHMARK, "benchmark"))
    )
    market_reader = PointInTimeMarketReader(session)
    adjustments = MarketAdjustmentPrimitives(
        market_reader, PointInTimeCorporateActionReader(session)
    )
    engine = MarketStructureFeaturePrimitives(market_reader, adjustments)

    def read(cutoff: datetime):
        return engine.features_as_of(
            market_provider_dataset_id=_dataset_id(session, MARKET),
            corporate_action_provider_dataset_id=_dataset_id(session, ACTIONS),
            benchmark_provider_dataset_id=_dataset_id(session, BENCHMARK),
            benchmark_code="NIFTY50",
            security_id=security_id,
            interval="1d",
            as_of=cutoff,
        )

    baseline_cutoff = datetime(2026, 3, 5, tzinfo=UTC)
    bundle = read(baseline_cutoff)
    baseline_score = _score_market_structure(bundle)

    assert bundle.relative_strength_60_to_benchmark.value == Decimal("0")
    assert bundle.close_to_sma20.value == Decimal("0")
    assert bundle.sma20_to_sma60.value == Decimal("0")
    assert bundle.return_volatility_20.value == Decimal("0")
    assert bundle.return_volatility_60.value == Decimal("0")
    assert bundle.average_close_times_volume_20_inr.value == Decimal("20000")
    assert bundle.close_times_volume_ratio_20_to_60.value == Decimal("1")
    assert bundle.average_delivery_percentage_20.value == Decimal("0.4")
    assert baseline_score.score is not None
    assert baseline_score.weight_coverage == Decimal("0.85")
    assert baseline_score.missing_subfactors == ("volatility_ratio_20_to_60",)
    relative_evidence = bundle.relative_strength_60_to_benchmark.evidence
    assert isinstance(relative_evidence, RelativeStrengthEvidence)
    assert relative_evidence.security_start.raw_bar.source_record.raw_object_key
    action_source = relative_evidence.security_start.applied_adjustments[0].action.source_record
    assert action_source.raw_object_key
    assert relative_evidence.benchmark_start is not None
    assert relative_evidence.benchmark_start.source_record.raw_object_key

    corrected_action = CorporateActionRecord(
        security_isin=ISIN,
        action_type="split",
        announcement_date=date(2026, 1, 20),
        ex_date=None,
        record_date=None,
        effective_date=START + timedelta(days=30),
        ratio_numerator=4,
        ratio_denominator=1,
        cash_amount=None,
        cash_currency=None,
        cash_unit=None,
        subscription_price=None,
        subscription_currency=None,
        exchange=None,
        old_symbol=None,
        new_symbol=None,
        successor_isin=None,
    )
    action_cutoff = datetime(2026, 3, 10, tzinfo=UTC)
    service.ingest_corporate_actions(
        MockCorporateActionProvider(
            _batch(
                [corrected_action],
                ACTIONS,
                "actions-corrected",
                available_at=action_cutoff,
                external_ids=["actions-0"],
            )
        )
    )
    action_corrected = read(action_cutoff)
    action_score = _score_market_structure(action_corrected)
    action_relative = action_corrected.relative_strength_60_to_benchmark
    action_relative_evidence = action_relative.evidence
    assert isinstance(action_relative_evidence, RelativeStrengthEvidence)
    applied = action_relative_evidence.security_start.applied_adjustments
    assert applied
    wrong_action = replace(applied[0].action, provider_dataset_id=UUID(int=997))
    wrong_adjustment = replace(applied[0], action=wrong_action)
    wrong_start = replace(
        action_relative_evidence.security_start,
        applied_adjustments=(wrong_adjustment, *applied[1:]),
    )
    with pytest.raises(ValueError, match="corporate-action evidence"):
        _score_market_structure(
            replace(
                action_corrected,
                relative_strength_60_to_benchmark=replace(
                    action_relative,
                    evidence=replace(
                        action_relative_evidence,
                        security_start=wrong_start,
                    ),
                ),
            )
        )
    assert action_corrected.sma20_to_sma60.value != bundle.sma20_to_sma60.value
    assert action_corrected.return_volatility_60.value != bundle.return_volatility_60.value
    assert action_corrected.relative_strength_60_to_benchmark.value != (
        bundle.relative_strength_60_to_benchmark.value
    )
    assert action_corrected.average_close_times_volume_20_inr.value == (
        bundle.average_close_times_volume_20_inr.value
    )
    assert action_corrected.average_delivery_percentage_20.value == (
        bundle.average_delivery_percentage_20.value
    )
    action_audit = {item.code: item for item in action_score.subfactors}
    baseline_audit = {item.code: item for item in baseline_score.subfactors}
    for code in (
        "close_times_volume_ratio_20_to_60",
        "average_delivery_percentage_20",
    ):
        assert action_audit[code].raw_value == baseline_audit[code].raw_value
        assert action_audit[code].scoring_value == baseline_audit[code].scoring_value
    assert action_corrected.sma20_to_sma60.available_at == action_cutoff
    assert action_corrected.average_close_times_volume_20_inr.available_at == datetime(
        2026, 3, 1, tzinfo=UTC
    )

    benchmark_cutoff = datetime(2026, 3, 20, tzinfo=UTC)
    corrected_benchmark = BenchmarkBarRecord(
        benchmark_code="NIFTY50",
        benchmark_display_name="Nifty 50",
        currency="INR",
        trading_date=START + timedelta(days=60),
        interval="1d",
        open_value=Decimal("110"),
        high_value=Decimal("110"),
        low_value=Decimal("110"),
        close_value=Decimal("110"),
    )
    service.ingest_benchmark_data(
        MockBenchmarkDataProvider(
            _batch(
                [corrected_benchmark],
                BENCHMARK,
                "benchmark-corrected",
                available_at=benchmark_cutoff,
                external_ids=["benchmark-60"],
            )
        )
    )
    benchmark_corrected = read(benchmark_cutoff)
    benchmark_score = _score_market_structure(benchmark_corrected)
    assert benchmark_corrected.relative_strength_60_to_benchmark.value != (
        action_corrected.relative_strength_60_to_benchmark.value
    )
    for code in (
        "close_to_sma20",
        "sma20_to_sma60",
        "return_volatility_20",
        "return_volatility_60",
        "consolidation_range_20",
        "average_close_times_volume_20_inr",
        "close_times_volume_ratio_20_to_60",
        "average_delivery_percentage_20",
    ):
        assert getattr(benchmark_corrected, code).value == getattr(action_corrected, code).value
    benchmark_audit = {item.code: item for item in benchmark_score.subfactors}
    for code in MARKET_STRUCTURE_SUBFACTOR_ORDER:
        if code == "relative_strength_60_to_benchmark":
            assert benchmark_audit[code].raw_value != action_audit[code].raw_value
        else:
            assert benchmark_audit[code].raw_value == action_audit[code].raw_value
            assert benchmark_audit[code].scoring_value == action_audit[code].scoring_value

    market_cutoff = datetime(2026, 3, 30, tzinfo=UTC)
    corrected_market = MarketBarRecord(
        security_isin=ISIN,
        trading_date=START + timedelta(days=60),
        interval="1d",
        open_price=Decimal("110"),
        high_price=Decimal("110"),
        low_price=Decimal("110"),
        close_price=Decimal("110"),
        volume=200,
        market_cap=None,
        delivery_quantity=None,
        delivery_percentage=Decimal("0.6"),
    )
    service.ingest_market_data(
        MockMarketDataProvider(
            _batch(
                [corrected_market],
                MARKET,
                "market-corrected",
                available_at=market_cutoff,
                external_ids=["market-60"],
            )
        )
    )
    market_corrected = read(market_cutoff)
    market_score = _score_market_structure(market_corrected)
    assert market_corrected.close_to_sma20.value != benchmark_corrected.close_to_sma20.value
    assert market_corrected.average_close_times_volume_20_inr.value != (
        benchmark_corrected.average_close_times_volume_20_inr.value
    )
    assert market_corrected.average_delivery_percentage_20.value != (
        benchmark_corrected.average_delivery_percentage_20.value
    )
    assert market_score.score != benchmark_score.score

    delivery_cutoff = datetime(2026, 3, 31, tzinfo=UTC)
    delivery_corrected_market = replace(
        corrected_market,
        delivery_percentage=Decimal("0.8"),
    )
    service.ingest_market_data(
        MockMarketDataProvider(
            _batch(
                [delivery_corrected_market],
                MARKET,
                "delivery-corrected",
                available_at=delivery_cutoff,
                external_ids=["market-60"],
            )
        )
    )
    delivery_corrected = read(delivery_cutoff)
    delivery_score = _score_market_structure(delivery_corrected)
    market_audit = {item.code: item for item in market_score.subfactors}
    delivery_audit = {item.code: item for item in delivery_score.subfactors}
    for code in MARKET_STRUCTURE_SUBFACTOR_ORDER:
        if code == "average_delivery_percentage_20":
            assert delivery_audit[code].raw_value != market_audit[code].raw_value
        else:
            assert delivery_audit[code].raw_value == market_audit[code].raw_value
            assert delivery_audit[code].scoring_value == market_audit[code].scoring_value

    historical_rerun = read(baseline_cutoff)
    historical_score = _score_market_structure(historical_rerun)
    assert historical_rerun.relative_strength_60_to_benchmark.value == (
        bundle.relative_strength_60_to_benchmark.value
    )
    assert historical_rerun.sma20_to_sma60.value == bundle.sma20_to_sma60.value
    assert historical_rerun.average_close_times_volume_20_inr.value == (
        bundle.average_close_times_volume_20_inr.value
    )
    assert historical_rerun.average_delivery_percentage_20.value == (
        bundle.average_delivery_percentage_20.value
    )
    assert historical_score.score == baseline_score.score
    assert historical_score.subfactors == baseline_score.subfactors
