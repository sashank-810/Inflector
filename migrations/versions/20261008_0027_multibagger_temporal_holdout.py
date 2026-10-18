"""Add immutable Production T temporal-holdout evaluation persistence.

Revision ID: 20261008_0027
Revises: 20261007_0026
"""

import sqlalchemy as sa
from alembic import op

revision = "20261008_0027"
down_revision = "20261007_0026"
branch_labels = None
depends_on = None


_SHORT_INDEX_NAMES = {
    (
        "multibagger_holdout_evaluation_runs",
        "champion_scoring_configuration_id",
    ): "ix_mb_holdout_run_champion_config",
}


def _index_name(table: str, column: str) -> str:
    return _SHORT_INDEX_NAMES.get((table, column), f"ix_{table}_{column}")


def upgrade() -> None:
    op.create_table(
        "multibagger_holdout_evaluation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("policy_code", sa.String(120), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("source_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_evaluation_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("temporal_split_run_id", sa.Uuid(), nullable=False),
        sa.Column("temporal_split_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("holdout_cohort_ids_json", sa.JSON(), nullable=False),
        sa.Column("holdout_membership_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("champion_scoring_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("champion_scoring_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("s2_experiment_run_id", sa.Uuid(), nullable=False),
        sa.Column("s2_experiment_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_class", sa.String(48), nullable=False),
        sa.Column("terminal_mode", sa.String(32), nullable=False),
        sa.Column("challenger_freeze_id", sa.Uuid(), nullable=True),
        sa.Column("challenger_freeze_key_sha256", sa.String(64), nullable=True),
        sa.Column("challenger_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("challenger_candidate_key_sha256", sa.String(64), nullable=True),
        sa.Column("challenger_candidate_checksum_sha256", sa.String(64), nullable=True),
        sa.Column("primary_objective_json", sa.JSON(), nullable=False),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score_selection_checksum_sha256", sa.String(64), nullable=True),
        sa.Column("holdout_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "terminal_mode IN ('champion_only','frozen_challenger')",
            name="ck_multibagger_holdout_terminal_mode",
        ),
        sa.CheckConstraint(
            "evidence_class IN ('production_authoritative','fixture_only')",
            name="ck_multibagger_holdout_evidence_class",
        ),
        sa.CheckConstraint(
            "status IN ('draft','scores_frozen','holdout_opened','evaluated','completed','failed')",
            name="ck_multibagger_holdout_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_run_id"], ["multibagger_evaluation_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["temporal_split_run_id"], ["multibagger_temporal_split_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["champion_scoring_configuration_id"],
            ["scoring_configurations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["s2_experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["challenger_freeze_id"], ["multibagger_challenger_freezes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["challenger_candidate_id"],
            ["multibagger_challenger_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_multibagger_holdout_run_key"),
    )
    for column in (
        "run_key_sha256",
        "source_evaluation_run_id",
        "temporal_split_run_id",
        "champion_scoring_configuration_id",
        "s2_experiment_run_id",
    ):
        op.create_index(
            _index_name("multibagger_holdout_evaluation_runs", column),
            "multibagger_holdout_evaluation_runs",
            [column],
        )

    op.create_table(
        "multibagger_holdout_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("holdout_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("source_evaluation_cohort_id", sa.Uuid(), nullable=False),
        sa.Column("source_prediction_row_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("historical_symbol", sa.String(64), nullable=False),
        sa.Column("source_score_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("source_prediction_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("source_snapshot_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column("component_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("score", sa.Numeric(50, 28), nullable=True),
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
            "subject_code IN ('champion','challenger')",
            name="ck_multibagger_holdout_score_subject",
        ),
        sa.CheckConstraint(
            "(rankable = true AND score IS NOT NULL AND display_order IS NOT NULL "
            "AND dense_score_rank IS NOT NULL AND unranked_reason IS NULL) OR "
            "(rankable = false AND score IS NULL AND display_order IS NULL "
            "AND dense_score_rank IS NULL AND unranked_reason IS NOT NULL)",
            name="ck_multibagger_holdout_score_rankability",
        ),
        sa.ForeignKeyConstraint(
            ["holdout_run_id"], ["multibagger_holdout_evaluation_runs.id"], ondelete="RESTRICT"
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
            "holdout_run_id",
            "subject_code",
            "source_prediction_row_id",
            name="uq_multibagger_holdout_score",
        ),
    )
    for column in ("holdout_run_id", "subject_code", "score_fingerprint_sha256"):
        op.create_index(
            _index_name("multibagger_holdout_scores", column),
            "multibagger_holdout_scores",
            [column],
        )

    op.create_table(
        "multibagger_holdout_evaluation_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("holdout_run_id", sa.Uuid(), nullable=False),
        sa.Column("holdout_score_id", sa.Uuid(), nullable=False),
        sa.Column("source_confusion_row_id", sa.Uuid(), nullable=False),
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
            "evaluation_view IN ('end_to_end','rankable_only')",
            name="ck_multibagger_holdout_evaluation_view",
        ),
        sa.CheckConstraint(
            "confusion_class IN ('tp','fp','fn','tn','excluded_unmatured',"
            "'excluded_unavailable','excluded_rankability_for_rankable_only')",
            name="ck_multibagger_holdout_evaluation_confusion",
        ),
        sa.ForeignKeyConstraint(
            ["holdout_run_id"], ["multibagger_holdout_evaluation_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["holdout_score_id"], ["multibagger_holdout_scores.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_confusion_row_id"], ["multibagger_confusion_rows.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "holdout_score_id",
            "outcome_contract_code",
            "selection_contract_code",
            "evaluation_view",
            name="uq_multibagger_holdout_evaluation_row",
        ),
    )
    for column in ("holdout_run_id", "row_fingerprint_sha256"):
        op.create_index(
            _index_name("multibagger_holdout_evaluation_rows", column),
            "multibagger_holdout_evaluation_rows",
            [column],
        )

    op.create_table(
        "multibagger_holdout_metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("holdout_run_id", sa.Uuid(), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("outcome_contract_code", sa.String(32), nullable=False),
        sa.Column("selection_contract_code", sa.String(32), nullable=False),
        sa.Column("evaluation_view", sa.String(32), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("champion_metrics_json", sa.JSON(), nullable=True),
        sa.Column("delta_metrics_json", sa.JSON(), nullable=True),
        sa.Column("rankability_delta", sa.Integer(), nullable=False),
        sa.Column("coverage_delta", sa.Integer(), nullable=False),
        sa.Column("selection_membership_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("metric_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "subject_code IN ('champion','challenger')",
            name="ck_multibagger_holdout_metric_subject",
        ),
        sa.ForeignKeyConstraint(
            ["holdout_run_id"], ["multibagger_holdout_evaluation_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "holdout_run_id",
            "subject_code",
            "outcome_contract_code",
            "selection_contract_code",
            "evaluation_view",
            name="uq_multibagger_holdout_metric_cell",
        ),
    )
    for column in ("holdout_run_id", "metric_fingerprint_sha256"):
        op.create_index(
            _index_name("multibagger_holdout_metrics", column),
            "multibagger_holdout_metrics",
            [column],
        )

    op.create_table(
        "multibagger_holdout_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("holdout_run_id", sa.Uuid(), nullable=False),
        sa.Column("decision_code", sa.String(64), nullable=False),
        sa.Column("decision_rule", sa.String(96), nullable=False),
        sa.Column("gate_json", sa.JSON(), nullable=False),
        sa.Column("production_promotion_performed", sa.Boolean(), nullable=False),
        sa.Column("decision_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "decision_code IN ('CHAMPION_RETAINED_NO_CHALLENGER',"
            "'CHALLENGER_PASSES_HOLDOUT','CHALLENGER_FAILS_HOLDOUT')",
            name="ck_multibagger_holdout_decision_code",
        ),
        sa.ForeignKeyConstraint(
            ["holdout_run_id"], ["multibagger_holdout_evaluation_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holdout_run_id", name="uq_multibagger_holdout_decision_run"),
    )
    op.create_index(
        "ix_multibagger_holdout_decisions_holdout_run_id",
        "multibagger_holdout_decisions",
        ["holdout_run_id"],
    )
    op.create_index(
        "ix_multibagger_holdout_decisions_decision_fingerprint_sha256",
        "multibagger_holdout_decisions",
        ["decision_fingerprint_sha256"],
    )


def downgrade() -> None:
    for table, indexes in (
        (
            "multibagger_holdout_decisions",
            ("decision_fingerprint_sha256", "holdout_run_id"),
        ),
        ("multibagger_holdout_metrics", ("metric_fingerprint_sha256", "holdout_run_id")),
        (
            "multibagger_holdout_evaluation_rows",
            ("row_fingerprint_sha256", "holdout_run_id"),
        ),
        (
            "multibagger_holdout_scores",
            ("score_fingerprint_sha256", "subject_code", "holdout_run_id"),
        ),
        (
            "multibagger_holdout_evaluation_runs",
            (
                "s2_experiment_run_id",
                "champion_scoring_configuration_id",
                "temporal_split_run_id",
                "source_evaluation_run_id",
                "run_key_sha256",
            ),
        ),
    ):
        for column in indexes:
            op.drop_index(_index_name(table, column), table_name=table)
        op.drop_table(table)
