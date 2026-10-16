"""Strict, checksum-bound Production S2 challenger-development policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from inflector_data.historical_evaluation_policy import canonical_json_sha256

COMPONENT_CODES = (
    "balance_sheet",
    "business_catalyst",
    "business_quality",
    "cash_flow_quality",
    "financial_inflection",
    "low_market_attention",
    "market_structure",
    "valuation",
)
OUTCOME_CONTRACTS = ("MB_2X_2Y", "MB_3X_3Y", "MB_5X_5Y")
SELECTION_CONTRACTS = (
    "TOP_5",
    "TOP_10",
    "TOP_20",
    "TOP_1_PERCENT",
    "TOP_5_PERCENT",
    "TOP_10_PERCENT",
)
EVALUATION_VIEWS = ("end_to_end", "rankable_only")
INHERITED_CHECKSUMS = {
    "research_profile_checksum_sha256": (
        "f5d07ec154851f2a75b622ac2fa9742447e0e415572ff6c67986bc669e18d7b1"
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
    "champion_scoring_configuration_checksum_sha256": (
        "702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569"
    ),
}


@dataclass(frozen=True, slots=True)
class MultibaggerChallengerDevelopmentPolicy:
    code: str
    version: str
    checksum_sha256: str
    inherited_checksums: dict[str, str]
    challenger_algorithm_code: str
    component_codes: tuple[str, ...]
    primary_outcome_contract: str
    primary_selection_contract: str
    primary_evaluation_view: str
    primary_comparison_metric: str
    outcome_contracts: tuple[str, ...]
    selection_contracts: tuple[str, ...]
    evaluation_views: tuple[str, ...]
    allowed_splits: tuple[str, ...]
    maximum_candidates_per_experiment: int
    candidate_set_sealing_rule: str
    validation_opening_rule: str
    freeze_rule: str


def load_multibagger_challenger_development_policy(
    path: Path,
) -> MultibaggerChallengerDevelopmentPolicy:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("challenger development policy is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("challenger development policy must be an object")
    expected = {
        "challenger_development_policy_code",
        "policy_version",
        *INHERITED_CHECKSUMS,
        "challenger_algorithm_code",
        "component_codes",
        "primary_outcome_contract",
        "primary_selection_contract",
        "primary_evaluation_view",
        "primary_comparison_metric",
        "outcome_contracts",
        "selection_contracts",
        "evaluation_views",
        "allowed_splits",
        "holdout_access",
        "maximum_candidates_per_experiment",
        "candidate_set_sealing_rule",
        "validation_opening_rule",
        "freeze_rule",
        "production_promotion",
        "automatic_weight_search",
    }
    if set(value) != expected:
        raise ValueError("challenger development policy fields are not exact")
    for field, checksum in INHERITED_CHECKSUMS.items():
        if value[field] != checksum:
            raise ValueError(f"challenger development policy {field} binding mismatch")
    if (
        value["challenger_development_policy_code"]
        != "production_multibagger_challenger_development_v1"
        or value["policy_version"] != "1"
        or value["challenger_algorithm_code"]
        != "counterfactual_v5_top_level_weight_recomposition_v1"
        or tuple(value["component_codes"]) != COMPONENT_CODES
        or value["primary_outcome_contract"] != "MB_3X_3Y"
        or value["primary_selection_contract"] != "TOP_20"
        or value["primary_evaluation_view"] != "end_to_end"
        or value["primary_comparison_metric"] != "f1"
        or tuple(value["outcome_contracts"]) != OUTCOME_CONTRACTS
        or tuple(value["selection_contracts"]) != SELECTION_CONTRACTS
        or tuple(value["evaluation_views"]) != EVALUATION_VIEWS
        or tuple(value["allowed_splits"]) != ("development", "validation")
        or value["holdout_access"] != "forbidden"
        or value["maximum_candidates_per_experiment"] != 8
        or value["candidate_set_sealing_rule"]
        != "ordered_candidate_checksums_immutable_before_validation_v1"
        or value["validation_opening_rule"]
        != "all_validation_scores_persisted_and_flushed_before_outcomes_v1"
        or value["freeze_rule"]
        != "validation_conservative_gate_single_candidate_for_production_t_v1"
        or value["production_promotion"] != "forbidden"
        or value["automatic_weight_search"] != "forbidden"
    ):
        raise ValueError("challenger development policy semantics are unsupported")
    return MultibaggerChallengerDevelopmentPolicy(
        code=str(value["challenger_development_policy_code"]),
        version=str(value["policy_version"]),
        checksum_sha256=canonical_json_sha256(value),
        inherited_checksums=dict(INHERITED_CHECKSUMS),
        challenger_algorithm_code=str(value["challenger_algorithm_code"]),
        component_codes=COMPONENT_CODES,
        primary_outcome_contract=str(value["primary_outcome_contract"]),
        primary_selection_contract=str(value["primary_selection_contract"]),
        primary_evaluation_view=str(value["primary_evaluation_view"]),
        primary_comparison_metric=str(value["primary_comparison_metric"]),
        outcome_contracts=OUTCOME_CONTRACTS,
        selection_contracts=SELECTION_CONTRACTS,
        evaluation_views=EVALUATION_VIEWS,
        allowed_splits=("development", "validation"),
        maximum_candidates_per_experiment=8,
        candidate_set_sealing_rule=str(value["candidate_set_sealing_rule"]),
        validation_opening_rule=str(value["validation_opening_rule"]),
        freeze_rule=str(value["freeze_rule"]),
    )
