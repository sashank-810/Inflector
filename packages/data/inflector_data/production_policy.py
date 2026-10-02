"""Database-specific binding and immutable initialization of production scoring policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_data.gdelt_attention import GDELT_NEWS_METHOD_DEFINITION_SHA256
from inflector_data.research_profile import (
    DatasetBinding,
    ProductionResearchProfile,
    load_profile_financial_endpoint_policy,
    load_profile_financial_primitive_policy,
)
from inflector_database.models import (
    DataProvider,
    ModelVersion,
    ProviderDataset,
    ScoringConfiguration,
)
from inflector_database.scoring_repository import ScoringPolicyRepository


class ProductionPolicyBindingError(RuntimeError):
    """The explicit production provider or immutable model identity is unavailable."""


@dataclass(frozen=True, slots=True)
class BoundProductionPolicy:
    policy: InflectionScoringPolicy
    dataset_ids: dict[str, UUID | None]


@dataclass(frozen=True, slots=True)
class InitializedProductionModel:
    model_version: ModelVersion
    scoring_configuration: ScoringConfiguration
    policy_checksum_sha256: str
    dataset_ids: dict[str, UUID | None]
    created_model: bool
    created_configuration: bool


def resolve_profile_datasets(
    session: Session,
    profile: ProductionResearchProfile,
    *,
    require_optional: bool = False,
) -> dict[str, UUID | None]:
    resolved: dict[str, UUID | None] = {}
    for domain, binding in profile.provider_datasets.items():
        if binding is None:
            resolved[domain] = None
            continue
        dataset = _resolve_dataset(session, binding)
        if dataset is None:
            if domain in {"news_attention", "analyst_attention"} and not require_optional:
                resolved[domain] = None
                continue
            raise ProductionPolicyBindingError(
                f"required provider dataset is unavailable: "
                f"{binding.provider_code}/{binding.dataset_code}"
            )
        resolved[domain] = dataset.id
    return resolved


def bind_production_policy(
    session: Session,
    profile: ProductionResearchProfile,
    *,
    repository_root: Path,
) -> BoundProductionPolicy:
    try:
        load_profile_financial_primitive_policy(profile, repository_root)
        load_profile_financial_endpoint_policy(profile, repository_root)
    except ValueError as error:
        raise ProductionPolicyBindingError(str(error)) from error
    dataset_ids = resolve_profile_datasets(session, profile, require_optional=True)
    financial_id = dataset_ids["financial"]
    if financial_id is None:
        raise ProductionPolicyBindingError("financial provider binding is required")
    asset_path = repository_root / profile.scoring_policy_asset
    try:
        mapping = json.loads(asset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProductionPolicyBindingError("production scoring policy asset is invalid") from error
    if not isinstance(mapping, dict):
        raise ProductionPolicyBindingError("production scoring policy root must be an object")
    financial_context = mapping.get("financial_context")
    if not isinstance(financial_context, dict):
        raise ProductionPolicyBindingError("production policy lacks financial_context")
    financial_context["provider_dataset_priority"] = [str(financial_id)]
    financial_context["filing_scope_priority"] = list(profile.financial_scope_priority)

    attention = mapping.get("low_market_attention")
    if not isinstance(attention, dict):
        raise ProductionPolicyBindingError("production policy lacks Low Market Attention")
    news_series = attention.get("news_series")
    if not isinstance(news_series, dict):
        raise ProductionPolicyBindingError("production policy lacks news series")
    news_id = dataset_ids["news_attention"]
    if news_id is not None:
        news_series["provider_dataset_id"] = str(news_id)
    news_series["measurement_definition_sha256"] = GDELT_NEWS_METHOD_DEFINITION_SHA256
    return BoundProductionPolicy(
        policy=scoring_policy_from_mapping(cast(dict[str, Any], mapping)),
        dataset_ids=dataset_ids,
    )


def initialize_production_model(
    session: Session,
    *,
    profile: ProductionResearchProfile,
    repository_root: Path,
    model_family: str,
    model_semantic_version: str,
    git_sha: str,
    effective_from: datetime,
) -> InitializedProductionModel:
    family = _trimmed(model_family, "model_family")
    semantic_version = _trimmed(model_semantic_version, "model_semantic_version")
    commit = _trimmed(git_sha, "git_sha")
    starts_at = _aware_utc(effective_from, "effective_from")
    bound = bind_production_policy(session, profile, repository_root=repository_root)
    checksum = scoring_policy_checksum(bound.policy)
    repository = ScoringPolicyRepository(session)

    matching_models = list(
        session.scalars(
            select(ModelVersion).where(
                ModelVersion.model_family == family,
                ModelVersion.semantic_version == semantic_version,
            )
        )
    )
    if len(matching_models) > 1:
        raise ProductionPolicyBindingError("model version identity is ambiguous")
    created_model = False
    if matching_models:
        model = matching_models[0]
        if model.git_sha != commit or model.status != "active":
            raise ProductionPolicyBindingError(
                "existing model version conflicts with requested immutable identity"
            )
    else:
        other_active = session.scalar(
            select(ModelVersion.id).where(
                ModelVersion.model_family == family,
                ModelVersion.status == "active",
            )
        )
        if other_active is not None:
            raise ProductionPolicyBindingError(
                "another active model version already exists for this family"
            )
        model = repository.create_model_version(
            model_family=family,
            semantic_version=semantic_version,
            git_sha=commit,
            status="active",
            activated_at=starts_at,
        )
        created_model = True

    configurations = list(
        session.scalars(
            select(ScoringConfiguration).where(
                ScoringConfiguration.model_version_id == model.id,
                ScoringConfiguration.configuration_name == profile.research_profile_code,
                ScoringConfiguration.configuration_version == profile.profile_version,
            )
        )
    )
    if len(configurations) > 1:
        raise ProductionPolicyBindingError("scoring configuration identity is ambiguous")
    created_configuration = False
    if configurations:
        configuration = configurations[0]
        if (
            configuration.checksum_sha256 != checksum
            or configuration.status != "active"
            or _optional_utc(configuration.effective_from) != starts_at
        ):
            raise ProductionPolicyBindingError(
                "existing scoring configuration conflicts with requested immutable content"
            )
        repository.get_scoring_configuration(configuration.id)
    else:
        other_active_configuration = session.scalar(
            select(ScoringConfiguration.id)
            .join(ModelVersion)
            .where(
                ModelVersion.model_family == family,
                ScoringConfiguration.status == "active",
            )
        )
        if other_active_configuration is not None:
            raise ProductionPolicyBindingError(
                "another active scoring configuration exists for this model family"
            )
        persisted = repository.create_scoring_configuration(
            model_version_id=model.id,
            configuration_name=profile.research_profile_code,
            configuration_version=profile.profile_version,
            status="active",
            policy=bound.policy,
            effective_from=starts_at,
        )
        configuration = persisted.record
        created_configuration = True
    session.commit()
    return InitializedProductionModel(
        model_version=model,
        scoring_configuration=configuration,
        policy_checksum_sha256=checksum,
        dataset_ids=bound.dataset_ids,
        created_model=created_model,
        created_configuration=created_configuration,
    )


def _resolve_dataset(session: Session, binding: DatasetBinding) -> ProviderDataset | None:
    values = list(
        session.scalars(
            select(ProviderDataset)
            .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
            .where(
                DataProvider.code == binding.provider_code,
                ProviderDataset.code == binding.dataset_code,
            )
        )
    )
    if len(values) > 1:
        raise ProductionPolicyBindingError(
            f"provider dataset binding is ambiguous: {binding.provider_code}/{binding.dataset_code}"
        )
    return values[0] if values else None


def _trimmed(value: str, field: str) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and trimmed")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _aware_utc(value, "persisted datetime")
