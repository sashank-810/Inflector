"""Strict checksum-bound Production T temporal-holdout policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.multibagger_challenger_policy import (
    EVALUATION_VIEWS,
    OUTCOME_CONTRACTS,
    SELECTION_CONTRACTS,
)

INHERITED_CHECKSUMS = {
    "research_profile_checksum_sha256": (
        "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
    ),
    "champion_scoring_configuration_checksum_sha256": (
        "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
    ),
    "backtest_policy_checksum_sha256": (
        "e1f1f886f686045f7f2774be355674a726b155db1a32c9080999c32becd35984"
    ),
    "historical_universe_policy_checksum_sha256": (
        "ac0b519b8a0e82961d70ff697aa459566e02df2927a1613057a0287287137982"
    ),
    "multibagger_outcome_policy_checksum_sha256": (
        "3c901049e37dfa1764d70feb7e8fdfa8762b3c4733ea283f8b6d99c73475332f"
    ),
    "multibagger_evaluation_policy_checksum_sha256": (
        "121236d373fc7a6c7b7fe53881217ddce2cf607125f4ca20c3e71c3d128d3a11"
    ),
    "historical_evaluation_activation_policy_checksum_sha256": (
        "3d67d3bbe9522bfc5a88748e5e3bce6dc7ca6478eb2661a896e701bc2be237b1"
    ),
    "temporal_split_policy_checksum_sha256": (
        "140b1c05116587a83e99715c962b117e414e53ab3c2e3c9930f60b34ec1f274f"
    ),
    "error_analysis_policy_checksum_sha256": (
        "5d7373331e4fdb7b53d2161e65d46198f9e6ab5d19ae1fb65dca628850f29e0d"
    ),
    "challenger_development_policy_checksum_sha256": (
        "17642f0e05640464b0166bc82740882e6fda5c99e62ca555c19d7be30ebe85e0"
    ),
}


@dataclass(frozen=True, slots=True)
class MultibaggerTemporalHoldoutPolicy:
    code: str
    version: str
    checksum_sha256: str
    inherited_checksums: dict[str, str]
    primary_outcome_contract: str
    primary_selection_contract: str
    primary_evaluation_view: str
    primary_comparison_metric: str
    outcome_contracts: tuple[str, ...]
    selection_contracts: tuple[str, ...]
    evaluation_views: tuple[str, ...]
    allowed_split: str
    score_freeze_rule: str
    decision_rule: str


def load_multibagger_temporal_holdout_policy(
    path: Path,
) -> MultibaggerTemporalHoldoutPolicy:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("temporal holdout policy is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("temporal holdout policy must be an object")
    expected = {
        "temporal_holdout_policy_code",
        "policy_version",
        *INHERITED_CHECKSUMS,
        "primary_outcome_contract",
        "primary_selection_contract",
        "primary_evaluation_view",
        "primary_comparison_metric",
        "outcome_contracts",
        "selection_contracts",
        "evaluation_views",
        "allowed_split",
        "score_freeze_rule",
        "decision_rule",
        "tuning",
        "candidate_creation",
        "automatic_search",
        "model_training",
        "production_promotion",
        "post_holdout_model_modification",
    }
    if set(value) != expected:
        raise ValueError("temporal holdout policy fields are not exact")
    for field, checksum in INHERITED_CHECKSUMS.items():
        if value[field] != checksum:
            raise ValueError(f"temporal holdout policy {field} binding mismatch")
    forbidden = (
        "tuning",
        "candidate_creation",
        "automatic_search",
        "model_training",
        "production_promotion",
        "post_holdout_model_modification",
    )
    if (
        value["temporal_holdout_policy_code"] != "production_multibagger_temporal_holdout_v1"
        or value["policy_version"] != "1"
        or value["primary_outcome_contract"] != "MB_3X_3Y"
        or value["primary_selection_contract"] != "TOP_20"
        or value["primary_evaluation_view"] != "end_to_end"
        or value["primary_comparison_metric"] != "f1"
        or tuple(value["outcome_contracts"]) != OUTCOME_CONTRACTS
        or tuple(value["selection_contracts"]) != SELECTION_CONTRACTS
        or tuple(value["evaluation_views"]) != EVALUATION_VIEWS
        or value["allowed_split"] != "holdout"
        or value["score_freeze_rule"]
        != "all_subject_scores_persisted_and_flushed_before_holdout_outcomes_v1"
        or value["decision_rule"] != "conservative_holdout_guardrails_v1"
        or any(value[field] != "forbidden" for field in forbidden)
    ):
        raise ValueError("temporal holdout policy semantics are unsupported")
    return MultibaggerTemporalHoldoutPolicy(
        code=str(value["temporal_holdout_policy_code"]),
        version=str(value["policy_version"]),
        checksum_sha256=canonical_json_sha256(value),
        inherited_checksums=dict(INHERITED_CHECKSUMS),
        primary_outcome_contract="MB_3X_3Y",
        primary_selection_contract="TOP_20",
        primary_evaluation_view="end_to_end",
        primary_comparison_metric="f1",
        outcome_contracts=OUTCOME_CONTRACTS,
        selection_contracts=SELECTION_CONTRACTS,
        evaluation_views=EVALUATION_VIEWS,
        allowed_split="holdout",
        score_freeze_rule=str(value["score_freeze_rule"]),
        decision_rule=str(value["decision_rule"]),
    )
