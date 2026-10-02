"""Add production operational run, stage, and symbol ledgers.

Revision ID: 20261002_0016
Revises: 20260929_0015
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261002_0016"
down_revision = "20260929_0015"
branch_labels = None
depends_on = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "operational_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("operations_profile_code", sa.String(120), nullable=False),
        sa.Column("operations_profile_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("research_profile_code", sa.String(120), nullable=False),
        sa.Column("research_profile_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("model_family", sa.String(120), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=False),
        sa.Column("cycle_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol_set_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_symbols_json", _json_type(), nullable=False),
        sa.Column("inputs_json", _json_type(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner_token", sa.String(128), nullable=True),
        sa.Column("lease_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_json", _json_type(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','running','completed','completed_with_symbol_failures',"
            "'failed','stale')",
            name="ck_operational_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_operational_run_key"),
    )
    op.create_index("ix_operational_runs_run_key_sha256", "operational_runs", ["run_key_sha256"])

    op.create_table(
        "operational_run_stages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operational_run_id", sa.Uuid(), nullable=False),
        sa.Column("stage_name", sa.String(64), nullable=False),
        sa.Column("stage_order", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_summary_json", _json_type(), nullable=False),
        sa.Column("attempt_history_json", _json_type(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','running','completed','failed','blocked','skipped')",
            name="ck_operational_stage_status",
        ),
        sa.ForeignKeyConstraint(
            ["operational_run_id"], ["operational_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operational_run_id", "stage_name", name="uq_operational_run_stage"),
    )
    op.create_index(
        "ix_operational_run_stages_operational_run_id",
        "operational_run_stages",
        ["operational_run_id"],
    )

    op.create_table(
        "operational_run_symbols",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operational_run_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("result_json", _json_type(), nullable=False),
        sa.Column("change_json", _json_type(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["operational_run_id"], ["operational_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operational_run_id", "symbol", name="uq_operational_run_symbol"),
    )
    op.create_index(
        "ix_operational_run_symbols_operational_run_id",
        "operational_run_symbols",
        ["operational_run_id"],
    )
    op.create_index(
        "ix_operational_run_symbols_snapshot_id", "operational_run_symbols", ["snapshot_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_operational_run_symbols_snapshot_id", table_name="operational_run_symbols")
    op.drop_index(
        "ix_operational_run_symbols_operational_run_id", table_name="operational_run_symbols"
    )
    op.drop_table("operational_run_symbols")
    op.drop_index(
        "ix_operational_run_stages_operational_run_id", table_name="operational_run_stages"
    )
    op.drop_table("operational_run_stages")
    op.drop_index("ix_operational_runs_run_key_sha256", table_name="operational_runs")
    op.drop_table("operational_runs")
