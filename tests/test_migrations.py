from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_creates_identity_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-test.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        table_names = inspect(engine).get_table_names()
        assert {
            "companies",
            "securities",
            "exchange_listings",
            "data_providers",
            "provider_datasets",
            "ingestion_runs",
            "source_records",
            "data_quality_issues",
            "price_bars",
            "fiscal_periods",
            "financial_filings",
            "financial_metric_definitions",
            "financial_facts",
            "corporate_actions",
            "security_relationships",
            "model_versions",
            "scoring_configurations",
        }.issubset(table_names)
        source_columns = {
            column["name"] for column in inspect(engine).get_columns("source_records")
        }
        assert "raw_payload_reference" in source_columns
    finally:
        engine.dispose()


def test_phase_4a_migration_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-4a-migration-test.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"model_versions", "scoring_configurations"}.issubset(tables)
        assert not {
            "feature_snapshots",
            "feature_values",
            "score_snapshots",
            "score_components",
            "score_explanations",
        }.intersection(tables)
        foreign_keys = inspector.get_foreign_keys("scoring_configurations")
        assert any(key["referred_table"] == "model_versions" for key in foreign_keys)
    finally:
        engine.dispose()

    command.downgrade(config, "20260909_0006")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert "corporate_actions" in tables
        assert "model_versions" not in tables
        assert "scoring_configurations" not in tables
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"model_versions", "scoring_configurations"}.issubset(tables)
    finally:
        engine.dispose()

    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        table_names = inspect(engine).get_table_names()
        assert not {"companies", "securities", "exchange_listings"}.intersection(table_names)
    finally:
        engine.dispose()
