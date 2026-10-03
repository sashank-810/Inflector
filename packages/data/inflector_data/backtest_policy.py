"""Versioned strict-knowledge-time backtest and source-availability policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class HistoricalAvailabilityRule:
    dataset_code: str | None
    classification: str
    treatment: str
    rationale: str


@dataclass(frozen=True, slots=True)
class HistoricalAvailabilityManifest:
    code: str
    version: str
    classifications: dict[str, HistoricalAvailabilityRule]
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class BacktestPolicy:
    code: str
    version: str
    mode: str
    scoring_policy_asset: str
    scoring_policy_asset_checksum_sha256: str
    research_profile_asset: str
    research_profile_checksum_sha256: str
    historical_availability_manifest_asset: str
    historical_availability_manifest_checksum_sha256: str
    financial_primitive_policy_checksum_sha256: str
    financial_endpoint_policy_checksum_sha256: str
    cutoff_cadence: str
    eligible_universe_policy: str
    maximum_symbols: int
    forward_return_horizons: tuple[int, ...]
    entry_observation_rule: str
    forward_observation_rule: str
    benchmark_observation_rule: str
    return_price_basis: str
    cash_dividend_treatment: str
    corporate_action_treatment: str
    benchmark_code: str
    missing_outcome_handling: str
    delisting_handling: str
    minimum_forward_observations: str
    duplicate_snapshot_handling: str
    research_state_projection_version: str
    evaluation_grouping_rules: tuple[str, ...]
    score_buckets: tuple[dict[str, str], ...]
    minimum_bucket_sample_size: int
    multibagger_horizon_observations: int
    multibagger_return_threshold: Decimal
    checksum_sha256: str


def canonical_json_sha256(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_historical_availability_manifest(path: Path) -> HistoricalAvailabilityManifest:
    value = _object(path, "historical availability manifest")
    if set(value) != {
        "historical_availability_manifest_code",
        "manifest_version",
        "classifications",
    }:
        raise ValueError("historical availability manifest fields are unsupported")
    raw_rules = value.get("classifications")
    if not isinstance(raw_rules, dict) or not raw_rules:
        raise ValueError("historical availability classifications must be a non-empty object")
    rules: dict[str, HistoricalAvailabilityRule] = {}
    for domain, raw in raw_rules.items():
        if not isinstance(domain, str) or not domain.strip() or not isinstance(raw, dict):
            raise ValueError("historical availability classification is invalid")
        if set(raw) != {"dataset_code", "classification", "treatment", "rationale"}:
            raise ValueError(f"historical availability rule {domain!r} is invalid")
        classification = _text(raw, "classification")
        if classification not in {"A", "B", "C", "D"}:
            raise ValueError("historical availability classification must be A, B, C, or D")
        dataset = raw.get("dataset_code")
        if dataset is not None and (not isinstance(dataset, str) or not dataset.strip()):
            raise ValueError("historical dataset code must be null or non-empty")
        rules[domain] = HistoricalAvailabilityRule(
            dataset_code=dataset,
            classification=classification,
            treatment=_text(raw, "treatment"),
            rationale=_text(raw, "rationale"),
        )
    required = {
        "canonical_nse_universe",
        "financials",
        "prices",
        "benchmark",
        "delivery",
        "corporate_actions",
        "business_events",
        "news_attention",
        "analyst_attention",
        "market_cap",
    }
    if set(rules) != required:
        raise ValueError("historical availability manifest domains are incomplete")
    return HistoricalAvailabilityManifest(
        code=_text(value, "historical_availability_manifest_code"),
        version=_text(value, "manifest_version"),
        classifications=rules,
        checksum_sha256=canonical_json_sha256(value),
    )


def load_backtest_policy(path: Path, *, repository_root: Path) -> BacktestPolicy:
    value = _object(path, "backtest policy")
    expected = {
        "backtest_policy_code",
        "policy_version",
        "mode",
        "scoring_policy_asset",
        "scoring_policy_asset_checksum_sha256",
        "research_profile_asset",
        "research_profile_checksum_sha256",
        "historical_availability_manifest_asset",
        "historical_availability_manifest_checksum_sha256",
        "financial_primitive_policy_checksum_sha256",
        "financial_endpoint_policy_checksum_sha256",
        "cutoff_cadence",
        "eligible_universe_policy",
        "maximum_symbols",
        "forward_return_horizons",
        "entry_observation_rule",
        "forward_observation_rule",
        "benchmark_observation_rule",
        "return_price_basis",
        "cash_dividend_treatment",
        "corporate_action_treatment",
        "benchmark_code",
        "missing_outcome_handling",
        "delisting_handling",
        "minimum_forward_observations",
        "duplicate_snapshot_handling",
        "research_state_projection_version",
        "evaluation_grouping_rules",
        "score_buckets",
        "minimum_bucket_sample_size",
        "multibagger_horizon_observations",
        "multibagger_return_threshold",
    }
    if set(value) != expected:
        raise ValueError("backtest policy fields are incomplete or unsupported")
    exact = {
        "mode": "strict_knowledge_time",
        "cutoff_cadence": "calendar_month_end_utc",
        "eligible_universe_policy": "observed_pit_universe",
        "entry_observation_rule": "latest_pit_visible_market_bar_on_or_before_cutoff",
        "forward_observation_rule": (
            "nth_subsequent_security_trading_observation_strictly_after_cutoff_date"
        ),
        "benchmark_observation_rule": (
            "same_entry_and_exit_trading_dates_as_security_from_outcome_cutoff"
        ),
        "return_price_basis": "corporate_action_adjusted_close_price_return_v1",
        "cash_dividend_treatment": "excluded_price_return_only",
        "corporate_action_treatment": "existing_split_bonus_adjusted_market_price_v1",
        "benchmark_code": "NIFTY 50",
        "missing_outcome_handling": "explicit_unavailable_no_imputation",
        "delisting_handling": "outcome_unavailable_due_to_delisting_without_terminal_value",
        "minimum_forward_observations": "exact_horizon",
        "duplicate_snapshot_handling": "audit_all_headline_state_changes_only",
        "research_state_projection_version": "backtest_research_state_projection_v1",
    }
    for field, accepted in exact.items():
        if _text(value, field) != accepted:
            raise ValueError(f"backtest policy {field} is unsupported")
    maximum_symbols = _positive_int(value, "maximum_symbols")
    if maximum_symbols > 25:
        raise ValueError("backtest maximum_symbols must not exceed 25")
    horizons = _positive_int_tuple(value, "forward_return_horizons")
    if horizons != (21, 63, 126, 252):
        raise ValueError("backtest forward horizons must preserve the reviewed diagnostics")
    root = repository_root.resolve()
    _verify_asset(
        root,
        _text(value, "scoring_policy_asset"),
        _sha(value, "scoring_policy_asset_checksum_sha256"),
    )
    research_path = _verify_asset(
        root,
        _text(value, "research_profile_asset"),
        _sha(value, "research_profile_checksum_sha256"),
    )
    # Research profiles use the same canonical JSON checksum definition.
    if canonical_json_sha256(_object(research_path, "research profile")) != _sha(
        value, "research_profile_checksum_sha256"
    ):
        raise ValueError("research profile checksum does not match backtest policy")
    manifest_path = _verify_asset(
        root,
        _text(value, "historical_availability_manifest_asset"),
        _sha(value, "historical_availability_manifest_checksum_sha256"),
    )
    manifest = load_historical_availability_manifest(manifest_path)
    if manifest.checksum_sha256 != _sha(value, "historical_availability_manifest_checksum_sha256"):
        raise ValueError("historical availability checksum does not match backtest policy")
    grouping = _strings(value, "evaluation_grouping_rules")
    raw_buckets = value.get("score_buckets")
    if not isinstance(raw_buckets, list) or not raw_buckets:
        raise ValueError("score_buckets must be a non-empty list")
    buckets: list[dict[str, str]] = []
    for raw in raw_buckets:
        if not isinstance(raw, dict) or not all(isinstance(item, str) for item in raw.values()):
            raise ValueError("score bucket boundaries must be exact decimal strings")
        try:
            for item in raw.values():
                Decimal(item)
        except InvalidOperation as error:
            raise ValueError("score bucket boundary is not an exact Decimal") from error
        buckets.append(dict(raw))
    threshold = _decimal(value, "multibagger_return_threshold")
    if threshold != Decimal("1"):
        raise ValueError("multibagger threshold must remain the predefined 100% return")
    return BacktestPolicy(
        code=_text(value, "backtest_policy_code"),
        version=_text(value, "policy_version"),
        mode=exact["mode"],
        scoring_policy_asset=_text(value, "scoring_policy_asset"),
        scoring_policy_asset_checksum_sha256=_sha(value, "scoring_policy_asset_checksum_sha256"),
        research_profile_asset=_text(value, "research_profile_asset"),
        research_profile_checksum_sha256=_sha(value, "research_profile_checksum_sha256"),
        historical_availability_manifest_asset=_text(
            value, "historical_availability_manifest_asset"
        ),
        historical_availability_manifest_checksum_sha256=manifest.checksum_sha256,
        financial_primitive_policy_checksum_sha256=_sha(
            value, "financial_primitive_policy_checksum_sha256"
        ),
        financial_endpoint_policy_checksum_sha256=_sha(
            value, "financial_endpoint_policy_checksum_sha256"
        ),
        cutoff_cadence=exact["cutoff_cadence"],
        eligible_universe_policy=exact["eligible_universe_policy"],
        maximum_symbols=maximum_symbols,
        forward_return_horizons=horizons,
        entry_observation_rule=exact["entry_observation_rule"],
        forward_observation_rule=exact["forward_observation_rule"],
        benchmark_observation_rule=exact["benchmark_observation_rule"],
        return_price_basis=exact["return_price_basis"],
        cash_dividend_treatment=exact["cash_dividend_treatment"],
        corporate_action_treatment=exact["corporate_action_treatment"],
        benchmark_code=exact["benchmark_code"],
        missing_outcome_handling=exact["missing_outcome_handling"],
        delisting_handling=exact["delisting_handling"],
        minimum_forward_observations=exact["minimum_forward_observations"],
        duplicate_snapshot_handling=exact["duplicate_snapshot_handling"],
        research_state_projection_version=exact["research_state_projection_version"],
        evaluation_grouping_rules=grouping,
        score_buckets=tuple(buckets),
        minimum_bucket_sample_size=_positive_int(value, "minimum_bucket_sample_size"),
        multibagger_horizon_observations=_positive_int(value, "multibagger_horizon_observations"),
        multibagger_return_threshold=threshold,
        checksum_sha256=canonical_json_sha256(value),
    )


def _verify_asset(root: Path, relative: str, expected_checksum: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("backtest policy asset must remain inside the repository") from error
    actual = canonical_json_sha256(_object(path, "bound policy asset"))
    if actual != expected_checksum:
        raise ValueError(f"bound policy asset checksum mismatch: {relative}")
    return path


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _text(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or item != item.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return item


def _sha(value: dict[str, Any], field: str) -> str:
    item = _text(value, field)
    if len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
        raise ValueError(f"{field} must be lowercase SHA-256")
    return item


def _positive_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return item


def _positive_int_tuple(value: dict[str, Any], field: str) -> tuple[int, ...]:
    item = value.get(field)
    if not isinstance(item, list) or not item:
        raise ValueError(f"{field} must be a non-empty list")
    result = tuple(
        entry
        for entry in item
        if isinstance(entry, int) and not isinstance(entry, bool) and entry > 0
    )
    if len(result) != len(item) or tuple(sorted(set(result))) != result:
        raise ValueError(f"{field} must contain unique ascending positive integers")
    return result


def _strings(value: dict[str, Any], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or not item:
        raise ValueError(f"{field} must be a non-empty list")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result


def _decimal(value: dict[str, Any], field: str) -> Decimal:
    item = value.get(field)
    if not isinstance(item, str):
        raise ValueError(f"{field} must be an exact decimal string")
    try:
        return Decimal(item)
    except InvalidOperation as error:
        raise ValueError(f"{field} is not an exact Decimal") from error
