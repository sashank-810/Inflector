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
            "benchmark_series",
            "benchmark_bars",
            "fiscal_periods",
            "financial_filings",
            "financial_metric_definitions",
            "financial_facts",
            "corporate_actions",
            "security_relationships",
            "model_versions",
            "scoring_configurations",
            "score_snapshots",
            "score_components",
            "score_explanations",
        }.issubset(table_names)
        source_columns = {
            column["name"] for column in inspect(engine).get_columns("source_records")
        }
        assert "raw_payload_reference" in source_columns
    finally:
        engine.dispose()


def test_phase_2da_market_enrichment_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-2da-market-enrichment.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    enrichment_columns = {"market_cap", "delivery_quantity", "delivery_percentage"}
    benchmark_tables = {"benchmark_series", "benchmark_bars"}

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert benchmark_tables.issubset(set(inspector.get_table_names()))
        price_columns = {column["name"]: column for column in inspector.get_columns("price_bars")}
        assert enrichment_columns.issubset(price_columns)
        assert all(price_columns[name]["nullable"] for name in enrichment_columns)
        assert any(
            key["referred_table"] == "provider_datasets"
            for key in inspector.get_foreign_keys("benchmark_series")
        )
        assert {key["referred_table"] for key in inspector.get_foreign_keys("benchmark_bars")} == {
            "benchmark_series",
            "source_records",
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260919_0008")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert not benchmark_tables.intersection(tables)
        assert not enrichment_columns.intersection(
            {column["name"] for column in inspector.get_columns("price_bars")}
        )
        assert {
            "companies",
            "price_bars",
            "financial_facts",
            "model_versions",
            "score_snapshots",
        }.issubset(tables)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert benchmark_tables.issubset(set(inspector.get_table_names()))
        assert enrichment_columns.issubset(
            {column["name"] for column in inspector.get_columns("price_bars")}
        )
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
        assert not {"feature_snapshots", "feature_values"}.intersection(tables)
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


def test_phase_4c_migration_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-4c-migration-test.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    phase4c_tables = {"score_snapshots", "score_components", "score_explanations"}

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert phase4c_tables.issubset(tables)
        assert not {"feature_snapshots", "feature_values"}.intersection(tables)
        assert any(
            key["referred_table"] == "scoring_configurations"
            for key in inspect(engine).get_foreign_keys("score_snapshots")
        )
    finally:
        engine.dispose()

    command.downgrade(config, "20260918_0007")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not phase4c_tables.intersection(tables)
        assert {"model_versions", "scoring_configurations"}.issubset(tables)
        assert "financial_facts" in tables
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert phase4c_tables.issubset(set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()


def test_phase_4dg_security_identity_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-4dg-security-identity.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "20260919_0009")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("score_snapshots")}
        assert "selected_security_id" not in columns
    finally:
        engine.dispose()

    command.upgrade(config, "20260928_0010")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("score_snapshots")}
        assert columns["selected_security_id"]["nullable"]
        security_fks = [
            key
            for key in inspector.get_foreign_keys("score_snapshots")
            if key["constrained_columns"] == ["selected_security_id"]
        ]
        assert len(security_fks) == 1
        assert security_fks[0]["referred_table"] == "securities"
        assert security_fks[0].get("options", {}).get("ondelete") == "RESTRICT"
    finally:
        engine.dispose()

    command.downgrade(config, "20260919_0009")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        columns = {column["name"] for column in inspect(engine).get_columns("score_snapshots")}
        assert "selected_security_id" not in columns
    finally:
        engine.dispose()

    command.upgrade(config, "20260928_0010")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "selected_security_id" in {
            column["name"] for column in inspect(engine).get_columns("score_snapshots")
        }
    finally:
        engine.dispose()


def test_phase_6a_announcement_evidence_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-6a-announcement-evidence.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    phase6a_tables = {"announcements", "documents", "announcement_documents"}

    command.upgrade(config, "20260928_0010")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert not phase6a_tables.intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()


    command.upgrade(config, "20260928_0011")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert phase6a_tables.issubset(inspector.get_table_names())
        announcement_columns = {
            column["name"]: column for column in inspector.get_columns("announcements")
        }
        document_columns = {column["name"]: column for column in inspector.get_columns("documents")}
        assert announcement_columns["security_id"]["nullable"]
        assert not announcement_columns["source_record_id"]["nullable"]
        assert document_columns["document_content_sha256"]["nullable"]
        assert {key["referred_table"] for key in inspector.get_foreign_keys("announcements")} == {
            "companies",
            "securities",
            "provider_datasets",
            "source_records",
        }
        assert {key["referred_table"] for key in inspector.get_foreign_keys("documents")} == {
            "companies",
            "securities",
            "provider_datasets",
            "source_records",
        }
        assert {
            key["referred_table"] for key in inspector.get_foreign_keys("announcement_documents")
        } == {"announcements", "documents"}
        relation_pk = inspector.get_pk_constraint("announcement_documents")
        assert set(relation_pk["constrained_columns"]) == {
            "announcement_id",
            "document_id",
        }
    finally:
        engine.dispose()


    command.downgrade(config, "20260928_0010")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not phase6a_tables.intersection(tables)
        assert "selected_security_id" in {
            column["name"] for column in inspect(engine).get_columns("score_snapshots")
        }
    finally:
        engine.dispose()

    command.upgrade(config, "20260928_0011")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert phase6a_tables.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_phase_6b_document_text_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-6b-document-text.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    phase6b_tables = {"document_assets", "document_text_extractions"}

    command.upgrade(config, "20260928_0011")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert not phase6b_tables.intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()


    command.upgrade(config, "20260928_0012")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert phase6b_tables.issubset(inspector.get_table_names())
        asset_columns = {
            column["name"]: column for column in inspector.get_columns("document_assets")
        }
        extraction_columns = {
            column["name"]: column
            for column in inspector.get_columns("document_text_extractions")
        }
        assert not asset_columns["content_sha256"]["nullable"]
        assert not asset_columns["warnings_json"]["nullable"]
        assert extraction_columns["text_object_key"]["nullable"]
        assert not extraction_columns["page_map_json"]["nullable"]
        assert {key["referred_table"] for key in inspector.get_foreign_keys("document_assets")} == {
            "documents"
        }
        assert {
            key["referred_table"]
            for key in inspector.get_foreign_keys("document_text_extractions")
        } == {"document_assets"}
        asset_uniques = inspector.get_unique_constraints("document_assets")
        assert frozenset({"document_id", "content_sha256"}) in {
            frozenset(value["column_names"]) for value in asset_uniques
        }
        extraction_uniques = inspector.get_unique_constraints("document_text_extractions")
        extraction_identity = frozenset(
            {
                "document_asset_id",
                "extractor_code",
                "extractor_semantic_version",
                "extractor_runtime_version",
            }
        )
        assert extraction_identity in {
            frozenset(value["column_names"]) for value in extraction_uniques
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260928_0011")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not phase6b_tables.intersection(tables)
        assert {"announcements", "documents", "announcement_documents"}.issubset(tables)
    finally:
        engine.dispose()

    command.upgrade(config, "20260928_0012")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert phase6b_tables.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_phase_6ca_business_event_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-6ca-business-events.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    phase6ca_tables = {"business_events", "business_event_evidence"}

    command.upgrade(config, "20260928_0012")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert not phase6ca_tables.intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "20260928_0013")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert phase6ca_tables.issubset(inspector.get_table_names())
        event_columns = {
            column["name"]: column for column in inspector.get_columns("business_events")
        }
        evidence_columns = {
            column["name"]: column
            for column in inspector.get_columns("business_event_evidence")
        }
        assert not event_columns["detection_fingerprint_sha256"]["nullable"]
        assert event_columns["security_id"]["nullable"]
        assert not evidence_columns["excerpt_sha256"]["nullable"]
        assert evidence_columns["document_id"]["nullable"]
        assert {
            key["referred_table"] for key in inspector.get_foreign_keys("business_events")
        } == {"companies", "securities", "announcements", "provider_datasets"}
        assert {
            key["referred_table"]
            for key in inspector.get_foreign_keys("business_event_evidence")
        } == {
            "business_events",
            "announcements",
            "documents",
            "document_assets",
            "document_text_extractions",
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260928_0012")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not phase6ca_tables.intersection(tables)
        assert {"document_assets", "document_text_extractions"}.issubset(tables)
    finally:
        engine.dispose()

    command.upgrade(config, "20260928_0013")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert phase6ca_tables.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_phase_6cb_quantitative_facts_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-6cb-quantitative-facts.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    phase6cb_tables = {
        "business_event_quantitative_derivations",
        "business_event_quantitative_facts",
    }

    command.upgrade(config, "20260928_0013")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert not phase6cb_tables.intersection(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0014")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert phase6cb_tables.issubset(inspector.get_table_names())
        derivation_columns = {
            column["name"]: column
            for column in inspector.get_columns("business_event_quantitative_derivations")
        }
        fact_columns = {
            column["name"]: column
            for column in inspector.get_columns("business_event_quantitative_facts")
        }
        assert not derivation_columns["available_fact_codes_json"]["nullable"]
        assert not derivation_columns["derivation_fingerprint_sha256"]["nullable"]
        assert fact_columns["reported_value"]["nullable"]
        assert fact_columns["date_value"]["nullable"]
        assert {
            key["referred_table"]
            for key in inspector.get_foreign_keys(
                "business_event_quantitative_derivations"
            )
        } == {"business_events"}
        assert {
            key["referred_table"]
            for key in inspector.get_foreign_keys("business_event_quantitative_facts")
        } == {
            "business_event_quantitative_derivations",
            "business_event_evidence",
        }
    finally:
        engine.dispose()

    command.downgrade(config, "20260928_0013")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert not phase6cb_tables.intersection(tables)
        assert {"business_events", "business_event_evidence"}.issubset(tables)
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0014")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert phase6cb_tables.issubset(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_phase_6da_attention_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "phase-6da-attention.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "20260929_0014")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "attention_observations" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0015")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(engine)
        assert "attention_observations" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("attention_observations")
        }
        assert columns["security_id"]["nullable"]
        assert not columns["available_at"]["nullable"]
        assert not columns["reported_count"]["nullable"]
        assert {
            key["referred_table"]
            for key in inspector.get_foreign_keys("attention_observations")
        } == {"companies", "securities", "provider_datasets", "source_records"}
        assert any(
            constraint["column_names"] == ["source_record_id"]
            for constraint in inspector.get_unique_constraints("attention_observations")
        )
    finally:
        engine.dispose()

    command.downgrade(config, "20260929_0014")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "attention_observations" not in inspect(engine).get_table_names()
        assert "business_event_quantitative_facts" in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, "20260929_0015")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "attention_observations" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
