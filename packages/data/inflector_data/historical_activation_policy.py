"""Strict versioned policies for Production S1 activation and factual analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from inflector_data.backtest_policy import load_backtest_policy
from inflector_data.historical_evaluation_policy import (
    canonical_json_sha256,
    load_historical_universe_policy,
    load_multibagger_evaluation_policy,
    load_multibagger_outcome_policy,
)


@dataclass(frozen=True, slots=True)
class HistoricalEvaluationActivationPolicy:
    code: str
    version: str
    checksum_sha256: str
    source_provider_code: str
    mii_dataset_code: str
    mii_filename_pattern: str
    official_session_source_dataset_code: str
    benchmark_dataset_code: str
    benchmark_code: str
    calendar_verification_algorithm: str
    artifact_verification_semantics: str
    maximum_mii_decompressed_bytes: int
    inherited_checksums: dict[str, str]


@dataclass(frozen=True, slots=True)
class MultibaggerTemporalSplitPolicy:
    code: str
    version: str
    checksum_sha256: str
    source_evaluation_policy_checksum_sha256: str
    activation_policy_code: str
    activation_policy_checksum_sha256: str
    longest_horizon_calendar_years: int
    development_percent: int
    validation_percent: int
    holdout_percent: int
    minimum_validation_cutoffs: int
    minimum_holdout_cutoffs: int
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class MultibaggerErrorAnalysisPolicy:
    code: str
    version: str
    checksum_sha256: str
    source_evaluation_policy_checksum_sha256: str
    allowed_split_codes: tuple[str, ...]
    confusion_classes: tuple[str, ...]
    dimensions: tuple[str, ...]
    error_signatures: tuple[str, ...]
    algorithm_version: str


def load_historical_evaluation_activation_policy(
    path: Path, *, repository_root: Path
) -> HistoricalEvaluationActivationPolicy:
    value = _object(path)
    expected = {
        "activation_policy_code",
        "policy_version",
        "source_provider_code",
        "mii_dataset_code",
        "mii_filename_pattern",
        "official_session_source_dataset_code",
        "benchmark_dataset_code",
        "benchmark_code",
        "research_profile_code",
        "research_profile_checksum_sha256",
        "backtest_policy_code",
        "backtest_policy_checksum_sha256",
        "historical_universe_policy_code",
        "historical_universe_policy_checksum_sha256",
        "multibagger_outcome_policy_code",
        "multibagger_outcome_policy_checksum_sha256",
        "multibagger_evaluation_policy_code",
        "multibagger_evaluation_policy_checksum_sha256",
        "artifact_verification_semantics",
        "calendar_verification_algorithm",
        "current_status_filtering",
        "historical_available_at_semantics",
        "raw_archival_requirement",
        "maximum_mii_decompressed_bytes",
    }
    _exact_fields(value, expected, "activation policy")
    exact = {
        "research": "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1",
        "backtest": "e1f1f886f686045f7f2774be355674a726b155db1a32c9080999c32becd35984",
        "universe": "ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982",
        "outcomes": "3c901049e37dfa1764d70feb7e8fdfa8762b3c4733ea283f8b6d99c73475332f",
        "evaluation": "121236d373fc7a6c7b7fe53881217ddce2cf607125f4ca20c3e71c3d128d3a11",
    }
    bindings = {
        "research": _string(value, "research_profile_checksum_sha256"),
        "backtest": _string(value, "backtest_policy_checksum_sha256"),
        "universe": _string(value, "historical_universe_policy_checksum_sha256"),
        "outcomes": _string(value, "multibagger_outcome_policy_checksum_sha256"),
        "evaluation": _string(value, "multibagger_evaluation_policy_checksum_sha256"),
    }
    if bindings != exact:
        raise ValueError("activation policy inherited checksum binding mismatch")
    root = repository_root.resolve()
    research = _object(root / "config/research/production_research_v4.json")
    backtest = load_backtest_policy(
        root / "config/backtest/production_backtest_v1.json", repository_root=root
    )
    universe = load_historical_universe_policy(
        root / "config/backtest/production_historical_universe_v1.json"
    )
    outcomes = load_multibagger_outcome_policy(
        root / "config/backtest/production_multibagger_outcomes_v1.json",
        repository_root=root,
    )
    evaluation = load_multibagger_evaluation_policy(
        root / "config/backtest/production_multibagger_evaluation_v1.json",
        repository_root=root,
    )
    actual = {
        "research": canonical_json_sha256(research),
        "backtest": backtest.checksum_sha256,
        "universe": universe.checksum_sha256,
        "outcomes": outcomes.checksum_sha256,
        "evaluation": evaluation.checksum_sha256,
    }
    if actual != exact:
        raise ValueError("activation policy bound asset checksum mismatch")
    if (
        _string(value, "activation_policy_code") != "production_historical_evaluation_activation_v1"
        or _string(value, "policy_version") != "1"
        or _string(value, "source_provider_code") != "nse_official"
        or _string(value, "mii_dataset_code") != "nse_cm_mii_security_daily"
        or _string(value, "mii_filename_pattern") != "NSE_CM_security_DDMMYYYY.csv.gz"
        or _string(value, "official_session_source_dataset_code")
        != "nse_cm_official_session_calendar"
        or _string(value, "benchmark_dataset_code") != "nse_indices_daily"
        or _string(value, "benchmark_code") != "NIFTY 50"
        or _string(value, "research_profile_code") != "nse_current_research_v4"
        or _string(value, "backtest_policy_code") != "production_backtest_v1"
        or _string(value, "historical_universe_policy_code") != "production_historical_universe_v1"
        or _string(value, "multibagger_outcome_policy_code") != "production_multibagger_outcomes_v1"
        or _string(value, "multibagger_evaluation_policy_code")
        != "production_multibagger_evaluation_v1"
        or _string(value, "artifact_verification_semantics")
        != "original_gzip_sha256_and_canonical_semantic_rows_v1"
        or _string(value, "calendar_verification_algorithm")
        != "official_expected_sessions_exact_nifty_observed_set_v1"
        or _string(value, "current_status_filtering") != "forbidden"
        or _string(value, "historical_available_at_semantics")
        != "retrieval_time_never_source_trading_date"
        or _string(value, "raw_archival_requirement") != "archive_original_bytes_before_acceptance"
    ):
        raise ValueError("activation policy semantics are unsupported")
    maximum = value["maximum_mii_decompressed_bytes"]
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        raise ValueError("maximum_mii_decompressed_bytes must be a positive integer")
    return HistoricalEvaluationActivationPolicy(
        code=_string(value, "activation_policy_code"),
        version=_string(value, "policy_version"),
        checksum_sha256=canonical_json_sha256(value),
        source_provider_code=_string(value, "source_provider_code"),
        mii_dataset_code=_string(value, "mii_dataset_code"),
        mii_filename_pattern=_string(value, "mii_filename_pattern"),
        official_session_source_dataset_code=_string(value, "official_session_source_dataset_code"),
        benchmark_dataset_code=_string(value, "benchmark_dataset_code"),
        benchmark_code=_string(value, "benchmark_code"),
        calendar_verification_algorithm=_string(value, "calendar_verification_algorithm"),
        artifact_verification_semantics=_string(value, "artifact_verification_semantics"),
        maximum_mii_decompressed_bytes=maximum,
        inherited_checksums=exact,
    )


def load_multibagger_temporal_split_policy(path: Path) -> MultibaggerTemporalSplitPolicy:
    value = _object(path)
    expected = {
        "temporal_split_policy_code",
        "policy_version",
        "source_evaluation_policy_code",
        "source_evaluation_policy_checksum_sha256",
        "activation_policy_code",
        "activation_policy_checksum_sha256",
        "maturity_contract",
        "longest_horizon_calendar_years",
        "ordering",
        "development_percent",
        "validation_percent",
        "holdout_percent",
        "integer_partition_semantics",
        "minimum_validation_cutoffs",
        "minimum_holdout_cutoffs",
        "randomization",
        "label_classification_dependency",
        "algorithm_version",
    }
    _exact_fields(value, expected, "temporal split policy")
    percentages = tuple(
        _integer(value, key)
        for key in ("development_percent", "validation_percent", "holdout_percent")
    )
    if percentages != (60, 20, 20) or sum(percentages) != 100:
        raise ValueError("temporal split percentages are unsupported")
    if (
        _string(value, "temporal_split_policy_code") != "production_multibagger_temporal_split_v1"
        or _string(value, "policy_version") != "1"
        or _string(value, "source_evaluation_policy_code") != "production_multibagger_evaluation_v1"
        or _string(value, "source_evaluation_policy_checksum_sha256")
        != "121236d373fc7a6c7b7fe53881217ddce2cf607125f4ca20c3e71c3d128d3a11"
        or _string(value, "activation_policy_code")
        != "production_historical_evaluation_activation_v1"
        or _string(value, "activation_policy_checksum_sha256")
        != "3d67d3bbe9522bfc5a88748e5e3bce6dc7ca6478eb2661a896e701bc2be237b1"
        or _integer(value, "longest_horizon_calendar_years") != 5
        or _string(value, "maturity_contract") != "common_all_contracts_longest_horizon"
        or _string(value, "ordering") != "historical_cutoff_ascending"
        or _string(value, "integer_partition_semantics")
        != "floor_development_floor_validation_remainder_holdout_v1"
        or _integer(value, "minimum_validation_cutoffs") != 12
        or _integer(value, "minimum_holdout_cutoffs") != 12
        or _string(value, "randomization") != "forbidden"
        or _string(value, "label_classification_dependency") != "forbidden"
        or _string(value, "algorithm_version") != "chronological_common_maturity_60_20_20_v1"
    ):
        raise ValueError("temporal split policy semantics are unsupported")
    return MultibaggerTemporalSplitPolicy(
        code=_string(value, "temporal_split_policy_code"),
        version=_string(value, "policy_version"),
        checksum_sha256=canonical_json_sha256(value),
        source_evaluation_policy_checksum_sha256=_string(
            value, "source_evaluation_policy_checksum_sha256"
        ),
        activation_policy_code=_string(value, "activation_policy_code"),
        activation_policy_checksum_sha256=_string(value, "activation_policy_checksum_sha256"),
        longest_horizon_calendar_years=_integer(value, "longest_horizon_calendar_years"),
        development_percent=percentages[0],
        validation_percent=percentages[1],
        holdout_percent=percentages[2],
        minimum_validation_cutoffs=_integer(value, "minimum_validation_cutoffs"),
        minimum_holdout_cutoffs=_integer(value, "minimum_holdout_cutoffs"),
        algorithm_version=_string(value, "algorithm_version"),
    )


def load_multibagger_error_analysis_policy(path: Path) -> MultibaggerErrorAnalysisPolicy:
    value = _object(path)
    expected = {
        "error_analysis_policy_code",
        "policy_version",
        "source_evaluation_policy_code",
        "source_evaluation_policy_checksum_sha256",
        "allowed_split_codes",
        "confusion_classes",
        "dimensions",
        "score_bucket_source",
        "error_signatures",
        "component_diagnostics",
        "causal_claims",
        "threshold_optimization",
        "algorithm_version",
    }
    _exact_fields(value, expected, "error analysis policy")
    splits = _strings(value, "allowed_split_codes")
    classes = _strings(value, "confusion_classes")
    dimensions = _strings(value, "dimensions")
    signatures = _strings(value, "error_signatures")
    if (
        _string(value, "error_analysis_policy_code") != "production_multibagger_error_analysis_v1"
        or _string(value, "policy_version") != "1"
        or _string(value, "source_evaluation_policy_code") != "production_multibagger_evaluation_v1"
        or splits != ("development", "validation")
        or classes != ("fp", "fn")
        or dimensions
        != (
            "outcome_contract",
            "selection_contract",
            "evaluation_view",
            "temporal_split",
            "calendar_year",
            "score_bucket",
            "rankability",
            "snapshot_status",
            "component_availability_pattern",
            "unranked_reason",
            "confusion_class",
            "rank_distance_from_boundary",
            "score_gap_from_boundary",
            "threshold_time",
            "peak_multiple",
            "maximum_drawdown",
            "endpoint_return",
        )
        or signatures
        != (
            "UNRANKABLE_POSITIVE_FN",
            "RANKABLE_JUST_BELOW_BOUNDARY",
            "HIGH_SCORE_NEGATIVE",
            "LOW_SCORE_EVENTUAL_POSITIVE",
            "OTHER_FACTUAL_FP",
            "OTHER_FACTUAL_FN",
        )
        or _string(value, "score_bucket_source") != "production_backtest_v1"
        or _string(value, "component_diagnostics") != "frozen_v5_components_factual_association_v1"
        or _string(value, "causal_claims") != "forbidden"
        or _string(value, "threshold_optimization") != "forbidden"
        or _string(value, "algorithm_version") != "factual_fp_fn_error_analysis_v1"
        or _string(value, "source_evaluation_policy_checksum_sha256")
        != "121236d373fc7a6c7b7fe53881217ddce2cf607125f4ca20c3e71c3d128d3a11"
    ):
        raise ValueError("error analysis policy semantics are unsupported")
    return MultibaggerErrorAnalysisPolicy(
        code=_string(value, "error_analysis_policy_code"),
        version=_string(value, "policy_version"),
        checksum_sha256=canonical_json_sha256(value),
        source_evaluation_policy_checksum_sha256=_string(
            value, "source_evaluation_policy_checksum_sha256"
        ),
        allowed_split_codes=splits,
        confusion_classes=classes,
        dimensions=dimensions,
        error_signatures=signatures,
        algorithm_version=_string(value, "algorithm_version"),
    )


def _object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _exact_fields(value: dict[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(f"{label} fields are not exact")


def _string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _integer(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return item


def _strings(value: dict[str, object], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if (
        not isinstance(item, list)
        or not item
        or any(not isinstance(entry, str) or not entry for entry in item)
        or len(set(item)) != len(item)
    ):
        raise ValueError(f"{key} must be an ordered unique string list")
    return tuple(item)
