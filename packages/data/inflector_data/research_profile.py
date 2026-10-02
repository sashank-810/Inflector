"""Versioned operational profile for current production research assembly."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from inflector_data.financial_endpoint import (
    FinancialEndpointPolicy,
    load_financial_endpoint_policy,
)
from inflector_data.financial_primitive_policy import (
    FinancialPrimitivePolicy,
    load_financial_primitive_policy,
)


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
    delivery_observation_window: int | None
    critical_data_quality_rule_codes: tuple[str, ...]
    analyst_coverage_source: DatasetBinding | None
    scoring_policy_asset: str
    financial_primitive_policy_asset: str | None
    financial_primitive_policy_checksum_sha256: str | None
    financial_endpoint_policy_code: str | None
    financial_endpoint_policy_asset: str | None
    financial_endpoint_policy_checksum_sha256: str | None
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
    if frozenset(datasets) not in {
        frozenset(required_domains),
        frozenset({*required_domains, "delivery"}),
    }:
        raise ValueError("research profile dataset domains are incomplete")
    delivery_window_raw = value.get("delivery_observation_window")
    delivery_window = (
        None if delivery_window_raw is None else _positive_int(value, "delivery_observation_window")
    )
    if "delivery" in datasets and delivery_window != 20:
        raise ValueError("delivery-enabled research profile must use the accepted 20-bar window")
    if "delivery" not in datasets and delivery_window is not None:
        raise ValueError("delivery window requires an explicit delivery dataset")
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
    primitive_asset = _optional_text(value, "financial_primitive_policy_asset")
    primitive_checksum = _optional_text(value, "financial_primitive_policy_checksum_sha256")
    if (primitive_asset is None) != (primitive_checksum is None):
        raise ValueError("financial primitive policy asset and checksum must be supplied together")
    if primitive_checksum is not None and (
        len(primitive_checksum) != 64
        or any(character not in "0123456789abcdef" for character in primitive_checksum)
    ):
        raise ValueError("financial primitive policy checksum must be lowercase SHA-256")
    endpoint_code = _optional_text(value, "financial_endpoint_policy_code")
    endpoint_asset = _optional_text(value, "financial_endpoint_policy_asset")
    endpoint_checksum = _optional_text(value, "financial_endpoint_policy_checksum_sha256")
    if len({item is None for item in (endpoint_code, endpoint_asset, endpoint_checksum)}) != 1:
        raise ValueError(
            "financial endpoint policy code, asset, and checksum must be supplied together"
        )
    if endpoint_checksum is not None and (
        len(endpoint_checksum) != 64
        or any(character not in "0123456789abcdef" for character in endpoint_checksum)
    ):
        raise ValueError("financial endpoint policy checksum must be lowercase SHA-256")
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
        delivery_observation_window=delivery_window,
        critical_data_quality_rule_codes=critical,
        analyst_coverage_source=_dataset_binding(
            value.get("analyst_coverage_source"), "analyst_coverage_source"
        ),
        scoring_policy_asset=_text(value, "scoring_policy_asset"),
        financial_primitive_policy_asset=primitive_asset,
        financial_primitive_policy_checksum_sha256=primitive_checksum,
        financial_endpoint_policy_code=endpoint_code,
        financial_endpoint_policy_asset=endpoint_asset,
        financial_endpoint_policy_checksum_sha256=endpoint_checksum,
        checksum_sha256=sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


def load_profile_financial_primitive_policy(
    profile: ProductionResearchProfile, repository_root: Path
) -> FinancialPrimitivePolicy | None:
    """Resolve an explicitly bound policy inside the repository and verify its checksum."""

    if profile.financial_primitive_policy_asset is None:
        return None
    root = repository_root.resolve()
    policy_path = (root / profile.financial_primitive_policy_asset).resolve()
    try:
        policy_path.relative_to(root)
    except ValueError as error:
        raise ValueError("financial primitive policy must remain inside the repository") from error
    policy = load_financial_primitive_policy(policy_path)
    if policy.checksum_sha256 != profile.financial_primitive_policy_checksum_sha256:
        raise ValueError("financial primitive policy checksum does not match research profile")
    return policy


def load_profile_financial_endpoint_policy(
    profile: ProductionResearchProfile, repository_root: Path
) -> FinancialEndpointPolicy | None:
    """Resolve and verify the explicitly bound cutoff-aware endpoint policy."""

    if profile.financial_endpoint_policy_asset is None:
        return None
    root = repository_root.resolve()
    policy_path = (root / profile.financial_endpoint_policy_asset).resolve()
    try:
        policy_path.relative_to(root)
    except ValueError as error:
        raise ValueError("financial endpoint policy must remain inside the repository") from error
    policy = load_financial_endpoint_policy(policy_path)
    if (
        policy.financial_endpoint_policy_code != profile.financial_endpoint_policy_code
        or policy.checksum_sha256 != profile.financial_endpoint_policy_checksum_sha256
    ):
        raise ValueError("financial endpoint policy identity does not match research profile")
    return policy


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


def _optional_text(value: dict[str, Any], field: str) -> str | None:
    return None if value.get(field) is None else _text(value, field)


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
