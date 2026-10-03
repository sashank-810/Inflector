"""Add immutable current opportunity discovery runs and items.

Revision ID: 20261003_0019
Revises: 20261003_0018
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0019"
down_revision = "20261003_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_discovery_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("discovery_policy_code", sa.String(120), nullable=False),
        sa.Column("discovery_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("scoring_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_configuration_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("research_profile_code", sa.String(120), nullable=False),
        sa.Column("research_profile_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("financial_primitive_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("financial_endpoint_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("model_family", sa.String(120), nullable=False),
        sa.Column("discovery_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_mode", sa.String(64), nullable=False),
        sa.Column("ordered_symbols_json", sa.JSON(), nullable=False),
        sa.Column("symbol_set_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("selected_snapshot_set_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_selection_version", sa.String(96), nullable=False),
        sa.Column("ranking_version", sa.String(96), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','completed','failed')",
            name="ck_opportunity_discovery_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_configuration_id"], ["scoring_configurations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_opportunity_discovery_run_key"),
    )
    op.create_index(
        "ix_opportunity_discovery_runs_run_key_sha256",
        "opportunity_discovery_runs",
        ["run_key_sha256"],
    )
    op.create_index(
        "ix_opportunity_discovery_runs_scoring_configuration_id",
        "opportunity_discovery_runs",
        ["scoring_configuration_id"],
    )
    op.create_index(
        "ix_opportunity_discovery_runs_discovery_cutoff",
        "opportunity_discovery_runs",
        ["discovery_cutoff"],
    )

    op.create_table(
        "opportunity_discovery_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_discovery_run_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("rankable", sa.Boolean(), nullable=False),
        sa.Column("unranked_reason", sa.String(64), nullable=True),
        sa.Column("score_rank", sa.Integer(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("snapshot_age_days", sa.Integer(), nullable=True),
        sa.Column("freshness_state", sa.String(32), nullable=False),
        sa.Column("selected_snapshot_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(rankable = true AND unranked_reason IS NULL AND score_rank IS NOT NULL "
            "AND display_order IS NOT NULL) OR "
            "(rankable = false AND unranked_reason IS NOT NULL AND score_rank IS NULL "
            "AND display_order IS NULL)",
            name="ck_opportunity_discovery_item_rankability",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["opportunity_discovery_run_id"],
            ["opportunity_discovery_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "opportunity_discovery_run_id",
            "symbol",
            name="uq_opportunity_discovery_item_symbol",
        ),
    )
    for column in (
        "opportunity_discovery_run_id",
        "company_id",
        "security_id",
        "score_snapshot_id",
    ):
        op.create_index(
            f"ix_opportunity_discovery_items_{column}",
            "opportunity_discovery_items",
            [column],
        )


def downgrade() -> None:
    for column in (
        "score_snapshot_id",
        "security_id",
        "company_id",
        "opportunity_discovery_run_id",
    ):
        op.drop_index(
            f"ix_opportunity_discovery_items_{column}",
            table_name="opportunity_discovery_items",
        )
    op.drop_table("opportunity_discovery_items")
    op.drop_index(
        "ix_opportunity_discovery_runs_discovery_cutoff",
        table_name="opportunity_discovery_runs",
    )
    op.drop_index(
        "ix_opportunity_discovery_runs_scoring_configuration_id",
        table_name="opportunity_discovery_runs",
    )
    op.drop_index(
        "ix_opportunity_discovery_runs_run_key_sha256",
        table_name="opportunity_discovery_runs",
    )
    op.drop_table("opportunity_discovery_runs")
