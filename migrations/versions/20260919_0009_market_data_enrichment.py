"""Add raw market enrichment and benchmark bars.

Revision ID: 20260919_0009
Revises: 20260919_0008
"""

import sqlalchemy as sa
from alembic import op

revision = "20260919_0009"
down_revision = "20260919_0008"
branch_labels = None
depends_on = None


def _numeric():
    return sa.Numeric(50, 28).with_variant(sa.String(length=80), "sqlite")


def upgrade() -> None:
    with op.batch_alter_table("price_bars") as batch_op:
        batch_op.add_column(sa.Column("market_cap", _numeric(), nullable=True))
        batch_op.add_column(sa.Column("delivery_quantity", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("delivery_percentage", _numeric(), nullable=True))

    op.create_table(
        "benchmark_series",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_dataset_id", "code", name="uq_benchmark_series_dataset_code"),
    )
    op.create_index(
        "ix_benchmark_series_provider_dataset_id",
        "benchmark_series",
        ["provider_dataset_id"],
    )

    op.create_table(
        "benchmark_bars",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("benchmark_series_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("open_value", _numeric(), nullable=False),
        sa.Column("high_value", _numeric(), nullable=False),
        sa.Column("low_value", _numeric(), nullable=False),
        sa.Column("close_value", _numeric(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_series_id"], ["benchmark_series.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index(
        "ix_benchmark_bars_benchmark_series_id",
        "benchmark_bars",
        ["benchmark_series_id"],
    )
    op.create_index("ix_benchmark_bars_trading_date", "benchmark_bars", ["trading_date"])


def downgrade() -> None:
    op.drop_index("ix_benchmark_bars_trading_date", table_name="benchmark_bars")
    op.drop_index("ix_benchmark_bars_benchmark_series_id", table_name="benchmark_bars")
    op.drop_table("benchmark_bars")
    op.drop_index("ix_benchmark_series_provider_dataset_id", table_name="benchmark_series")
    op.drop_table("benchmark_series")

    with op.batch_alter_table("price_bars") as batch_op:
        batch_op.drop_column("delivery_percentage")
        batch_op.drop_column("delivery_quantity")
        batch_op.drop_column("market_cap")
