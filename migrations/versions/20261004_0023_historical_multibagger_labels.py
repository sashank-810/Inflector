"""Add historical-universe cohorts and immutable multibagger labels.

Revision ID: 20261004_0023
Revises: 20261003_0022
"""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0023"
down_revision = "20261003_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_universe_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("universe_policy_code", sa.String(120), nullable=False),
        sa.Column("universe_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('planned','completed','failed')",
            name="ck_historical_universe_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_historical_universe_run_key"),
    )
    op.create_index(
        "ix_historical_universe_runs_run_key_sha256",
        "historical_universe_runs",
        ["run_key_sha256"],
    )
    op.create_index(
        "ix_historical_universe_runs_source_provider_dataset_id",
        "historical_universe_runs",
        ["source_provider_dataset_id"],
    )

    op.create_table(
        "historical_universe_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("historical_universe_run_id", sa.Uuid(), nullable=False),
        sa.Column("historical_symbol", sa.String(64), nullable=False),
        sa.Column("historical_isin", sa.String(12), nullable=True),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("series", sa.String(16), nullable=False),
        sa.Column("membership_date", sa.Date(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("exchange_listing_id", sa.Uuid(), nullable=True),
        sa.Column("membership_status", sa.String(48), nullable=False),
        sa.Column("reason_code", sa.String(96), nullable=True),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("member_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "membership_status IN "
            "('eligible','unresolved_identity','ambiguous_identity',"
            "'unsupported_security_type','excluded_exchange','excluded_series')",
            name="ck_historical_universe_member_status",
        ),
        sa.CheckConstraint(
            "membership_status != 'eligible' OR "
            "(company_id IS NOT NULL AND security_id IS NOT NULL "
            "AND exchange_listing_id IS NOT NULL AND reason_code IS NULL)",
            name="ck_historical_universe_member_eligible_identity",
        ),
        sa.ForeignKeyConstraint(
            ["historical_universe_run_id"],
            ["historical_universe_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["exchange_listing_id"], ["exchange_listings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "historical_universe_run_id",
            "member_fingerprint_sha256",
            name="uq_historical_universe_member_fingerprint",
        ),
    )
    for column in (
        "historical_universe_run_id",
        "membership_date",
        "company_id",
        "security_id",
        "source_record_id",
    ):
        op.create_index(
            f"ix_historical_universe_members_{column}",
            "historical_universe_members",
            [column],
        )

    op.create_table(
        "multibagger_label_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_key_sha256", sa.String(64), nullable=False),
        sa.Column("label_policy_code", sa.String(120), nullable=False),
        sa.Column("label_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("source_backtest_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_backtest_run_key_sha256", sa.String(64), nullable=False),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("corporate_action_provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_state_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("ordered_contracts_json", sa.JSON(), nullable=False),
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
            name="ck_multibagger_label_run_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_backtest_run_id"], ["backtest_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["market_provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["corporate_action_provider_dataset_id"],
            ["provider_datasets.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key_sha256", name="uq_multibagger_label_run_key"),
    )
    op.create_index(
        "ix_multibagger_label_runs_run_key_sha256",
        "multibagger_label_runs",
        ["run_key_sha256"],
    )
    op.create_index(
        "ix_multibagger_label_runs_source_backtest_run_id",
        "multibagger_label_runs",
        ["source_backtest_run_id"],
    )

    op.create_table(
        "multibagger_outcome_labels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("multibagger_label_run_id", sa.Uuid(), nullable=False),
        sa.Column("backtest_observation_id", sa.Uuid(), nullable=False),
        sa.Column("contract_code", sa.String(32), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("unavailable_reason", sa.String(96), nullable=True),
        sa.Column("threshold_multiple", sa.Numeric(50, 28), nullable=False),
        sa.Column("horizon_calendar_years", sa.Integer(), nullable=False),
        sa.Column("entry_trading_date", sa.Date(), nullable=True),
        sa.Column("adjusted_entry_close", sa.Numeric(50, 28), nullable=True),
        sa.Column("horizon_end_date", sa.Date(), nullable=True),
        sa.Column("first_threshold_hit_trading_date", sa.Date(), nullable=True),
        sa.Column("trading_observations_available", sa.Integer(), nullable=False),
        sa.Column("peak_adjusted_close", sa.Numeric(50, 28), nullable=True),
        sa.Column("peak_price_multiple", sa.Numeric(50, 28), nullable=True),
        sa.Column("maximum_forward_price_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("endpoint_adjusted_close", sa.Numeric(50, 28), nullable=True),
        sa.Column("endpoint_return", sa.Numeric(50, 28), nullable=True),
        sa.Column("calendar_days_to_threshold", sa.Integer(), nullable=True),
        sa.Column("trading_observations_to_threshold", sa.Integer(), nullable=True),
        sa.Column("maximum_drawdown", sa.Numeric(50, 28), nullable=True),
        sa.Column("listing_valid_to", sa.Date(), nullable=True),
        sa.Column("label_matured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("label_fingerprint_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "classification IN ('positive','negative','unmatured','unavailable')",
            name="ck_multibagger_outcome_label_classification",
        ),
        sa.CheckConstraint(
            "(classification IN ('positive','negative') "
            "AND label_matured_at IS NOT NULL AND unavailable_reason IS NULL) OR "
            "(classification = 'unmatured' AND label_matured_at IS NULL "
            "AND unavailable_reason IS NULL) OR "
            "(classification = 'unavailable' AND unavailable_reason IS NOT NULL)",
            name="ck_multibagger_outcome_label_state_fields",
        ),
        sa.CheckConstraint(
            "threshold_multiple > 1 AND horizon_calendar_years > 0 "
            "AND trading_observations_available >= 0",
            name="ck_multibagger_outcome_label_measurement_bounds",
        ),
        sa.ForeignKeyConstraint(
            ["multibagger_label_run_id"], ["multibagger_label_runs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["backtest_observation_id"], ["backtest_observations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "multibagger_label_run_id",
            "backtest_observation_id",
            "contract_code",
            name="uq_multibagger_outcome_label_contract",
        ),
        sa.UniqueConstraint(
            "label_fingerprint_sha256", name="uq_multibagger_outcome_label_fingerprint"
        ),
    )
    for column in (
        "multibagger_label_run_id",
        "backtest_observation_id",
        "classification",
    ):
        op.create_index(
            f"ix_multibagger_outcome_labels_{column}",
            "multibagger_outcome_labels",
            [column],
        )


def downgrade() -> None:
    for column in (
        "classification",
        "backtest_observation_id",
        "multibagger_label_run_id",
    ):
        op.drop_index(
            f"ix_multibagger_outcome_labels_{column}",
            table_name="multibagger_outcome_labels",
        )
    op.drop_table("multibagger_outcome_labels")
    op.drop_index(
        "ix_multibagger_label_runs_source_backtest_run_id",
        table_name="multibagger_label_runs",
    )
    op.drop_index("ix_multibagger_label_runs_run_key_sha256", table_name="multibagger_label_runs")
    op.drop_table("multibagger_label_runs")
    for column in (
        "source_record_id",
        "security_id",
        "company_id",
        "membership_date",
        "historical_universe_run_id",
    ):
        op.drop_index(
            f"ix_historical_universe_members_{column}",
            table_name="historical_universe_members",
        )
    op.drop_table("historical_universe_members")
    op.drop_index(
        "ix_historical_universe_runs_source_provider_dataset_id",
        table_name="historical_universe_runs",
    )
    op.drop_index(
        "ix_historical_universe_runs_run_key_sha256",
        table_name="historical_universe_runs",
    )
    op.drop_table("historical_universe_runs")
