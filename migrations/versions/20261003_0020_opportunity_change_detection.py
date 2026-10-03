"""Add immutable opportunity discovery change runs and items.

Revision ID: 20261003_0020
Revises: 20261003_0019
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0020"
down_revision = "20261003_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "opportunity_change_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("change_policy_code", sa.String(120), nullable=False),
        sa.Column("change_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("baseline_discovery_run_id", sa.Uuid(), nullable=False),
        sa.Column("current_discovery_run_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("current_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("baseline_snapshot_set_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("current_snapshot_set_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("comparison_version", sa.String(96), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','completed','failed','incompatible_runs')",
            name="ck_opportunity_change_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_discovery_run_id"],
            ["opportunity_discovery_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_discovery_run_id"],
            ["opportunity_discovery_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_opportunity_change_run_key"),
    )
    for column in (
        "run_key_sha256",
        "baseline_discovery_run_id",
        "current_discovery_run_id",
    ):
        op.create_index(
            f"ix_opportunity_change_runs_{column}", "opportunity_change_runs", [column]
        )

    op.create_table(
        "opportunity_change_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_change_run_id", sa.Uuid(), nullable=False),
        sa.Column("identity_key", sa.String(160), nullable=False),
        sa.Column("identity_basis", sa.String(32), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_symbol", sa.String(64), nullable=True),
        sa.Column("current_symbol", sa.String(64), nullable=True),
        sa.Column("baseline_discovery_item_id", sa.Uuid(), nullable=True),
        sa.Column("current_discovery_item_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("current_score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("change_codes_json", sa.JSON(), nullable=False),
        sa.Column("changed", sa.Boolean(), nullable=False),
        sa.Column("baseline_rankable", sa.Boolean(), nullable=True),
        sa.Column("current_rankable", sa.Boolean(), nullable=True),
        sa.Column("baseline_unranked_reason", sa.String(64), nullable=True),
        sa.Column("current_unranked_reason", sa.String(64), nullable=True),
        sa.Column("baseline_final_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("current_final_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("score_delta", sa.Numeric(50, 28), nullable=True),
        sa.Column("baseline_score_rank", sa.Integer(), nullable=True),
        sa.Column("current_score_rank", sa.Integer(), nullable=True),
        sa.Column("rank_delta", sa.Integer(), nullable=True),
        sa.Column("baseline_confidence", sa.Numeric(50, 28), nullable=True),
        sa.Column("current_confidence", sa.Numeric(50, 28), nullable=True),
        sa.Column("confidence_delta", sa.Numeric(50, 28), nullable=True),
        sa.Column("components_gained_json", sa.JSON(), nullable=False),
        sa.Column("components_lost_json", sa.JSON(), nullable=False),
        sa.Column("component_change_detail_json", sa.JSON(), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["opportunity_change_run_id"],
            ["opportunity_change_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_discovery_item_id"],
            ["opportunity_discovery_items.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["current_discovery_item_id"],
            ["opportunity_discovery_items.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["current_score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "opportunity_change_run_id",
            "identity_key",
            name="uq_opportunity_change_item_identity",
        ),
    )
    for column in (
        "opportunity_change_run_id",
        "company_id",
        "security_id",
        "baseline_discovery_item_id",
        "current_discovery_item_id",
        "baseline_score_snapshot_id",
        "current_score_snapshot_id",
    ):
        op.create_index(
            f"ix_opportunity_change_items_{column}", "opportunity_change_items", [column]
        )


def downgrade() -> None:
    for column in (
        "current_score_snapshot_id",
        "baseline_score_snapshot_id",
        "current_discovery_item_id",
        "baseline_discovery_item_id",
        "security_id",
        "company_id",
        "opportunity_change_run_id",
    ):
        op.drop_index(
            f"ix_opportunity_change_items_{column}", table_name="opportunity_change_items"
        )
    op.drop_table("opportunity_change_items")
    for column in (
        "current_discovery_run_id",
        "baseline_discovery_run_id",
        "run_key_sha256",
    ):
        op.drop_index(
            f"ix_opportunity_change_runs_{column}", table_name="opportunity_change_runs"
        )
    op.drop_table("opportunity_change_runs")
