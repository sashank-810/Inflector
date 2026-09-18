"""Persistence boundary for immutable Phase 4A model and policy versions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    policy_to_canonical_mapping,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_database.models import ModelVersion, ScoringConfiguration

VALID_POLICY_STATUSES = frozenset({"draft", "active", "retired"})


class AmbiguousActiveConfigurationError(RuntimeError):
    """Raised when overlapping active policy rows make resolution unsafe."""


class ScoringPolicyIntegrityError(RuntimeError):
    """Raised when persisted policy content does not match its checksum."""


@dataclass(frozen=True, slots=True)
class PersistedScoringConfiguration:
    record: ScoringConfiguration
    policy: InflectionScoringPolicy


class ScoringPolicyRepository:
    """Create and read immutable policy versions without generic update methods."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_model_version(
        self,
        *,
        model_family: str,
        semantic_version: str,
        git_sha: str,
        status: str,
        activated_at: datetime | None = None,
        retired_at: datetime | None = None,
    ) -> ModelVersion:
        self._validate_status(status)
        model = ModelVersion(
            model_family=model_family,
            semantic_version=semantic_version,
            git_sha=git_sha,
            status=status,
            activated_at=self._optional_utc(activated_at, "activated_at"),
            retired_at=self._optional_utc(retired_at, "retired_at"),
        )
        self._session.add(model)
        self._session.flush()
        return model

    def get_model_version(self, model_version_id: UUID) -> ModelVersion | None:
        return self._session.get(ModelVersion, model_version_id)

    def create_scoring_configuration(
        self,
        *,
        model_version_id: UUID,
        configuration_name: str,
        configuration_version: str,
        status: str,
        policy: InflectionScoringPolicy,
        effective_from: datetime | None = None,
        effective_to: datetime | None = None,
    ) -> PersistedScoringConfiguration:
        self._validate_status(status)
        starts_at = self._optional_utc(effective_from, "effective_from")
        ends_at = self._optional_utc(effective_to, "effective_to")
        if starts_at is not None and ends_at is not None and ends_at <= starts_at:
            raise ValueError("effective_to must be later than effective_from")
        record = ScoringConfiguration(
            model_version_id=model_version_id,
            configuration_name=configuration_name,
            configuration_version=configuration_version,
            status=status,
            effective_from=starts_at,
            effective_to=ends_at,
            configuration_json=policy_to_canonical_mapping(policy),
            checksum_sha256=scoring_policy_checksum(policy),
        )
        self._session.add(record)
        self._session.flush()
        return PersistedScoringConfiguration(record=record, policy=policy)

    def get_scoring_configuration(
        self,
        configuration_id: UUID,
    ) -> PersistedScoringConfiguration | None:
        record = self._session.get(ScoringConfiguration, configuration_id)
        return None if record is None else self._validated_configuration(record)

    def resolve_active_configuration(
        self,
        *,
        model_family: str,
        at: datetime,
    ) -> PersistedScoringConfiguration | None:
        cutoff = self._aware_utc(at, "at")
        statement = (
            select(ScoringConfiguration)
            .join(ModelVersion)
            .where(
                ModelVersion.model_family == model_family,
                ModelVersion.status == "active",
                ScoringConfiguration.status == "active",
                or_(
                    ScoringConfiguration.effective_from.is_(None),
                    ScoringConfiguration.effective_from <= cutoff,
                ),
                or_(
                    ScoringConfiguration.effective_to.is_(None),
                    ScoringConfiguration.effective_to > cutoff,
                ),
            )
        )
        matches = list(self._session.scalars(statement))
        if len(matches) > 1:
            raise AmbiguousActiveConfigurationError(
                f"multiple active configurations for {model_family!r} at {cutoff.isoformat()}"
            )
        return self._validated_configuration(matches[0]) if matches else None

    @staticmethod
    def _validated_configuration(
        record: ScoringConfiguration,
    ) -> PersistedScoringConfiguration:
        policy = scoring_policy_from_mapping(record.configuration_json)
        if scoring_policy_checksum(policy) != record.checksum_sha256:
            raise ScoringPolicyIntegrityError("persisted policy checksum mismatch")
        return PersistedScoringConfiguration(record=record, policy=policy)

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_POLICY_STATUSES:
            raise ValueError("status must be draft, active, or retired")

    @classmethod
    def _optional_utc(cls, value: datetime | None, field_name: str) -> datetime | None:
        return None if value is None else cls._aware_utc(value, field_name)

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)
