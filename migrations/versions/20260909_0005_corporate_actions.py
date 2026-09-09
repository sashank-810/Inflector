"""Create Phase 2C corporate-action and security-succession storage.

Revision ID: 20260909_0005
Revises: 20260909_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0005"
down_revision = "20260909_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("announcement_date", sa.Date(), nullable=True),
        sa.Column("ex_date", sa.Date(), nullable=True),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("ratio_numerator", sa.Integer(), nullable=True),
        sa.Column("ratio_denominator", sa.Integer(), nullable=True),
        sa.Column("cash_amount", sa.Numeric(28, 8), nullable=True),
        sa.Column("cash_currency", sa.String(3), nullable=True),
        sa.Column("cash_unit", sa.String(32), nullable=True),
        sa.Column("subscription_price", sa.Numeric(28, 8), nullable=True),
        sa.Column("subscription_currency", sa.String(3), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"]),
        sa.ForeignKeyConstraint(["provider_dataset_id"], ["provider_datasets.id"]),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index("ix_corporate_actions_security_id", "corporate_actions", ["security_id"])
    op.create_index(
        "ix_corporate_actions_provider_dataset_id", "corporate_actions", ["provider_dataset_id"]
    )
    op.create_index(
        "ix_corporate_actions_dates",
        "corporate_actions",
        ["ex_date", "effective_date", "available_at"],
    )
    op.create_table(
        "security_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_security_id", sa.Uuid(), nullable=False),
        sa.Column("successor_security_id", sa.Uuid(), nullable=False),
        sa.Column("relationship_type", sa.String(32), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["predecessor_security_id"], ["securities.id"]),
        sa.ForeignKeyConstraint(["successor_security_id"], ["securities.id"]),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "predecessor_security_id",
            "successor_security_id",
            "relationship_type",
            name="uq_security_relationship",
        ),
    )
    op.create_index(
        "ix_security_relationships_predecessor_security_id",
        "security_relationships",
        ["predecessor_security_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_security_relationships_predecessor_security_id", table_name="security_relationships"
    )
    op.drop_table("security_relationships")
    op.drop_index("ix_corporate_actions_dates", table_name="corporate_actions")
    op.drop_index("ix_corporate_actions_provider_dataset_id", table_name="corporate_actions")
    op.drop_index("ix_corporate_actions_security_id", table_name="corporate_actions")
    op.drop_table("corporate_actions")
