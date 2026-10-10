"""Versioned Production Q historical-universe and outcome-label policies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

from inflector_data.backtest_policy import load_backtest_policy


def canonical_json_sha256(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoricalUniversePolicy:
    code: str
    version: str
    exchange: str
    source_provider_code: str
    source_dataset_code: str
    eligible_series: tuple[str, ...]
    security_types: tuple[str, ...]
    cutoff_cadence: str
    identity_resolution_semantics: str
    survivorship_handling: str
    ambiguity_handling: str
    unresolved_handling: str
    current_status_filtering: str
    source_semantics: str
    source_filename_pattern: str
    source_snapshot_semantics: str
    complete_member_set_semantics: str
    semantic_row_verification_version: str
    cohort_source_date_semantics: str
    shard_ordering: tuple[str, ...]
    maximum_symbols_per_backtest_shard: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class MultibaggerContract:
    code: str
    threshold_multiple: Decimal
    horizon_calendar_years: int


@dataclass(frozen=True, slots=True)
class MultibaggerOutcomePolicy:
    code: str
    version: str
    source_backtest_policy_code: str
    source_backtest_policy_checksum_sha256: str
    entry_observation_rule: str
    return_price_basis: str
    corporate_action_treatment: str
    cash_dividend_treatment: str
    positive_maturation_semantics: str
    negative_maturation_semantics: str
    outcome_window_completeness_algorithm: str
    unmatured_semantics: str
    unavailable_semantics: str
    maximum_drawdown_algorithm: str
    label_fingerprint_version: str
    contracts: tuple[MultibaggerContract, ...]
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class EvaluationSelectionContract:
    """A predeclared diagnostic budget, never an outcome-tuned threshold."""

    code: str
    kind: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class MultibaggerEvaluationPolicy:
    """Frozen Production R prediction-evaluation contract."""

    code: str
    version: str
    source_backtest_policy_code: str
    source_backtest_policy_checksum_sha256: str
    historical_universe_policy_code: str
    historical_universe_policy_checksum_sha256: str
    multibagger_outcome_policy_code: str
    multibagger_outcome_policy_checksum_sha256: str
    research_profile_code: str
    research_profile_checksum_sha256: str
    ranking_source: str
    ranking_direction: str
    tie_order: tuple[str, ...]
    selection_contracts: tuple[EvaluationSelectionContract, ...]
    evaluation_views: tuple[str, ...]
    prediction_integrity_version: str
    calendar_integrity_requirement: str
    score_bucket_source: str
    algorithm_version: str
    checksum_sha256: str


def load_historical_universe_policy(path: Path) -> HistoricalUniversePolicy:
    value = _object(path, "historical universe policy")
    expected = {
        "historical_universe_policy_code",
        "policy_version",
        "exchange",
        "source_provider_code",
        "source_dataset_code",
        "eligible_series",
        "security_types",
        "cutoff_cadence",
        "identity_resolution_semantics",
        "survivorship_handling",
        "ambiguity_handling",
        "unresolved_handling",
        "current_status_filtering",
        "source_semantics",
        "source_filename_pattern",
        "source_snapshot_semantics",
        "complete_member_set_semantics",
        "semantic_row_verification_version",
        "cohort_source_date_semantics",
        "shard_ordering",
        "maximum_symbols_per_backtest_shard",
    }
    if set(value) != expected:
        raise ValueError("historical universe policy fields are incomplete or unsupported")
    exact = {
        "historical_universe_policy_code": "production_historical_universe_v1",
        "policy_version": "1",
        "exchange": "NSE",
        "source_provider_code": "nse_official",
        "source_dataset_code": "nse_cm_mii_security_daily",
        "cutoff_cadence": "calendar_month_end_utc",
        "identity_resolution_semantics": ("exact_isin_then_exact_historical_symbol_interval_v1"),
        "survivorship_handling": ("include_historical_members_regardless_of_current_status"),
        "ambiguity_handling": "explicit_ambiguous_identity_fail_closed",
        "unresolved_handling": "persist_explicit_unresolved_identity",
        "current_status_filtering": "forbidden",
        "source_semantics": ("official_nse_cm_mii_daily_security_master_historical_snapshot_v1"),
        "source_filename_pattern": "NSE_CM_security_ddmmyyyy.csv.gz",
        "source_snapshot_semantics": ("single_completed_ingestion_single_archived_mii_artifact_v1"),
        "complete_member_set_semantics": ("exact_accepted_eligible_source_record_set_equality_v1"),
        "semantic_row_verification_version": ("nse_cm_mii_security_semantic_row_sha256_v1"),
        "cohort_source_date_semantics": (
            "latest_complete_mii_security_snapshot_in_cutoff_month_v1"
        ),
    }
    for field, expected_value in exact.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"historical universe {field} is unsupported")
    series = _strings(value, "eligible_series")
    security_types = _strings(value, "security_types")
    shard_ordering = _strings(value, "shard_ordering")
    if series != ("EQ",) or security_types != ("equity",):
        raise ValueError("historical universe V1 supports NSE EQ ordinary equity only")
    if shard_ordering != (
        "security_id_asc",
        "historical_isin_asc",
        "historical_symbol_asc",
        "member_fingerprint_asc",
    ):
        raise ValueError("historical universe shard ordering is unsupported")
    maximum = _integer(value, "maximum_symbols_per_backtest_shard")
    if maximum != 25:
        raise ValueError("historical universe V1 must retain J's 25-symbol shard bound")
    return HistoricalUniversePolicy(
        code=exact["historical_universe_policy_code"],
        version=exact["policy_version"],
        exchange=exact["exchange"],
        source_provider_code=exact["source_provider_code"],
        source_dataset_code=exact["source_dataset_code"],
        eligible_series=series,
        security_types=security_types,
        cutoff_cadence=exact["cutoff_cadence"],
        identity_resolution_semantics=exact["identity_resolution_semantics"],
        survivorship_handling=exact["survivorship_handling"],
        ambiguity_handling=exact["ambiguity_handling"],
        unresolved_handling=exact["unresolved_handling"],
        current_status_filtering=exact["current_status_filtering"],
        source_semantics=exact["source_semantics"],
        source_filename_pattern=exact["source_filename_pattern"],
        source_snapshot_semantics=exact["source_snapshot_semantics"],
        complete_member_set_semantics=exact["complete_member_set_semantics"],
        semantic_row_verification_version=exact["semantic_row_verification_version"],
        cohort_source_date_semantics=exact["cohort_source_date_semantics"],
        shard_ordering=shard_ordering,
        maximum_symbols_per_backtest_shard=maximum,
        checksum_sha256=canonical_json_sha256(value),
    )


def load_multibagger_outcome_policy(
    path: Path, *, repository_root: Path
) -> MultibaggerOutcomePolicy:
    value = _object(path, "multibagger outcome policy")
    expected = {
        "multibagger_outcome_policy_code",
        "policy_version",
        "source_backtest_policy_code",
        "source_backtest_policy_checksum_sha256",
        "entry_observation_rule",
        "return_price_basis",
        "corporate_action_treatment",
        "cash_dividend_treatment",
        "positive_maturation_semantics",
        "negative_maturation_semantics",
        "outcome_window_completeness_algorithm",
        "unmatured_semantics",
        "unavailable_semantics",
        "maximum_drawdown_algorithm",
        "label_fingerprint_version",
        "contracts",
    }
    if set(value) != expected:
        raise ValueError("multibagger outcome policy fields are incomplete or unsupported")
    exact = {
        "multibagger_outcome_policy_code": "production_multibagger_outcomes_v1",
        "policy_version": "1",
        "source_backtest_policy_code": "production_backtest_v1",
        "entry_observation_rule": (
            "exact_pit_visible_raw_entry_bar_rebased_on_outcome_adjusted_series_v1"
        ),
        "return_price_basis": "corporate_action_adjusted_close_price_multiple_v1",
        "corporate_action_treatment": "existing_split_bonus_adjusted_market_price_v1",
        "cash_dividend_treatment": "excluded_price_return_only",
        "positive_maturation_semantics": ("raw_entry_hit_and_intervening_adjustment_evidence_v1"),
        "negative_maturation_semantics": (
            "full_calendar_horizon_with_complete_nse_session_coverage_v1"
        ),
        "outcome_window_completeness_algorithm": (
            "nifty50_session_dates_full_security_bar_coverage_v1"
        ),
        "unmatured_semantics": "horizon_not_elapsed_without_threshold_hit_v1",
        "unavailable_semantics": "explicit_reason_without_negative_imputation_v1",
        "maximum_drawdown_algorithm": ("running_peak_adjusted_close_within_contract_window_v1"),
        "label_fingerprint_version": "multibagger_outcome_label_fingerprint_v1",
    }
    for field, expected_value in exact.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"multibagger outcome {field} is unsupported")
    source_checksum = _sha(value, "source_backtest_policy_checksum_sha256")
    source_policy = load_backtest_policy(
        repository_root / "config/backtest/production_backtest_v1.json",
        repository_root=repository_root,
    )
    if source_policy.code != exact["source_backtest_policy_code"]:
        raise ValueError("multibagger source backtest policy code mismatch")
    if source_policy.checksum_sha256 != source_checksum:
        raise ValueError("multibagger source backtest policy checksum mismatch")
    raw_contracts = value.get("contracts")
    if not isinstance(raw_contracts, list):
        raise ValueError("multibagger contracts must be an array")
    contracts: list[MultibaggerContract] = []
    for raw in raw_contracts:
        if not isinstance(raw, dict) or set(raw) != {
            "code",
            "threshold_multiple",
            "horizon_calendar_years",
        }:
            raise ValueError("multibagger contract is malformed")
        try:
            threshold = Decimal(_text(raw, "threshold_multiple"))
        except InvalidOperation as error:
            raise ValueError("multibagger threshold must be an exact Decimal string") from error
        contracts.append(
            MultibaggerContract(
                code=_text(raw, "code"),
                threshold_multiple=threshold,
                horizon_calendar_years=_integer(raw, "horizon_calendar_years"),
            )
        )
    expected_contracts = (
        ("MB_2X_2Y", Decimal("2"), 2),
        ("MB_3X_3Y", Decimal("3"), 3),
        ("MB_5X_5Y", Decimal("5"), 5),
    )
    if (
        tuple(
            (item.code, item.threshold_multiple, item.horizon_calendar_years) for item in contracts
        )
        != expected_contracts
    ):
        raise ValueError("multibagger V1 contract set or order is unsupported")
    return MultibaggerOutcomePolicy(
        code=exact["multibagger_outcome_policy_code"],
        version=exact["policy_version"],
        source_backtest_policy_code=exact["source_backtest_policy_code"],
        source_backtest_policy_checksum_sha256=source_checksum,
        entry_observation_rule=exact["entry_observation_rule"],
        return_price_basis=exact["return_price_basis"],
        corporate_action_treatment=exact["corporate_action_treatment"],
        cash_dividend_treatment=exact["cash_dividend_treatment"],
        positive_maturation_semantics=exact["positive_maturation_semantics"],
        negative_maturation_semantics=exact["negative_maturation_semantics"],
        outcome_window_completeness_algorithm=exact["outcome_window_completeness_algorithm"],
        unmatured_semantics=exact["unmatured_semantics"],
        unavailable_semantics=exact["unavailable_semantics"],
        maximum_drawdown_algorithm=exact["maximum_drawdown_algorithm"],
        label_fingerprint_version=exact["label_fingerprint_version"],
        contracts=tuple(contracts),
        checksum_sha256=canonical_json_sha256(value),
    )


def load_multibagger_evaluation_policy(
    path: Path, *, repository_root: Path
) -> MultibaggerEvaluationPolicy:
    """Load the deliberately small, pre-outcome Production R contract."""

    value = _object(path, "multibagger evaluation policy")
    expected = {
        "multibagger_evaluation_policy_code",
        "policy_version",
        "source_backtest_policy_code",
        "source_backtest_policy_checksum_sha256",
        "historical_universe_policy_code",
        "historical_universe_policy_checksum_sha256",
        "multibagger_outcome_policy_code",
        "multibagger_outcome_policy_checksum_sha256",
        "research_profile_code",
        "research_profile_checksum_sha256",
        "ranking_source",
        "ranking_direction",
        "tie_order",
        "selection_contracts",
        "evaluation_views",
        "prediction_integrity_version",
        "calendar_integrity_requirement",
        "score_bucket_source",
        "algorithm_version",
    }
    if set(value) != expected:
        raise ValueError("multibagger evaluation policy fields are incomplete or unsupported")
    exact = {
        "multibagger_evaluation_policy_code": "production_multibagger_evaluation_v1",
        "policy_version": "1",
        "source_backtest_policy_code": "production_backtest_v1",
        "source_backtest_policy_checksum_sha256": (
            "e1f1f886f686045f7f2774be355674a726b155db1a32c9080999c32becd35984"
        ),
        "historical_universe_policy_code": "production_historical_universe_v1",
        "historical_universe_policy_checksum_sha256": (
            "ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982"
        ),
        "multibagger_outcome_policy_code": "production_multibagger_outcomes_v1",
        "multibagger_outcome_policy_checksum_sha256": (
            "3c901049e37dfa1764d70feb7e8fdfa8762b3c4733ea283f8b6d99c73475332f"
        ),
        "research_profile_code": "nse_current_research_v4",
        "research_profile_checksum_sha256": (
            "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
        ),
        "ranking_source": "persisted_v5_final_score_only",
        "ranking_direction": "descending",
        "prediction_integrity_version": "frozen_backtest_observation_v5_binding_v1",
        "calendar_integrity_requirement": "verified_benchmark_session_archive_v1",
        "score_bucket_source": "production_backtest_v1",
        "algorithm_version": "multibagger_prediction_evaluation_v1",
    }
    for field, expected_value in exact.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"multibagger evaluation policy {field} is unsupported")
    tie_order = _strings(value, "tie_order")
    if tie_order != ("symbol_asc", "security_id_asc"):
        raise ValueError("multibagger evaluation tie order is unsupported")
    views = _strings(value, "evaluation_views")
    if views != ("end_to_end", "rankable_only"):
        raise ValueError("multibagger evaluation views are unsupported")
    raw_contracts = value.get("selection_contracts")
    if not isinstance(raw_contracts, list):
        raise ValueError("selection_contracts must be a list")
    contracts: list[EvaluationSelectionContract] = []
    for raw in raw_contracts:
        if not isinstance(raw, dict) or set(raw) != {"code", "kind", "value"}:
            raise ValueError("selection contract is malformed")
        kind = _text(raw, "kind")
        if kind not in {"count", "percent"}:
            raise ValueError("selection contract kind is unsupported")
        try:
            amount = Decimal(_text(raw, "value"))
        except InvalidOperation as error:
            raise ValueError("selection contract value must be an exact Decimal") from error
        if amount <= 0:
            raise ValueError("selection contract value must be positive")
        contracts.append(EvaluationSelectionContract(_text(raw, "code"), kind, amount))
    expected_contracts = (
        ("TOP_5", "count", Decimal("5")),
        ("TOP_10", "count", Decimal("10")),
        ("TOP_20", "count", Decimal("20")),
        ("TOP_1_PERCENT", "percent", Decimal("1")),
        ("TOP_5_PERCENT", "percent", Decimal("5")),
        ("TOP_10_PERCENT", "percent", Decimal("10")),
    )
    if tuple((item.code, item.kind, item.value) for item in contracts) != expected_contracts:
        raise ValueError("selection contracts must be the reviewed Production R V1 set")
    # Verify the immutable assets rather than trusting copied checksum strings.
    root = repository_root.resolve()
    backtest = load_backtest_policy(
        root / "config/backtest/production_backtest_v1.json", repository_root=root
    )
    universe = load_historical_universe_policy(
        root / "config/backtest/production_historical_universe_v1.json"
    )
    outcomes = load_multibagger_outcome_policy(
        root / "config/backtest/production_multibagger_outcomes_v1.json", repository_root=root
    )
    if (
        backtest.code != exact["source_backtest_policy_code"]
        or backtest.checksum_sha256 != exact["source_backtest_policy_checksum_sha256"]
        or universe.code != exact["historical_universe_policy_code"]
        or universe.checksum_sha256 != exact["historical_universe_policy_checksum_sha256"]
        or outcomes.code != exact["multibagger_outcome_policy_code"]
        or outcomes.checksum_sha256 != exact["multibagger_outcome_policy_checksum_sha256"]
    ):
        raise ValueError("multibagger evaluation policy has an invalid bound policy asset")
    return MultibaggerEvaluationPolicy(
        code=exact["multibagger_evaluation_policy_code"],
        version=exact["policy_version"],
        source_backtest_policy_code=exact["source_backtest_policy_code"],
        source_backtest_policy_checksum_sha256=exact["source_backtest_policy_checksum_sha256"],
        historical_universe_policy_code=exact["historical_universe_policy_code"],
        historical_universe_policy_checksum_sha256=exact[
            "historical_universe_policy_checksum_sha256"
        ],
        multibagger_outcome_policy_code=exact["multibagger_outcome_policy_code"],
        multibagger_outcome_policy_checksum_sha256=exact[
            "multibagger_outcome_policy_checksum_sha256"
        ],
        research_profile_code=exact["research_profile_code"],
        research_profile_checksum_sha256=exact["research_profile_checksum_sha256"],
        ranking_source=exact["ranking_source"],
        ranking_direction=exact["ranking_direction"],
        tie_order=tie_order,
        selection_contracts=tuple(contracts),
        evaluation_views=views,
        prediction_integrity_version=exact["prediction_integrity_version"],
        calendar_integrity_requirement=exact["calendar_integrity_requirement"],
        score_bucket_source=exact["score_bucket_source"],
        algorithm_version=exact["algorithm_version"],
        checksum_sha256=canonical_json_sha256(value),
    )


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _text(value: dict[str, Any], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{field} must be non-empty text")
    return result


def _strings(value: dict[str, Any], field: str) -> tuple[str, ...]:
    raw = value.get(field)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} must be a non-empty array")
    result = tuple(item for item in raw if isinstance(item, str) and item.strip())
    if len(result) != len(raw) or len(set(result)) != len(result):
        raise ValueError(f"{field} must contain unique non-empty strings")
    return result


def _integer(value: dict[str, Any], field: str) -> int:
    result = value.get(field)
    if not isinstance(result, int) or isinstance(result, bool) or result <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return result


def _sha(value: dict[str, Any], field: str) -> str:
    result = _text(value, field)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return result
