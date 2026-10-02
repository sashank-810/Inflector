"""Add immutable official market delivery observations.

Revision ID: 20261002_0017
Revises: 20261002_0016
"""

import sqlalchemy as sa
from alembic import op

revision = "20261002_0017"
down_revision = "20261002_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_delivery_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("series", sa.String(16), nullable=False),
        sa.Column("total_traded_quantity", sa.BigInteger(), nullable=True),
        sa.Column("delivery_quantity", sa.BigInteger(), nullable=True),
        sa.Column("reported_delivery_percentage", sa.Numeric(50, 28), nullable=True),
        sa.Column("delivery_percentage", sa.Numeric(50, 28), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index(
        "ix_market_delivery_observations_security_id",
        "market_delivery_observations",
        ["security_id"],
    )
    op.create_index(
        "ix_market_delivery_observations_provider_dataset_id",
        "market_delivery_observations",
        ["provider_dataset_id"],
    )
    op.create_index(
        "ix_market_delivery_observations_trading_date",
        "market_delivery_observations",
        ["trading_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_delivery_observations_trading_date",
        table_name="market_delivery_observations",
    )
    op.drop_index(
        "ix_market_delivery_observations_provider_dataset_id",
        table_name="market_delivery_observations",
    )
    op.drop_index(
        "ix_market_delivery_observations_security_id",
        table_name="market_delivery_observations",
    )
    op.drop_table("market_delivery_observations")
