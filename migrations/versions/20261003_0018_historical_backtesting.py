"""Add immutable strict-PIT backtest runs, observations, and outcomes.

Revision ID: 20261003_0018
Revises: 20261002_0017
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0018"
down_revision = "20261002_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("backtest_policy_code", sa.String(120), nullable=False),
        sa.Column("backtest_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("availability_manifest_code", sa.String(120), nullable=False),
        sa.Column("availability_manifest_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("scoring_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_configuration_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("research_profile_code", sa.String(120), nullable=False),
        sa.Column("research_profile_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("financial_primitive_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("financial_endpoint_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("source_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("model_family", sa.String(120), nullable=False),
        sa.Column("cutoff_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_cadence", sa.String(64), nullable=False),
        sa.Column("universe_policy", sa.String(64), nullable=False),
        sa.Column("benchmark_code", sa.String(64), nullable=False),
        sa.Column("return_horizons_json", sa.JSON(), nullable=False),
        sa.Column("ordered_symbols_json", sa.JSON(), nullable=False),
        sa.Column("symbol_set_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','snapshots_built','outcomes_built','completed','failed')",
            name="ck_backtest_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["scoring_configuration_id"], ["scoring_configurations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_backtest_run_key"),
    )
    op.create_index("ix_backtest_runs_run_key_sha256", "backtest_runs", ["run_key_sha256"])
    op.create_index(
        "ix_backtest_runs_scoring_configuration_id",
        "backtest_runs",
        ["scoring_configuration_id"],
    )

    op.create_table(
        "backtest_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("backtest_run_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_status", sa.String(64), nullable=False),
        sa.Column("selected_fiscal_year", sa.Integer(), nullable=True),
        sa.Column("selected_fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column("selected_filing_scope", sa.String(32), nullable=True),
        sa.Column("selected_period_end", sa.Date(), nullable=True),
        sa.Column("snapshot_status", sa.String(64), nullable=True),
        sa.Column("snapshot_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column("research_state_projection_version", sa.String(96), nullable=False),
        sa.Column("research_state_sha256", sa.String(64), nullable=False),
        sa.Column("research_state_changed", sa.Boolean(), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["backtest_run_id"], ["backtest_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backtest_run_id",
            "security_id",
            "knowledge_cutoff",
            name="uq_backtest_observation_identity",
        ),
    )
    for column in (
        "backtest_run_id",
        "company_id",
        "security_id",
        "score_snapshot_id",
        "knowledge_cutoff",
    ):
        op.create_index(f"ix_backtest_observations_{column}", "backtest_observations", [column])

    op.create_table(
        "backtest_outcomes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("backtest_observation_id", sa.Uuid(), nullable=False),
        sa.Column("horizon_observations", sa.Integer(), nullable=False),
        sa.Column("outcome_status", sa.String(64), nullable=False),
        sa.Column("entry_trading_date", sa.Date(), nullable=True),
        sa.Column("exit_trading_date", sa.Date(), nullable=True),
        sa.Column("entry_adjusted_close", sa.Numeric(50, 28), nullable=True),
        sa.Column("exit_adjusted_close", sa.Numeric(50, 28), nullable=True),
        sa.Column("security_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("benchmark_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("excess_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["backtest_observation_id"], ["backtest_observations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backtest_observation_id",
            "horizon_observations",
            name="uq_backtest_outcome_horizon",
        ),
    )
    op.create_index(
        "ix_backtest_outcomes_backtest_observation_id",
        "backtest_outcomes",
        ["backtest_observation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_outcomes_backtest_observation_id", table_name="backtest_outcomes")
    op.drop_table("backtest_outcomes")
    for column in (
        "knowledge_cutoff",
        "score_snapshot_id",
        "security_id",
        "company_id",
        "backtest_run_id",
    ):
        op.drop_index(f"ix_backtest_observations_{column}", table_name="backtest_observations")
    op.drop_table("backtest_observations")
    op.drop_index("ix_backtest_runs_scoring_configuration_id", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_run_key_sha256", table_name="backtest_runs")
    op.drop_table("backtest_runs")
