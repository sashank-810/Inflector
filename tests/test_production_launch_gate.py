"""Final launch-gate CI, environment, and documentation regressions."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from scripts.audit_postgresql_identifiers import audit
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inflector_data.model_release_cli as release_cli

ROOT = Path(__file__).resolve().parents[1]


def test_ci_is_read_only_and_exercises_postgresql_migrations() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert "image: postgres:16-alpine" in workflow
    assert "DATABASE_URL: postgresql+psycopg://inflector_ci:" in workflow
    assert 'python-version: "3.12"' in workflow
    assert 'python -m pip install -e ".[dev]"' in workflow
    assert "ruff check ." in workflow
    assert "pyright" in workflow
    assert "pytest" in workflow
    assert "python -m alembic heads" in workflow
    assert '20261009_0028' in workflow
    assert "python -m alembic upgrade head" in workflow
    assert "python -m alembic downgrade 20261008_0027" in workflow
    assert "pytest tests/test_postgresql_production_semantics.py" in workflow
    assert "npm ci" in workflow
    assert "npm audit --omit=dev --audit-level=high" in workflow
    assert "npm run lint" in workflow
    assert "npm run typecheck" in workflow
    assert "npm run build" in workflow
    assert "deploy" not in workflow.lower()


def test_production_environment_template_contains_names_without_secrets() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    for name in (
        "INFLECTOR_PRODUCTION_DATABASE_URL",
        "INFLECTOR_PRODUCTION_RAW_ROOT",
        "INFLECTOR_NSE_LICENSE_CLASS",
        "INFLECTOR_GDELT_RAW_ROOT",
        "INFLECTOR_GDELT_LICENSE_CLASS",
        "INFLECTOR_MODEL_FAMILY",
        "INFLECTOR_RESEARCH_PROFILE",
        "INFLECTOR_OPERATIONS_PROFILE",
        "INFLECTOR_SYMBOLS_FILE",
        "INFLECTOR_TELEGRAM_BOT_TOKEN",
        "INFLECTOR_TELEGRAM_CHAT_ID",
        "INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE",
    ):
        assert f"{name}=" in template
    assert "INFLECTOR_PRODUCTION_DATABASE_URL=postgres" not in template
    assert "INFLECTOR_TELEGRAM_BOT_TOKEN=\n" in template
    assert "INFLECTOR_TELEGRAM_CHAT_ID=\n" in template


def test_release_readiness_uses_inflector_telegram_presence_names(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_readiness(session: Session, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"status": "environment_blocked"}

    monkeypatch.setattr(release_cli, "production_release_readiness", fake_readiness)
    monkeypatch.setattr(
        release_cli,
        "_upstream",
        lambda args: (object(), object(), object(), object()),
    )
    monkeypatch.setenv("INFLECTOR_TELEGRAM_BOT_TOKEN", "present")
    monkeypatch.setenv("INFLECTOR_TELEGRAM_CHAT_ID", "present")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    args = argparse.Namespace(
        command="model-release-readiness",
        release_policy=ROOT / "config/operations/production_model_release_v1.json",
        repository_root=ROOT,
        observed_at=datetime(2026, 10, 18, tzinfo=UTC),
        raw_root=ROOT,
        gdelt_raw_root=ROOT,
        model_family="inflector_v1",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with Session(engine) as session:
            release_cli._execute(args, session)  # noqa: SLF001
    finally:
        engine.dispose()

    assert observed["telegram_bot_token_present"] is True
    assert observed["telegram_chat_id_present"] is True


def test_launch_runbook_keeps_real_side_effects_operator_controlled() -> None:
    runbook = (ROOT / "docs/production-launch-gate.md").read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())

    assert "Codex does not perform this send" in normalized
    assert "Do not test destructive downgrade on production" in normalized
    assert "historical_model_validation = data_blocked" in runbook
    assert "active_subject = champion" in runbook
    assert "NO_GO_ENVIRONMENT" in runbook
    assert "GO_FOR_CONTROLLED_PRODUCTION_LAUNCH" in runbook


def test_postgresql_index_names_fit_the_identifier_limit() -> None:
    migration_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "migrations/versions").glob("*.py")
    )

    for index_name in (
        "ix_be_quant_deriv_fingerprint_sha256",
        "ix_hist_session_artifact_acq_key",
        "ix_hist_session_artifact_provenance",
        "ix_hist_session_artifact_provider_dataset",
        "ix_hist_session_artifact_ingestion",
        "ix_hist_session_calendar_source_artifact",
        "ix_hist_session_dates_calendar_run_id",
        "ix_mb_split_cutoff_split_run",
        "ix_mb_split_cutoff_eval_cohort",
        "ix_mb_error_row_analysis_run",
        "ix_mb_challenger_experiment_source_eval_run",
        "ix_mb_challenger_experiment_champion_config",
        "ix_mb_challenger_eval_row_fingerprint",
        "ix_mb_holdout_run_champion_config",
        "ix_model_release_prod_score_fingerprint",
        "ix_opp_discovery_item_release_score_id",
    ):
        assert len(index_name) <= 63
        assert index_name in migration_sources
    assert (
        "ix_business_event_quantitative_derivations_derivation_fingerprint_sha256"
        not in migration_sources
    )
    assert (
        "ix_historical_session_calendar_dates_historical_session_calendar_run_id"
        not in migration_sources
    )
    assert (
        "ix_opportunity_discovery_items_model_release_production_score_id"
        not in migration_sources
    )


def test_all_explicit_postgresql_identifiers_fit_the_identifier_limit() -> None:
    result = audit(ROOT)

    assert result["status"] == "pass"
    assert cast(int, result["identifier_count"]) > 0
    assert cast(int, result["maximum_identifier_bytes"]) <= 63
    assert result["violations"] == ()
