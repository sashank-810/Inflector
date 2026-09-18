"""Add immutable model versions and scoring configurations.

Revision ID: 20260918_0007
Revises: 20260909_0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260918_0007"
down_revision = "20260909_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_family", sa.String(length=120), nullable=False),
        sa.Column("semantic_version", sa.String(length=64), nullable=False),
        sa.Column("git_sha", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_family",
            "semantic_version",
            name="uq_model_version_family_semantic",
        ),
    )
    policy_json = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "scoring_configurations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_name", sa.String(length=120), nullable=False),
        sa.Column("configuration_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("configuration_json", policy_json, nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_version_id",
            "configuration_name",
            "configuration_version",
            name="uq_scoring_configuration_version",
        ),
    )
    op.create_index(
        "ix_scoring_configurations_model_version_id",
        "scoring_configurations",
        ["model_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_scoring_configurations_checksum_sha256",
        "scoring_configurations",
        ["checksum_sha256"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scoring_configurations_checksum_sha256",
        table_name="scoring_configurations",
    )
    op.drop_index(
        "ix_scoring_configurations_model_version_id",
        table_name="scoring_configurations",
    )
    op.drop_table("scoring_configurations")
    op.drop_table("model_versions")
