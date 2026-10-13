"""Add historical evaluation activation, temporal split, and factual error analysis.

Revision ID: 20261006_0025
Revises: 20261005_0024
"""

import sqlalchemy as sa
from alembic import op

revision = "20261006_0025"
down_revision = "20261005_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_mii_artifact_acquisitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("acquisition_key_sha256", sa.String(64), nullable=False),
        sa.Column("requested_source_date", sa.Date(), nullable=False),
        sa.Column("acquisition_provenance", sa.String(48), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("raw_object_key", sa.Text(), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_filename", sa.String(160), nullable=False),
        sa.Column("parser_version", sa.String(96), nullable=False),
        sa.Column("provenance_status", sa.String(48), nullable=False),
        sa.Column("status", sa.String(48), nullable=False),
        sa.Column("linked_ingestion_run_id", sa.Uuid(), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "acquisition_provenance IN "
            "('verified_official_https','operator_supplied_unverified','fixture_only')",
            name="ck_historical_mii_acquisition_provenance",
        ),
        sa.CheckConstraint(
            "status IN ('archived','accepted','reused_semantic_snapshot',"
            "'revision_conflict','unverified','failed')",
            name="ck_historical_mii_acquisition_status",
        ),
        sa.CheckConstraint(
            "acquisition_provenance = 'verified_official_https' OR "
            "linked_ingestion_run_id IS NULL",
            name="ck_historical_mii_unverified_has_no_ingestion",
        ),
        sa.ForeignKeyConstraint(
            ["linked_ingestion_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("acquisition_key_sha256", name="uq_historical_mii_acquisition_key"),
    )
    for column in (
        "acquisition_key_sha256",
        "requested_source_date",
        "acquisition_provenance",
        "raw_content_sha256",
        "linked_ingestion_run_id",
    ):
        op.create_index(
            f"ix_historical_mii_artifact_acquisitions_{column}",
            "historical_mii_artifact_acquisitions",
            [column],
        )
    op.create_table(
        "historical_session_artifact_acquisitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("acquisition_key_sha256", sa.String(64), nullable=False),
        sa.Column("acquisition_provenance", sa.String(48), nullable=False),
        sa.Column("source_provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_ingestion_run_id", sa.Uuid(), nullable=True),
        sa.Column("source_record_id", sa.Uuid(), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("raw_object_key", sa.Text(), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parser_version", sa.String(96), nullable=False),
        sa.Column("provenance_status", sa.String(48), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "acquisition_provenance IN "
            "('verified_official_https','operator_supplied_unverified','fixture_only')",
            name="ck_historical_session_acquisition_provenance",
        ),
        sa.CheckConstraint(
            "status IN ('archived','parsed','unsupported','failed')",
            name="ck_historical_session_acquisition_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_ingestion_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"], ["source_records.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "acquisition_key_sha256", name="uq_historical_session_acquisition_key"
        ),
    )
    for column in (
        "acquisition_key_sha256",
        "acquisition_provenance",
        "source_provider_dataset_id",
        "source_ingestion_run_id",
        "source_record_id",
        "raw_content_sha256",
    ):
        op.create_index(
            f"ix_historical_session_artifact_acquisitions_{column}",
            "historical_session_artifact_acquisitions",
            [column],
        )
    op.create_table(
        "historical_session_calendar_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("activation_policy_code", sa.String(120), nullable=False),
        sa.Column("activation_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("source_provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("source_artifact_acquisition_id", sa.Uuid(), nullable=False),
        sa.Column("acquisition_provenance", sa.String(48), nullable=False),
        sa.Column("parser_version", sa.String(96), nullable=False),
        sa.Column("benchmark_provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("benchmark_series_id", sa.Uuid(), nullable=False),
        sa.Column("raw_object_key", sa.Text(), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_from_date", sa.Date(), nullable=False),
        sa.Column("requested_to_date", sa.Date(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("observed_count", sa.Integer(), nullable=False),
        sa.Column("missing_dates_json", sa.JSON(), nullable=False),
        sa.Column("unexpected_dates_json", sa.JSON(), nullable=False),
        sa.Column("first_expected_session", sa.Date(), nullable=True),
        sa.Column("last_expected_session", sa.Date(), nullable=True),
        sa.Column("first_observed_session", sa.Date(), nullable=True),
        sa.Column("last_observed_session", sa.Date(), nullable=True),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verification_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('verified','incomplete','unsupported','failed')",
            name="ck_historical_session_calendar_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_ingestion_run_id"], ["ingestion_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"], ["source_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_acquisition_id"],
            ["historical_session_artifact_acquisitions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_series_id"], ["benchmark_series.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_historical_session_calendar_run_key"),
    )
    for column in (
        "run_key_sha256",
        "source_provider_dataset_id",
        "source_ingestion_run_id",
        "source_record_id",
        "source_artifact_acquisition_id",
    ):
        op.create_index(
            f"ix_historical_session_calendar_runs_{column}",
            "historical_session_calendar_runs",
            [column],
        )
    op.create_table(
        "historical_session_calendar_dates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("historical_session_calendar_run_id", sa.Uuid(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("benchmark_observed", sa.Boolean(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("date_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["historical_session_calendar_run_id"],
            ["historical_session_calendar_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id"], ["source_records.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "historical_session_calendar_run_id",
            "session_date",
            name="uq_historical_session_calendar_date",
        ),
    )
    op.create_index(
        "ix_historical_session_calendar_dates_historical_session_calendar_run_id",
        "historical_session_calendar_dates",
        ["historical_session_calendar_run_id"],
    )
    op.create_index(
        "ix_historical_session_calendar_dates_session_date",
        "historical_session_calendar_dates",
        ["session_date"],
    )
    op.create_table(
        "multibagger_temporal_split_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("split_policy_code", sa.String(120), nullable=False),
        sa.Column("split_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("source_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_evaluation_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("session_calendar_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordered_eligible_cutoffs_json", sa.JSON(), nullable=False),
        sa.Column("development_cutoffs_json", sa.JSON(), nullable=False),
        sa.Column("validation_cutoffs_json", sa.JSON(), nullable=False),
        sa.Column("holdout_cutoffs_json", sa.JSON(), nullable=False),
        sa.Column("split_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('completed','data_blocked','failed')",
            name="ck_multibagger_temporal_split_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_run_id"],
            ["multibagger_evaluation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_calendar_run_id"],
            ["historical_session_calendar_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_multibagger_temporal_split_run_key"),
    )
    for column in (
        "run_key_sha256",
        "source_evaluation_run_id",
        "session_calendar_run_id",
        "split_checksum_sha256",
    ):
        op.create_index(
            f"ix_multibagger_temporal_split_runs_{column}",
            "multibagger_temporal_split_runs",
            [column],
        )
    op.create_table(
        "multibagger_temporal_split_cutoffs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_temporal_split_run_id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_evaluation_cohort_id", sa.Uuid(), nullable=False),
        sa.Column("historical_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("split_code", sa.String(32), nullable=False),
        sa.Column("assignment_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "split_code IN ('development','validation','holdout')",
            name="ck_multibagger_temporal_split_code",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_temporal_split_run_id"],
            ["multibagger_temporal_split_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_evaluation_cohort_id"],
            ["multibagger_evaluation_cohorts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "multibagger_temporal_split_run_id",
            "multibagger_evaluation_cohort_id",
            name="uq_multibagger_temporal_split_cohort",
        ),
    )
    for column in (
        "multibagger_temporal_split_run_id",
        "multibagger_evaluation_cohort_id",
        "split_code",
    ):
        op.create_index(
            f"ix_multibagger_temporal_split_cutoffs_{column}",
            "multibagger_temporal_split_cutoffs",
            [column],
        )
    op.create_table(
        "multibagger_error_analysis_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("error_policy_code", sa.String(120), nullable=False),
        sa.Column("error_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("temporal_split_run_id", sa.Uuid(), nullable=False),
        sa.Column("temporal_split_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("source_evaluation_run_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("universe_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("outcome_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("model_family", sa.String(120), nullable=False),
        sa.Column("scoring_configuration_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("holdout_outcomes_accessed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('completed','data_blocked','failed')",
            name="ck_multibagger_error_analysis_run_status",
        ),
        sa.CheckConstraint(
            "holdout_outcomes_accessed = false",
            name="ck_multibagger_error_analysis_no_holdout_access",
        ),
        sa.ForeignKeyConstraint(
            ["temporal_split_run_id"],
            ["multibagger_temporal_split_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_run_id"],
            ["multibagger_evaluation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_multibagger_error_analysis_run_key"),
    )
    for column in ("run_key_sha256", "temporal_split_run_id", "source_evaluation_run_id"):
        op.create_index(
            f"ix_multibagger_error_analysis_runs_{column}",
            "multibagger_error_analysis_runs",
            [column],
        )
    op.create_table(
        "multibagger_error_analysis_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_error_analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("temporal_split_cutoff_id", sa.Uuid(), nullable=False),
        sa.Column("source_confusion_row_id", sa.Uuid(), nullable=False),
        sa.Column("prediction_row_id", sa.Uuid(), nullable=False),
        sa.Column("split_code", sa.String(32), nullable=False),
        sa.Column("historical_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("historical_symbol", sa.String(64), nullable=False),
        sa.Column("outcome_contract_code", sa.String(32), nullable=False),
        sa.Column("selection_contract_code", sa.String(32), nullable=False),
        sa.Column("evaluation_view", sa.String(32), nullable=False),
        sa.Column("confusion_class", sa.String(8), nullable=False),
        sa.Column("rankable", sa.Boolean(), nullable=False),
        sa.Column("unranked_reason", sa.String(96), nullable=True),
        sa.Column("snapshot_status", sa.String(64), nullable=True),
        sa.Column("final_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("dense_score_rank", sa.Integer(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=True),
        sa.Column("selection_boundary_count", sa.Integer(), nullable=False),
        sa.Column("selection_boundary_score", sa.Numeric(50, 28), nullable=True),
        sa.Column("rank_positions_below_boundary", sa.Integer(), nullable=True),
        sa.Column("score_gap_to_boundary", sa.Numeric(50, 28), nullable=True),
        sa.Column("threshold_hit_trading_date", sa.Date(), nullable=True),
        sa.Column("calendar_days_to_threshold", sa.Integer(), nullable=True),
        sa.Column("trading_observations_to_threshold", sa.Integer(), nullable=True),
        sa.Column("endpoint_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("peak_price_multiple", sa.Numeric(50, 28), nullable=True),
        sa.Column("maximum_drawdown", sa.Numeric(50, 28), nullable=True),
        sa.Column("available_component_codes_json", sa.JSON(), nullable=False),
        sa.Column("component_values_json", sa.JSON(), nullable=False),
        sa.Column("score_bucket", sa.String(32), nullable=False),
        sa.Column("error_signature", sa.String(64), nullable=False),
        sa.Column("factual_provenance_json", sa.JSON(), nullable=False),
        sa.Column("row_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "split_code IN ('development','validation')",
            name="ck_multibagger_error_analysis_split",
        ),
        sa.CheckConstraint(
            "confusion_class IN ('fp','fn')",
            name="ck_multibagger_error_analysis_confusion",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_error_analysis_run_id"],
            ["multibagger_error_analysis_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["temporal_split_cutoff_id"],
            ["multibagger_temporal_split_cutoffs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_confusion_row_id"],
            ["multibagger_confusion_rows.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prediction_row_id"], ["multibagger_prediction_rows.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "multibagger_error_analysis_run_id",
            "source_confusion_row_id",
            name="uq_multibagger_error_analysis_source_confusion",
        ),
    )
    for column in (
        "multibagger_error_analysis_run_id",
        "temporal_split_cutoff_id",
        "source_confusion_row_id",
        "prediction_row_id",
        "split_code",
        "row_fingerprint_sha256",
    ):
        op.create_index(
            f"ix_multibagger_error_analysis_rows_{column}",
            "multibagger_error_analysis_rows",
            [column],
        )


def downgrade() -> None:
    for table, columns in (
        (
            "multibagger_error_analysis_rows",
            (
                "row_fingerprint_sha256",
                "split_code",
                "prediction_row_id",
                "source_confusion_row_id",
                "temporal_split_cutoff_id",
                "multibagger_error_analysis_run_id",
            ),
        ),
        (
            "multibagger_error_analysis_runs",
            ("source_evaluation_run_id", "temporal_split_run_id", "run_key_sha256"),
        ),
        (
            "multibagger_temporal_split_cutoffs",
            ("split_code", "multibagger_evaluation_cohort_id", "multibagger_temporal_split_run_id"),
        ),
        (
            "multibagger_temporal_split_runs",
            (
                "split_checksum_sha256",
                "session_calendar_run_id",
                "source_evaluation_run_id",
                "run_key_sha256",
            ),
        ),
        (
            "historical_session_calendar_dates",
            ("session_date", "historical_session_calendar_run_id"),
        ),
        (
            "historical_session_calendar_runs",
            (
                "source_artifact_acquisition_id",
                "source_record_id",
                "source_ingestion_run_id",
                "source_provider_dataset_id",
                "run_key_sha256",
            ),
        ),
    ):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
    for table, columns in (
        (
            "historical_session_artifact_acquisitions",
            (
                "raw_content_sha256",
                "source_record_id",
                "source_ingestion_run_id",
                "source_provider_dataset_id",
                "acquisition_provenance",
                "acquisition_key_sha256",
            ),
        ),
        (
            "historical_mii_artifact_acquisitions",
            (
                "linked_ingestion_run_id",
                "raw_content_sha256",
                "acquisition_provenance",
                "requested_source_date",
                "acquisition_key_sha256",
            ),
        ),
    ):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
