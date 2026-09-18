"""Phase 4A immutable model/configuration repository tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from inflector_core.scoring_policy import (
    InflectionScoringPolicy,
    scoring_policy_checksum,
    scoring_policy_from_mapping,
)
from inflector_database.models import ScoringConfiguration
from inflector_database.scoring_repository import (
    AmbiguousActiveConfigurationError,
    ScoringPolicyRepository,
)

POLICY_PATH = Path(__file__).parent / "fixtures" / "inflection_model_v1_development.json"


def _at(month: int, day: int = 1) -> datetime:
    return datetime(2027, month, day, 12, tzinfo=UTC)


def _policy(threshold: str = "0.1") -> InflectionScoringPolicy:
    mapping = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    mapping["financial_inflection"]["persistence_threshold"] = threshold
    return scoring_policy_from_mapping(mapping)


def test_repository_creates_model_and_enforces_family_semantic_identity(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="draft",
    )
    session.commit()

    assert repository.get_model_version(model.id) is not None
    with pytest.raises(IntegrityError), session.begin_nested():
        repository.create_model_version(
            model_family="inflection",
            semantic_version="1.0.0",
            git_sha="b" * 40,
            status="draft",
        )
    other = repository.create_model_version(
        model_family="another_family",
        semantic_version="1.0.0",
        git_sha="c" * 40,
        status="draft",
    )
    assert other.id != model.id


def test_repository_round_trips_typed_policy_and_preserves_old_versions(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="active",
    )
    original_policy = _policy("0.10")
    original = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="draft",
        policy=original_policy,
    )
    original_json = dict(original.record.configuration_json)
    original_checksum = original.record.checksum_sha256
    revised = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="2",
        status="draft",
        policy=_policy("0.2"),
    )
    session.expire_all()

    loaded = repository.get_scoring_configuration(original.record.id)

    assert loaded is not None and loaded.policy == original_policy
    assert loaded.record.configuration_json == original_json
    assert loaded.record.checksum_sha256 == original_checksum
    assert loaded.record.checksum_sha256 == scoring_policy_checksum(original_policy)
    assert revised.record.id != original.record.id
    assert revised.record.model_version.id == model.id


def test_repository_configuration_identity_constraint_and_status_validation(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="draft",
    )
    repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="draft",
        policy=_policy(),
    )
    session.commit()

    with pytest.raises(IntegrityError), session.begin_nested():
        repository.create_scoring_configuration(
            model_version_id=model.id,
            configuration_name="development",
            configuration_version="1",
            status="draft",
            policy=_policy("0.2"),
        )
    with pytest.raises(ValueError, match="draft, active, or retired"):
        repository.create_model_version(
            model_family="bad",
            semantic_version="1",
            git_sha="b" * 40,
            status="unknown",
        )
    with pytest.raises(ValueError, match="later"):
        repository.create_scoring_configuration(
            model_version_id=model.id,
            configuration_name="bad_interval",
            configuration_version="1",
            status="draft",
            policy=_policy(),
            effective_from=_at(2),
            effective_to=_at(1),
        )


def test_active_resolution_uses_half_open_effective_intervals(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="active",
    )
    bounded = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="active",
        policy=_policy(),
        effective_from=_at(1),
        effective_to=_at(2),
    )

    assert (
        repository.resolve_active_configuration(
            model_family="inflection", at=_at(1) - timedelta(microseconds=1)
        )
        is None
    )
    at_start = repository.resolve_active_configuration(model_family="inflection", at=_at(1))
    before_end = repository.resolve_active_configuration(
        model_family="inflection", at=_at(2) - timedelta(microseconds=1)
    )
    assert at_start is not None and at_start.record.id == bounded.record.id
    assert before_end is not None and before_end.record.id == bounded.record.id
    assert repository.resolve_active_configuration(model_family="inflection", at=_at(2)) is None

    open_ended = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="2",
        status="active",
        policy=_policy("0.2"),
        effective_from=_at(2),
    )
    resolved = repository.resolve_active_configuration(model_family="inflection", at=_at(12))
    assert resolved is not None and resolved.record.id == open_ended.record.id


def test_active_resolution_fails_closed_on_overlap(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="active",
    )
    for version in ("1", "2"):
        repository.create_scoring_configuration(
            model_version_id=model.id,
            configuration_name="overlap",
            configuration_version=version,
            status="active",
            policy=_policy(version),
            effective_from=_at(1),
        )

    with pytest.raises(AmbiguousActiveConfigurationError):
        repository.resolve_active_configuration(model_family="inflection", at=_at(2))


def test_active_resolution_ignores_draft_retired_and_inactive_models(session) -> None:
    repository = ScoringPolicyRepository(session)
    for index, (model_status, configuration_status) in enumerate(
        (("draft", "active"), ("retired", "active"), ("active", "draft"), ("active", "retired")),
        start=1,
    ):
        model = repository.create_model_version(
            model_family=f"family_{index}",
            semantic_version="1.0.0",
            git_sha=str(index) * 40,
            status=model_status,
        )
        repository.create_scoring_configuration(
            model_version_id=model.id,
            configuration_name="development",
            configuration_version="1",
            status=configuration_status,
            policy=_policy(),
        )
        assert (
            repository.resolve_active_configuration(
                model_family=f"family_{index}", at=_at(2)
            )
            is None
        )


def test_active_resolution_normalizes_aware_time_and_rejects_naive_time(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="active",
    )
    repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="active",
        policy=_policy(),
        effective_from=_at(1),
    )
    ist = timezone(timedelta(hours=5, minutes=30))

    assert (
        repository.resolve_active_configuration(
            model_family="inflection",
            at=datetime(2027, 1, 2, 17, 30, tzinfo=ist),
        )
        is not None
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.resolve_active_configuration(
            model_family="inflection", at=datetime(2027, 1, 2)
        )


def test_configuration_json_and_checksum_are_persisted_exactly(session) -> None:
    repository = ScoringPolicyRepository(session)
    model = repository.create_model_version(
        model_family="inflection",
        semantic_version="1.0.0",
        git_sha="a" * 40,
        status="draft",
    )
    created = repository.create_scoring_configuration(
        model_version_id=model.id,
        configuration_name="development",
        configuration_version="1",
        status="draft",
        policy=_policy(),
    )
    session.flush()
    raw = session.get(ScoringConfiguration, created.record.id)

    assert raw is not None and isinstance(raw.configuration_json, dict)
    assert raw.checksum_sha256 == scoring_policy_checksum(_policy())
