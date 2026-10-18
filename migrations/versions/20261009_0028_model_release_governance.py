"""Add Production U model release governance persistence.

Revision ID: 20261009_0028
Revises: 20261008_0027
"""

import sqlalchemy as sa
from alembic import op

revision = "20261009_0028"
down_revision = "20261008_0027"
branch_labels = None
depends_on = None


_SHORT_INDEX_NAMES = {
    (
        "model_release_production_scores",
        "projection_fingerprint_sha256",
    ): "ix_model_release_prod_score_fingerprint",
}


def upgrade() -> None:
    op.create_table(
        "model_release_candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_candidate_key_sha256", sa.String(64), nullable=False),
        sa.Column("policy_code", sa.String(120), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("evidence_class", sa.String(48), nullable=False),
        sa.Column("champion_scoring_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("champion_scoring_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("temporal_holdout_run_id", sa.Uuid(), nullable=True),
        sa.Column("temporal_holdout_run_key_sha256", sa.String(64), nullable=True),
        sa.Column("temporal_holdout_decision_id", sa.Uuid(), nullable=True),
        sa.Column("temporal_holdout_decision_fingerprint_sha256", sa.String(64), nullable=True),
        sa.Column("s2_experiment_run_id", sa.Uuid(), nullable=True),
        sa.Column("s2_experiment_run_key_sha256", sa.String(64), nullable=True),
        sa.Column("s2_freeze_id", sa.Uuid(), nullable=True),
        sa.Column("s2_freeze_key_sha256", sa.String(64), nullable=True),
        sa.Column("s2_candidate_id", sa.Uuid(), nullable=True),
        sa.Column("s2_candidate_key_sha256", sa.String(64), nullable=True),
        sa.Column("s2_candidate_checksum_sha256", sa.String(64), nullable=True),
        sa.Column("s2_development_evidence_checksum_sha256", sa.String(64), nullable=True),
        sa.Column("scoring_definition_json", sa.JSON(), nullable=False),
        sa.Column("scoring_definition_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "subject_code IN ('champion','challenger')", name="ck_model_release_candidate_subject"
        ),
        sa.CheckConstraint(
            "evidence_class IN ('accepted_v5_champion','production_authoritative')",
            name="ck_model_release_candidate_evidence",
        ),
        sa.CheckConstraint(
            "status IN ('candidate_created','shadow_ready','shadow_running',"
            "'burn_in_complete','activation_approved','active','rolled_back','rejected')",
            name="ck_model_release_candidate_status",
        ),
        sa.ForeignKeyConstraint(
            ["champion_scoring_configuration_id"],
            ["scoring_configurations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["temporal_holdout_run_id"],
            ["multibagger_holdout_evaluation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["temporal_holdout_decision_id"],
            ["multibagger_holdout_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["s2_experiment_run_id"],
            ["multibagger_challenger_experiment_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["s2_freeze_id"], ["multibagger_challenger_freezes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["s2_candidate_id"], ["multibagger_challenger_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_candidate_key_sha256", name="uq_model_release_candidate_key"),
    )
    _indexes("model_release_candidates", "release_candidate_key_sha256", "subject_code")

    op.create_table(
        "model_deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_key_sha256", sa.String(64), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("scoring_definition_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("previous_deployment_id", sa.Uuid(), nullable=True),
        sa.Column("activation_approval_id", sa.Uuid(), nullable=True),
        sa.Column("activation_reason", sa.String(96), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deployment_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "subject_code IN ('champion','challenger')", name="ck_deployment_subject"
        ),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["previous_deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_key_sha256", name="uq_model_deployment_key"),
    )
    _indexes("model_deployments", "deployment_key_sha256", "release_candidate_id")

    op.create_table(
        "active_model_release",
        sa.Column("pointer_name", sa.String(32), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("scoring_definition_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("pointer_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("pointer_name = 'production'", name="ck_active_model_release_singleton"),
        sa.CheckConstraint(
            "subject_code IN ('champion','challenger')",
            name="ck_active_model_release_subject",
        ),
        sa.CheckConstraint("pointer_version >= 1", name="ck_active_model_release_version"),
        sa.ForeignKeyConstraint(["deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("pointer_name"),
        sa.UniqueConstraint("deployment_id"),
    )

    op.create_table(
        "model_shadow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shadow_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("active_champion_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("requirements_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('shadow_ready','shadow_running','burn_in_complete','rejected')",
            name="ck_model_shadow_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["active_champion_deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shadow_run_key_sha256", name="uq_model_shadow_run_key"),
        sa.UniqueConstraint("release_candidate_id", name="uq_model_shadow_run_candidate"),
    )
    _indexes("model_shadow_runs", "shadow_run_key_sha256", "release_candidate_id")

    op.create_table(
        "model_shadow_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shadow_run_id", sa.Uuid(), nullable=False),
        sa.Column("shadow_event_key_sha256", sa.String(64), nullable=False),
        sa.Column("cycle_key", sa.String(128), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_score_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("component_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("champion_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("challenger_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("champion_rankable", sa.Boolean(), nullable=False),
        sa.Column("challenger_rankable", sa.Boolean(), nullable=False),
        sa.Column("rankability_delta", sa.Integer(), nullable=False),
        sa.Column("coverage_delta", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Numeric(50, 28), nullable=False),
        sa.Column("integrity_violation", sa.Boolean(), nullable=False),
        sa.Column("score_definition_drift", sa.Boolean(), nullable=False),
        sa.Column("missing_required_components", sa.Boolean(), nullable=False),
        sa.Column("scheduler_failure", sa.Boolean(), nullable=False),
        sa.Column("production_health_regression", sa.Boolean(), nullable=False),
        sa.Column("production_notification_emitted", sa.Boolean(), nullable=False),
        sa.Column("event_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("rankability_delta = 0", name="ck_model_shadow_rankability_delta"),
        sa.CheckConstraint("coverage_delta = 0", name="ck_model_shadow_coverage_delta"),
        sa.CheckConstraint(
            "production_notification_emitted = false", name="ck_model_shadow_no_notification"
        ),
        sa.ForeignKeyConstraint(["shadow_run_id"], ["model_shadow_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shadow_event_key_sha256", name="uq_model_shadow_event_key"),
    )
    _indexes("model_shadow_events", "shadow_run_id", "shadow_event_key_sha256")

    op.create_table(
        "model_burn_in_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shadow_run_id", sa.Uuid(), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("burn_in_key_sha256", sa.String(64), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("future_outcomes_read", sa.Boolean(), nullable=False),
        sa.Column("model_tuning_performed", sa.Boolean(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('passed','failed')", name="ck_model_burn_in_status"),
        sa.CheckConstraint(
            "future_outcomes_read = false", name="ck_model_burn_in_no_future_outcomes"
        ),
        sa.CheckConstraint("model_tuning_performed = false", name="ck_model_burn_in_no_tuning"),
        sa.ForeignKeyConstraint(["shadow_run_id"], ["model_shadow_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shadow_run_id", name="uq_model_burn_in_shadow"),
        sa.UniqueConstraint("burn_in_key_sha256", name="uq_model_burn_in_key"),
    )

    op.create_table(
        "model_activation_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_key_sha256", sa.String(64), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("burn_in_run_id", sa.Uuid(), nullable=False),
        sa.Column("current_champion_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("challenger_definition_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("approver_provenance", sa.String(255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["burn_in_run_id"], ["model_burn_in_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["current_champion_deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_key_sha256", name="uq_model_activation_approval_key"),
        sa.UniqueConstraint("release_candidate_id", name="uq_model_activation_approval_candidate"),
    )

    op.create_table(
        "model_rollbacks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rollback_key_sha256", sa.String(64), nullable=False),
        sa.Column("rolled_back_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("restored_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("rollback_deployment_id", sa.Uuid(), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=False),
        sa.Column("health_evidence_json", sa.JSON(), nullable=False),
        sa.Column("policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rollback_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["rolled_back_deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["restored_deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rollback_deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rollback_key_sha256", name="uq_model_rollback_key"),
    )

    op.create_table(
        "model_release_cycle_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operational_run_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("scoring_definition_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("binding_key_sha256", sa.String(64), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["operational_run_id"], ["operational_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operational_run_id", name="uq_model_release_cycle_operational_run"),
        sa.UniqueConstraint("binding_key_sha256", name="uq_model_release_cycle_binding_key"),
    )

    op.create_table(
        "model_release_production_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operational_run_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_release_binding_id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("release_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("subject_code", sa.String(32), nullable=False),
        sa.Column("source_score_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("source_snapshot_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("component_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("scoring_definition_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("production_final_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("rankable", sa.Boolean(), nullable=False),
        sa.Column("unranked_reason", sa.String(64), nullable=True),
        sa.Column("rankability_delta", sa.Integer(), nullable=False),
        sa.Column("coverage_delta", sa.Integer(), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projection_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "subject_code IN ('champion','challenger')",
            name="ck_model_release_production_score_subject",
        ),
        sa.CheckConstraint(
            "(rankable = true AND unranked_reason IS NULL AND production_final_score IS NOT NULL) "
            "OR (rankable = false AND unranked_reason IS NOT NULL "
            "AND production_final_score IS NULL)",
            name="ck_model_release_production_score_rankability",
        ),
        sa.CheckConstraint(
            "rankability_delta = 0", name="ck_model_release_production_score_rankability_delta"
        ),
        sa.CheckConstraint(
            "coverage_delta = 0", name="ck_model_release_production_score_coverage_delta"
        ),
        sa.ForeignKeyConstraint(
            ["operational_run_id"], ["operational_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cycle_release_binding_id"],
            ["model_release_cycle_bindings.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["deployment_id"], ["model_deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["model_release_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operational_run_id",
            "source_score_snapshot_id",
            name="uq_model_release_production_score_source",
        ),
        sa.UniqueConstraint(
            "projection_fingerprint_sha256",
            name="uq_model_release_production_score_fingerprint",
        ),
    )
    _indexes(
        "model_release_production_scores",
        "operational_run_id",
        "source_score_snapshot_id",
        "projection_fingerprint_sha256",
    )
    with op.batch_alter_table("opportunity_discovery_items") as batch:
        batch.add_column(sa.Column("model_release_production_score_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_opportunity_discovery_item_release_score",
            "model_release_production_scores",
            ["model_release_production_score_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index(
            "ix_opp_discovery_item_release_score_id",
            ["model_release_production_score_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("opportunity_discovery_items") as batch:
        batch.drop_index("ix_opp_discovery_item_release_score_id")
        batch.drop_constraint("fk_opportunity_discovery_item_release_score", type_="foreignkey")
        batch.drop_column("model_release_production_score_id")
    for table in (
        "model_release_production_scores",
        "model_release_cycle_bindings",
        "model_rollbacks",
        "model_activation_approvals",
        "model_burn_in_runs",
        "model_shadow_events",
        "model_shadow_runs",
        "active_model_release",
        "model_deployments",
        "model_release_candidates",
    ):
        op.drop_table(table)


def _indexes(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(
            _SHORT_INDEX_NAMES.get((table, column), f"ix_{table}_{column}"),
            table,
            [column],
        )
