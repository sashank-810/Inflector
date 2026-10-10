"""Add append-only frozen-prediction multibagger evaluation records.

Revision ID: 20261005_0024
Revises: 20261004_0023
"""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0024"
down_revision = "20261004_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "multibagger_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("evaluation_policy_code", sa.String(120), nullable=False),
        sa.Column("evaluation_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("backtest_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("universe_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("outcome_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("research_profile_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("model_family", sa.String(120), nullable=False),
        sa.Column("scoring_configuration_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_bundle_checksums_json", sa.JSON(), nullable=False),
        sa.Column("selection_contracts_json", sa.JSON(), nullable=False),
        sa.Column("outcome_contracts_json", sa.JSON(), nullable=False),
        sa.Column("algorithm_versions_json", sa.JSON(), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','completed','failed')",
            name="ck_multibagger_evaluation_run_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_multibagger_evaluation_run_key"),
    )
    op.create_index(
        "ix_multibagger_evaluation_runs_run_key_sha256",
        "multibagger_evaluation_runs",
        ["run_key_sha256"],
    )
    op.create_table(
        "multibagger_evaluation_cohorts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("historical_universe_run_id", sa.Uuid(), nullable=False),
        sa.Column("historical_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("eligible_resolved_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False),
        sa.Column("unsupported_count", sa.Integer(), nullable=False),
        sa.Column("ordered_security_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("backtest_run_ids_json", sa.JSON(), nullable=False),
        sa.Column("label_run_ids_json", sa.JSON(), nullable=False),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bundle_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("rankable_count", sa.Integer(), nullable=False),
        sa.Column("unrankable_count", sa.Integer(), nullable=False),
        sa.Column("calendar_integrity_status", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('completed','failed')", name="ck_multibagger_evaluation_cohort_status"
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_evaluation_run_id"],
            ["multibagger_evaluation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["historical_universe_run_id"], ["historical_universe_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "multibagger_evaluation_run_id",
            "historical_universe_run_id",
            name="uq_multibagger_evaluation_cohort_universe",
        ),
    )
    for column in (
        "multibagger_evaluation_run_id",
        "historical_universe_run_id",
        "bundle_checksum_sha256",
    ):
        op.create_index(
            f"ix_multibagger_evaluation_cohorts_{column}",
            "multibagger_evaluation_cohorts",
            [column],
        )
    op.create_table(
        "multibagger_prediction_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_evaluation_cohort_id", sa.Uuid(), nullable=False),
        sa.Column("backtest_observation_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("historical_symbol", sa.String(64), nullable=False),
        sa.Column("score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column("snapshot_status", sa.String(64), nullable=True),
        sa.Column("rankable", sa.Boolean(), nullable=False),
        sa.Column("unranked_reason", sa.String(96), nullable=True),
        sa.Column("final_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("dense_score_rank", sa.Integer(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("available_component_codes_json", sa.JSON(), nullable=False),
        sa.Column("prediction_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(rankable = true AND final_score IS NOT NULL AND dense_score_rank IS NOT NULL "
            "AND display_order IS NOT NULL AND unranked_reason IS NULL) OR "
            "(rankable = false AND final_score IS NULL AND dense_score_rank IS NULL "
            "AND display_order IS NULL AND unranked_reason IS NOT NULL)",
            name="ck_multibagger_prediction_rankability",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_evaluation_cohort_id"],
            ["multibagger_evaluation_cohorts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["backtest_observation_id"], ["backtest_observations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "multibagger_evaluation_cohort_id",
            "security_id",
            name="uq_multibagger_prediction_cohort_security",
        ),
    )
    for column in (
        "multibagger_evaluation_cohort_id",
        "backtest_observation_id",
        "company_id",
        "security_id",
        "prediction_fingerprint_sha256",
        "score_snapshot_id",
    ):
        op.create_index(
            f"ix_multibagger_prediction_rows_{column}", "multibagger_prediction_rows", [column]
        )
    op.create_table(
        "multibagger_confusion_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_prediction_row_id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_outcome_label_id", sa.Uuid(), nullable=False),
        sa.Column("outcome_contract_code", sa.String(32), nullable=False),
        sa.Column("selection_contract_code", sa.String(32), nullable=False),
        sa.Column("evaluation_view", sa.String(32), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("actual_label", sa.String(32), nullable=False),
        sa.Column("confusion_class", sa.String(64), nullable=False),
        sa.Column("threshold_hit_trading_date", sa.Date(), nullable=True),
        sa.Column("endpoint_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("peak_price_multiple", sa.Numeric(50, 28), nullable=True),
        sa.Column("maximum_drawdown", sa.Numeric(50, 28), nullable=True),
        sa.Column("label_provenance_json", sa.JSON(), nullable=False),
        sa.Column("confusion_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "evaluation_view IN ('end_to_end','rankable_only')",
            name="ck_multibagger_confusion_view",
        ),
        sa.CheckConstraint(
            "actual_label IN ('positive','negative','unmatured','unavailable')",
            name="ck_multibagger_confusion_actual_label",
        ),
        sa.CheckConstraint(
            "confusion_class IN ('tp','fp','fn','tn','excluded_unmatured',"
            "'excluded_unavailable','excluded_rankability_for_rankable_only')",
            name="ck_multibagger_confusion_class",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_prediction_row_id"],
            ["multibagger_prediction_rows.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_outcome_label_id"], ["multibagger_outcome_labels.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "multibagger_prediction_row_id",
            "outcome_contract_code",
            "selection_contract_code",
            "evaluation_view",
            name="uq_multibagger_confusion_identity",
        ),
    )
    for column in (
        "multibagger_prediction_row_id",
        "multibagger_outcome_label_id",
        "confusion_class",
        "confusion_fingerprint_sha256",
    ):
        op.create_index(
            f"ix_multibagger_confusion_rows_{column}", "multibagger_confusion_rows", [column]
        )


def downgrade() -> None:
    for column in (
        "confusion_fingerprint_sha256",
        "confusion_class",
        "multibagger_outcome_label_id",
        "multibagger_prediction_row_id",
    ):
        op.drop_index(
            f"ix_multibagger_confusion_rows_{column}", table_name="multibagger_confusion_rows"
        )
    op.drop_table("multibagger_confusion_rows")
    for column in (
        "score_snapshot_id",
        "prediction_fingerprint_sha256",
        "security_id",
        "company_id",
        "backtest_observation_id",
        "multibagger_evaluation_cohort_id",
    ):
        op.drop_index(
            f"ix_multibagger_prediction_rows_{column}", table_name="multibagger_prediction_rows"
        )
    op.drop_table("multibagger_prediction_rows")
    for column in (
        "bundle_checksum_sha256",
        "historical_universe_run_id",
        "multibagger_evaluation_run_id",
    ):
        op.drop_index(
            f"ix_multibagger_evaluation_cohorts_{column}",
            table_name="multibagger_evaluation_cohorts",
        )
    op.drop_table("multibagger_evaluation_cohorts")
    op.drop_index(
        "ix_multibagger_evaluation_runs_run_key_sha256", table_name="multibagger_evaluation_runs"
    )
    op.drop_table("multibagger_evaluation_runs")
