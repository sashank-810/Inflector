"""Add append-only external attention observations.

Revision ID: 20260929_0015
Revises: 20260929_0014
"""

import sqlalchemy as sa
from alembic import op

revision = "20260929_0015"
down_revision = "20260929_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attention_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("metric_code", sa.String(length=64), nullable=False),
        sa.Column("reported_count", sa.BigInteger(), nullable=False),
        sa.Column("reported_unit", sa.String(length=32), nullable=False),
        sa.Column("scope_code", sa.String(length=120), nullable=False),
        sa.Column("methodology_version", sa.String(length=120), nullable=False),
        sa.Column("measurement_definition_sha256", sa.String(length=64), nullable=False),
        sa.Column("coverage_status", sa.String(length=32), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=True),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"], ["source_records.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    for column in (
        "company_id",
        "security_id",
        "provider_dataset_id",
        "metric_code",
        "observation_date",
        "window_end_at",
        "available_at",
    ):
        op.create_index(
            f"ix_attention_observations_{column}",
            "attention_observations",
            [column],
        )


def downgrade() -> None:
    for column in reversed(
        (
            "company_id",
            "security_id",
            "provider_dataset_id",
            "metric_code",
            "observation_date",
            "window_end_at",
            "available_at",
        )
    ):
        op.drop_index(
            f"ix_attention_observations_{column}",
            table_name="attention_observations",
        )
    op.drop_table("attention_observations")
