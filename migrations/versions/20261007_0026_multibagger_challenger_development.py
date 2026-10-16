"""Add governed multibagger challenger development persistence.

Revision ID: 20261007_0026
Revises: 20261006_0025
"""

import sqlalchemy as sa
from alembic import op

revision = "20261007_0026"
down_revision = "20261006_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "multibagger_challenger_experiment_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("policy_code", sa.String(120), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("activation_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("temporal_split_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("error_analysis_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("temporal_split_run_id", sa.Uuid(), nullable=False),
        sa.Column("temporal_split_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("error_analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_evaluation_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("champion_scoring_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("champion_scoring_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("champion_model_family", sa.String(120), nullable=False),
        sa.Column("development_cutoff_ids_json", sa.JSON(), nullable=False),
        sa.Column("validation_cutoff_ids_json", sa.JSON(), nullable=False),
        sa.Column("holdout_cutoff_ids_json", sa.JSON(), nullable=False),
        sa.Column("candidate_algorithm_version", sa.String(120), nullable=False),
        sa.Column("primary_objective_json", sa.JSON(), nullable=False),
        sa.Column("evidence_class", sa.String(48), nullable=False),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column("sealed_candidate_set_checksum_sha256", sa.String(64), nullable=True),
        sa.Column("validation_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("holdout_outcomes_accessed", sa.Boolean(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','development_evaluating','development_evaluated',"
            "'candidate_set_sealed','validation_evaluating','validation_evaluated',"
            "'candidate_frozen_for_t','no_challenger_selected','data_blocked','failed')",
            name="ck_multibagger_challenger_experiment_status",
        ),
        sa.CheckConstraint(
            "holdout_outcomes_accessed = false", name="ck_multibagger_challenger_no_holdout_access"
        ),
        sa.ForeignKeyConstraint(
            ["temporal_split_run_id"], ["multibagger_temporal_split_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["error_analysis_run_id"], ["multibagger_error_analysis_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_run_id"], ["multibagger_evaluation_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["champion_scoring_configuration_id"],
            ["scoring_configurations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_multibagger_challenger_experiment_key"),
    )
    for column in (
        "run_key_sha256",
        "temporal_split_run_id",
        "error_analysis_run_id",
        "source_evaluation_run_id",
        "champion_scoring_configuration_id",
    ):
        op.create_index(
            f"ix_multibagger_challenger_experiment_runs_{column}",
            "multibagger_challenger_experiment_runs",
            [column],
        )

    op.create_table(
        "multibagger_challenger_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_code", sa.String(120), nullable=False),
        sa.Column("candidate_key_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_weights_json", sa.JSON(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("source_error_analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("development_evidence_json", sa.JSON(), nullable=False),
        sa.Column("development_evidence_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(120), nullable=False),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('registered','development_evaluated','sealed','validation_evaluated',"
            "'eligible_for_t','not_eligible')",
            name="ck_multibagger_challenger_candidate_status",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_error_analysis_run_id"],
            ["multibagger_error_analysis_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_key_sha256", name="uq_multibagger_challenger_candidates_candidate_key_sha256"
        ),
        sa.UniqueConstraint(
            "experiment_run_id", "candidate_code", name="uq_multibagger_challenger_candidate_code"
        ),
        sa.UniqueConstraint(
            "experiment_run_id",
            "candidate_checksum_sha256",
            name="uq_multibagger_challenger_candidate_checksum",
        ),
    )
    for column in ("experiment_run_id", "candidate_checksum_sha256"):
        op.create_index(
            f"ix_multibagger_challenger_candidates_{column}",
            "multibagger_challenger_candidates",
            [column],
        )

    op.create_table(
        "multibagger_challenger_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("split_code", sa.String(32), nullable=False),
        sa.Column("source_evaluation_cohort_id", sa.Uuid(), nullable=False),
        sa.Column("source_prediction_row_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("historical_symbol", sa.String(64), nullable=False),
        sa.Column("source_score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("source_snapshot_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column("component_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("candidate_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("rankable", sa.Boolean(), nullable=False),
        sa.Column("unranked_reason", sa.String(96), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("dense_score_rank", sa.Integer(), nullable=True),
        sa.Column("rankable_count", sa.Integer(), nullable=False),
        sa.Column("cohort_size", sa.Integer(), nullable=False),
        sa.Column("selection_flags_json", sa.JSON(), nullable=False),
        sa.Column("selection_boundary_ties_json", sa.JSON(), nullable=False),
        sa.Column("score_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "split_code IN ('development','validation')",
            name="ck_multibagger_challenger_score_split",
        ),
        sa.CheckConstraint(
            "(rankable = true AND candidate_score IS NOT NULL AND display_order IS NOT NULL "
            "AND dense_score_rank IS NOT NULL AND unranked_reason IS NULL) OR "
            "(rankable = false AND candidate_score IS NULL AND display_order IS NULL "
            "AND dense_score_rank IS NULL AND unranked_reason IS NOT NULL)",
            name="ck_multibagger_challenger_score_rankability",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["multibagger_challenger_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_cohort_id"],
            ["multibagger_evaluation_cohorts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_prediction_row_id"], ["multibagger_prediction_rows.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id", "source_prediction_row_id", name="uq_multibagger_challenger_score"
        ),
    )
    for column in ("experiment_run_id", "candidate_id", "split_code", "score_fingerprint_sha256"):
        op.create_index(
            f"ix_multibagger_challenger_scores_{column}", "multibagger_challenger_scores", [column]
        )

    op.create_table(
        "multibagger_challenger_evaluation_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_score_id", sa.Uuid(), nullable=False),
        sa.Column("source_confusion_row_id", sa.Uuid(), nullable=False),
        sa.Column("split_code", sa.String(32), nullable=False),
        sa.Column("outcome_contract_code", sa.String(32), nullable=False),
        sa.Column("selection_contract_code", sa.String(32), nullable=False),
        sa.Column("evaluation_view", sa.String(32), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("actual_label", sa.String(32), nullable=False),
        sa.Column("confusion_class", sa.String(64), nullable=False),
        sa.Column("row_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "split_code IN ('development','validation')",
            name="ck_multibagger_challenger_evaluation_split",
        ),
        sa.CheckConstraint(
            "evaluation_view IN ('end_to_end','rankable_only')",
            name="ck_multibagger_challenger_evaluation_view",
        ),
        sa.CheckConstraint(
            "confusion_class IN ('tp','fp','fn','tn','excluded_unmatured',"
            "'excluded_unavailable','excluded_rankability_for_rankable_only')",
            name="ck_multibagger_challenger_evaluation_confusion",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["multibagger_challenger_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_score_id"], ["multibagger_challenger_scores.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_confusion_row_id"], ["multibagger_confusion_rows.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_score_id",
            "outcome_contract_code",
            "selection_contract_code",
            "evaluation_view",
            name="uq_multibagger_challenger_evaluation_row",
        ),
    )
    for column in ("experiment_run_id", "split_code", "row_fingerprint_sha256"):
        op.create_index(
            f"ix_multibagger_challenger_evaluation_rows_{column}",
            "multibagger_challenger_evaluation_rows",
            [column],
        )

    op.create_table(
        "multibagger_challenger_metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("split_code", sa.String(32), nullable=False),
        sa.Column("outcome_contract_code", sa.String(32), nullable=False),
        sa.Column("selection_contract_code", sa.String(32), nullable=False),
        sa.Column("evaluation_view", sa.String(32), nullable=False),
        sa.Column("candidate_metrics_json", sa.JSON(), nullable=False),
        sa.Column("champion_metrics_json", sa.JSON(), nullable=False),
        sa.Column("delta_metrics_json", sa.JSON(), nullable=False),
        sa.Column("rankability_delta", sa.Integer(), nullable=False),
        sa.Column("coverage_delta", sa.Integer(), nullable=False),
        sa.Column("metric_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "split_code IN ('development','validation')",
            name="ck_multibagger_challenger_metric_split",
        ),
        sa.CheckConstraint(
            "rankability_delta = 0", name="ck_multibagger_challenger_rankability_delta"
        ),
        sa.CheckConstraint("coverage_delta = 0", name="ck_multibagger_challenger_coverage_delta"),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["multibagger_challenger_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_id",
            "split_code",
            "outcome_contract_code",
            "selection_contract_code",
            "evaluation_view",
            name="uq_multibagger_challenger_metric_cell",
        ),
    )
    for column in ("experiment_run_id", "split_code", "metric_fingerprint_sha256"):
        op.create_index(
            f"ix_multibagger_challenger_metrics_{column}",
            "multibagger_challenger_metrics",
            [column],
        )

    op.create_table(
        "multibagger_challenger_freezes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("experiment_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("freeze_key_sha256", sa.String(64), nullable=False),
        sa.Column("freeze_purpose", sa.String(64), nullable=False),
        sa.Column("production_readiness_status", sa.String(32), nullable=False),
        sa.Column("validation_gate_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "freeze_purpose = 'candidate_for_production_t'",
            name="ck_multibagger_challenger_freeze_purpose",
        ),
        sa.CheckConstraint(
            "production_readiness_status = 'ready'",
            name="ck_multibagger_challenger_freeze_readiness",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["multibagger_challenger_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_run_id", name="uq_multibagger_challenger_freeze_experiment"
        ),
        sa.UniqueConstraint("freeze_key_sha256", name="uq_multibagger_challenger_freeze_key"),
    )


def downgrade() -> None:
    for table, indexes in (
        ("multibagger_challenger_freezes", ()),
        (
            "multibagger_challenger_metrics",
            ("metric_fingerprint_sha256", "split_code", "experiment_run_id"),
        ),
        (
            "multibagger_challenger_evaluation_rows",
            ("row_fingerprint_sha256", "split_code", "experiment_run_id"),
        ),
        (
            "multibagger_challenger_scores",
            ("score_fingerprint_sha256", "split_code", "candidate_id", "experiment_run_id"),
        ),
        ("multibagger_challenger_candidates", ("candidate_checksum_sha256", "experiment_run_id")),
        (
            "multibagger_challenger_experiment_runs",
            (
                "champion_scoring_configuration_id",
                "source_evaluation_run_id",
                "error_analysis_run_id",
                "temporal_split_run_id",
                "run_key_sha256",
            ),
        ),
    ):
        for column in indexes:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
