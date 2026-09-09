"""Create Phase 2A provenance and append-only price-bar tables.

Revision ID: 20260909_0002
Revises: 20260908_0001
Create Date: 2026-09-09 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0002"
down_revision = "20260908_0001"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column[sa.DateTime], sa.Column[sa.DateTime]]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    """Create provider provenance, quality, and price-bar storage."""

    op.create_table(
        "data_providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("provider_type", sa.String(length=80), nullable=False),
        sa.Column("licence_name", sa.String(length=160), nullable=False),
        sa.Column("licence_reference", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "provider_datasets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=120), nullable=False),
        sa.Column("licence_class", sa.String(length=80), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("redistributable", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["provider_id"], ["data_providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "code", name="uq_provider_dataset_code"),
    )
    op.create_index("ix_provider_datasets_provider_id", "provider_datasets", ["provider_id"])
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("records_received", sa.Integer(), nullable=False),
        sa.Column("records_accepted", sa.Integer(), nullable=False),
        sa.Column("records_quarantined", sa.Integer(), nullable=False),
        sa.Column("records_duplicated", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_runs_correlation_id", "ingestion_runs", ["correlation_id"])
    op.create_index(
        "ix_ingestion_runs_provider_dataset_id", "ingestion_runs", ["provider_dataset_id"]
    )
    op.create_table(
        "source_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("external_record_id", sa.String(length=255), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("raw_object_key", sa.Text(), nullable=False),
        sa.Column("raw_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parse_status", sa.String(length=32), nullable=False),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_dataset_id",
            "external_record_id",
            "content_sha256",
            name="uq_source_record_content",
        ),
    )
    op.create_index("ix_source_records_ingestion_run_id", "source_records", ["ingestion_run_id"])
    op.create_index(
        "ix_source_records_provider_dataset_id", "source_records", ["provider_dataset_id"]
    )
    op.create_table(
        "data_quality_issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=True),
        sa.Column("rule_code", sa.String(length=120), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_data_quality_issues_ingestion_run_id", "data_quality_issues", ["ingestion_run_id"]
    )
    op.create_index(
        "ix_data_quality_issues_source_record_id", "data_quality_issues", ["source_record_id"]
    )
    op.create_table(
        "price_bars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("open_price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("high_price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("low_price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("close_price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index("ix_price_bars_security_id", "price_bars", ["security_id"])
    op.create_index("ix_price_bars_trading_date", "price_bars", ["trading_date"])
    op.create_index(
        "ix_price_bars_security_date_available",
        "price_bars",
        ["security_id", "trading_date", "available_at"],
    )


def downgrade() -> None:
    """Remove Phase 2A tables in dependency order."""

    op.drop_index("ix_price_bars_security_date_available", table_name="price_bars")
    op.drop_index("ix_price_bars_trading_date", table_name="price_bars")
    op.drop_index("ix_price_bars_security_id", table_name="price_bars")
    op.drop_table("price_bars")
    op.drop_index("ix_data_quality_issues_source_record_id", table_name="data_quality_issues")
    op.drop_index("ix_data_quality_issues_ingestion_run_id", table_name="data_quality_issues")
    op.drop_table("data_quality_issues")
    op.drop_index("ix_source_records_provider_dataset_id", table_name="source_records")
    op.drop_index("ix_source_records_ingestion_run_id", table_name="source_records")
    op.drop_table("source_records")
    op.drop_index("ix_ingestion_runs_provider_dataset_id", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_correlation_id", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index("ix_provider_datasets_provider_id", table_name="provider_datasets")
    op.drop_table("provider_datasets")
    op.drop_table("data_providers")
