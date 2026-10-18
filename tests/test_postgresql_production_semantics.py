"""PostgreSQL-only checks for production locking and schema semantics."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


def _postgresql_engine() -> Engine:
    database_url = os.getenv("INFLECTOR_TEST_POSTGRES_URL", "").strip()
    if not database_url:
        pytest.skip("INFLECTOR_TEST_POSTGRES_URL is not configured")
    engine = create_engine(database_url)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.fail("INFLECTOR_TEST_POSTGRES_URL must select PostgreSQL")
    return engine


def test_postgresql_head_json_and_release_pointer_constraints() -> None:
    engine = _postgresql_engine()
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20261009_0028"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'model_release_candidates' "
                        "AND column_name = 'scoring_definition_json'"
                    )
                ).scalar_one()
                == "json"
            )
            constraints = set(
                connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'active_model_release'::regclass"
                    )
                ).scalars()
            )
            assert {
                "ck_active_model_release_singleton",
                "ck_active_model_release_subject",
                "ck_active_model_release_version",
            } <= constraints
    finally:
        engine.dispose()


def test_postgresql_for_update_nowait_rejects_a_competing_lock() -> None:
    engine = _postgresql_engine()
    try:
        with engine.connect() as owner, engine.connect() as contender:
            owner_transaction = owner.begin()
            contender_transaction = contender.begin()
            try:
                owner.execute(text("SELECT version_num FROM alembic_version FOR UPDATE")).all()
                with pytest.raises(DBAPIError) as captured:
                    contender.execute(
                        text("SELECT version_num FROM alembic_version FOR UPDATE NOWAIT")
                    ).all()
                assert getattr(captured.value.orig, "sqlstate", None) == "55P03"
            finally:
                contender_transaction.rollback()
                owner_transaction.rollback()
    finally:
        engine.dispose()
