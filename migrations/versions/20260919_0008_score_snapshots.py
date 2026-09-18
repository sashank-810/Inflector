"""Add immutable partial score snapshots and structured component audit.

Revision ID: 20260919_0008
Revises: 20260918_0007
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260919_0008"
down_revision = "20260918_0007"
branch_labels = None
depends_on = None


def _json() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _numeric():
    return sa.Numeric(50, 28).with_variant(sa.String(length=80), "sqlite")


def upgrade() -> None:
    op.create_table(
        "score_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("scoring_configuration_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("knowledge_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ending_fiscal_year", sa.Integer(), nullable=False),
        sa.Column("ending_fiscal_quarter", sa.Integer(), nullable=False),
        sa.Column("selected_provider_dataset_id", sa.Uuid(), nullable=True),
        sa.Column("selected_filing_scope", sa.String(length=32), nullable=True),
        sa.Column("snapshot_status", sa.String(length=64), nullable=False),
        sa.Column("eligibility_eligible", sa.Boolean(), nullable=False),
        sa.Column("eligibility_inputs_json", _json(), nullable=False),
        sa.Column("eligibility_reasons_json", _json(), nullable=False),
        sa.Column("eligibility_warnings_json", _json(), nullable=False),
        sa.Column("financial_core_coverage", _numeric(), nullable=False),
        sa.Column("confidence", _numeric(), nullable=False),
        sa.Column("confidence_inputs_json", _json(), nullable=False),
        sa.Column("confidence_details_json", _json(), nullable=False),
        sa.Column("top_level_component_weight_coverage", _numeric(), nullable=False),
        sa.Column("available_component_codes_json", _json(), nullable=False),
        sa.Column("missing_component_codes_json", _json(), nullable=False),
        sa.Column("context_resolution_json", _json(), nullable=False),
        sa.Column("input_manifest_json", _json(), nullable=False),
        sa.Column("fingerprint_payload_json", _json(), nullable=False),
        sa.Column("snapshot_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("final_score", _numeric(), nullable=True),
        sa.Column("algorithm_version", sa.String(length=96), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["scoring_configuration_id"],
            ["scoring_configurations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["selected_provider_dataset_id"],
            ["provider_datasets.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_fingerprint_sha256", name="uq_score_snapshot_fingerprint"
        ),
    )
    op.create_index("ix_score_snapshots_company_id", "score_snapshots", ["company_id"])
    op.create_index(
        "ix_score_snapshots_model_version_id", "score_snapshots", ["model_version_id"]
    )
    op.create_index(
        "ix_score_snapshots_scoring_configuration_id",
        "score_snapshots",
        ["scoring_configuration_id"],
    )
    op.create_index(
        "ix_score_snapshots_knowledge_cutoff", "score_snapshots", ["knowledge_cutoff"]
    )
    op.create_index(
        "ix_score_snapshots_snapshot_fingerprint_sha256",
        "score_snapshots",
        ["snapshot_fingerprint_sha256"],
    )

    op.create_table(
        "score_components",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("score_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("component_code", sa.String(length=96), nullable=False),
        sa.Column("score", _numeric(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("configured_top_level_weight", _numeric(), nullable=False),
        sa.Column("subfactor_weight_coverage", _numeric(), nullable=False),
        sa.Column("final_contribution", _numeric(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("algorithm_version", sa.String(length=96), nullable=False),
        sa.Column("missing_subfactors_json", _json(), nullable=False),
        sa.Column("warnings_json", _json(), nullable=False),
        sa.Column("detail_json", _json(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "score_snapshot_id", "component_code", name="uq_score_component_code"
        ),
    )
    op.create_index(
        "ix_score_components_score_snapshot_id", "score_components", ["score_snapshot_id"]
    )

    op.create_table(
        "score_explanations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("score_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("score_component_id", sa.Uuid(), nullable=False),
        sa.Column("factor_code", sa.String(length=96), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("raw_value", _numeric(), nullable=False),
        sa.Column("raw_unit", sa.String(length=32), nullable=False),
        sa.Column("normalized_score", _numeric(), nullable=False),
        sa.Column("configured_weight", _numeric(), nullable=False),
        sa.Column("effective_weight", _numeric(), nullable=False),
        sa.Column("component_contribution", _numeric(), nullable=False),
        sa.Column("input_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_type", sa.String(length=120), nullable=False),
        sa.Column("template_code", sa.String(length=120), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=True),
        sa.Column("evidence_manifest_json", _json(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["score_snapshot_id"], ["score_snapshots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["score_component_id"], ["score_components.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "score_component_id", "factor_code", name="uq_score_explanation_factor"
        ),
    )
    op.create_index(
        "ix_score_explanations_score_snapshot_id",
        "score_explanations",
        ["score_snapshot_id"],
    )
    op.create_index(
        "ix_score_explanations_score_component_id",
        "score_explanations",
        ["score_component_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_score_explanations_score_component_id", table_name="score_explanations"
    )
    op.drop_index(
        "ix_score_explanations_score_snapshot_id", table_name="score_explanations"
    )
    op.drop_table("score_explanations")
    op.drop_index("ix_score_components_score_snapshot_id", table_name="score_components")
    op.drop_table("score_components")
    op.drop_index(
        "ix_score_snapshots_snapshot_fingerprint_sha256", table_name="score_snapshots"
    )
    op.drop_index("ix_score_snapshots_knowledge_cutoff", table_name="score_snapshots")
    op.drop_index(
        "ix_score_snapshots_scoring_configuration_id", table_name="score_snapshots"
    )
    op.drop_index("ix_score_snapshots_model_version_id", table_name="score_snapshots")
    op.drop_index("ix_score_snapshots_company_id", table_name="score_snapshots")
    op.drop_table("score_snapshots")
