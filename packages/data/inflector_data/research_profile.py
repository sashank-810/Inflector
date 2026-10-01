"""Versioned operational profile for current production research assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DatasetBinding:
    provider_code: str
    dataset_code: str


@dataclass(frozen=True, slots=True)
class ProductionResearchProfile:
    research_profile_code: str
    profile_version: str
    provider_datasets: dict[str, DatasetBinding | None]
    benchmark_code: str
    financial_scope_priority: tuple[str, ...]
    financial_core_metrics: tuple[str, ...]
    financial_core_required: int
    comparable_history_metric: str
    confidence_required_feature_slots: tuple[str, ...]
    source_reliability: Decimal
    new_listing_horizon_days: int
    liquidity_window_observations: int
    liquidity_minimum_observations: int
    catalyst_lookback_days: int
    market_interval: str
    market_observation_days: int
    attention_news_window_days: int
    critical_data_quality_rule_codes: tuple[str, ...]
    analyst_coverage_source: DatasetBinding | None
    scoring_policy_asset: str
    checksum_sha256: str


def load_research_profile(path: Path) -> ProductionResearchProfile:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("research profile is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("research profile root must be an object")
    datasets_value = value.get("provider_datasets")
    if not isinstance(datasets_value, dict):
        raise ValueError("provider_datasets must be an object")
    datasets = {
        str(code): _dataset_binding(binding, str(code)) for code, binding in datasets_value.items()
    }
    required_domains = {
        "financial",
        "market",
        "benchmark",
        "corporate_actions",
        "business_events",
        "news_attention",
        "analyst_attention",
    }
    if set(datasets) != required_domains:
        raise ValueError("research profile dataset domains are incomplete")
    financial_core = _strings(value, "financial_core_metrics")
    confidence_slots = _strings(value, "confidence_required_feature_slots")
    scope_priority = _strings(value, "financial_scope_priority")
    if scope_priority != ("consolidated", "standalone"):
        raise ValueError("production financial scope priority must be explicit")
    source_reliability_raw = value.get("source_reliability")
    if not isinstance(source_reliability_raw, str):
        raise ValueError("source_reliability must be an exact decimal string")
    source_reliability = Decimal(source_reliability_raw)
    if not Decimal("0") <= source_reliability <= Decimal("1"):
        raise ValueError("source_reliability must be between zero and one")
    critical = _strings(value, "critical_data_quality_rule_codes", allow_empty=True)
    return ProductionResearchProfile(
        research_profile_code=_text(value, "research_profile_code"),
        profile_version=_text(value, "profile_version"),
        provider_datasets=datasets,
        benchmark_code=_text(value, "benchmark_code"),
        financial_scope_priority=scope_priority,
        financial_core_metrics=financial_core,
        financial_core_required=_positive_int(value, "financial_core_required"),
        comparable_history_metric=_text(value, "comparable_history_metric"),
        confidence_required_feature_slots=confidence_slots,
        source_reliability=source_reliability,
        new_listing_horizon_days=_positive_int(value, "new_listing_horizon_days"),
        liquidity_window_observations=_positive_int(value, "liquidity_window_observations"),
        liquidity_minimum_observations=_positive_int(value, "liquidity_minimum_observations"),
        catalyst_lookback_days=_positive_int(value, "catalyst_lookback_days"),
        market_interval=_text(value, "market_interval"),
        market_observation_days=_positive_int(value, "market_observation_days"),
        attention_news_window_days=_positive_int(value, "attention_news_window_days"),
        critical_data_quality_rule_codes=critical,
        analyst_coverage_source=_dataset_binding(
            value.get("analyst_coverage_source"), "analyst_coverage_source"
        ),
        scoring_policy_asset=_text(value, "scoring_policy_asset"),
        checksum_sha256=sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


def _dataset_binding(value: Any, field: str) -> DatasetBinding | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"provider_code", "dataset_code"}:
        raise ValueError(f"{field} must be a provider/dataset binding or null")
    provider_code = value.get("provider_code")
    dataset_code = value.get("dataset_code")
    if not isinstance(provider_code, str) or not provider_code.strip():
        raise ValueError(f"{field}.provider_code must be non-empty")
    if not isinstance(dataset_code, str) or not dataset_code.strip():
        raise ValueError(f"{field}.dataset_code must be non-empty")
    return DatasetBinding(provider_code.strip(), dataset_code.strip())


def _text(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or item != item.strip():
        raise ValueError(f"{field} must be non-empty and trimmed")
    return item


def _strings(value: dict[str, Any], field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or (not item and not allow_empty):
        raise ValueError(f"{field} must be a list of strings")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result


def _positive_int(value: dict[str, Any], field: str) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or item < 1:
        raise ValueError(f"{field} must be a positive integer")
    return item
