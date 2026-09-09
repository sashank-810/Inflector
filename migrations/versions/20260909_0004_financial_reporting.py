"""Create Phase 2B append-only financial reporting tables.

Revision ID: 20260909_0004
Revises: 20260909_0003
Create Date: 2026-09-09 01:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0004"
down_revision = "20260909_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create fiscal periods, immutable filing headers, dictionary, and facts."""

    op.create_table(
        "fiscal_periods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("period_kind", sa.String(32), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("fiscal_quarter", sa.Integer(), nullable=True),
        sa.Column("is_ytd", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fiscal_periods_company_id", "fiscal_periods", ["company_id"])
    op.create_table(
        "financial_filings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("external_filing_id", sa.String(255), nullable=False),
        sa.Column("filing_type", sa.String(64), nullable=False),
        sa.Column("filing_scope", sa.String(32), nullable=False),
        sa.Column("is_restatement", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["provider_dataset_id"], ["provider_datasets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_dataset_id",
            "external_filing_id",
            "filing_scope",
            "available_at",
            "revision_at",
            name="uq_financial_filing_revision",
        ),
    )
    op.create_index("ix_financial_filings_company_id", "financial_filings", ["company_id"])
    op.create_table(
        "financial_metric_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(96), nullable=False),
        sa.Column("statement_kind", sa.String(32), nullable=False),
        sa.Column("unit_category", sa.String(32), nullable=False),
        sa.Column("semantic_type", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "financial_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("filing_id", sa.Uuid(), nullable=False),
        sa.Column("fiscal_period_id", sa.Uuid(), nullable=False),
        sa.Column("metric_definition_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("reported_value", sa.Numeric(28, 8), nullable=False),
        sa.Column("reported_unit", sa.String(32), nullable=False),
        sa.Column("reported_scale", sa.String(32), nullable=False),
        sa.Column("reported_currency", sa.String(3), nullable=True),
        sa.Column("normalized_value", sa.Numeric(28, 8), nullable=True),
        sa.Column("normalized_unit", sa.String(32), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["filing_id"], ["financial_filings.id"]),
        sa.ForeignKeyConstraint(["fiscal_period_id"], ["fiscal_periods.id"]),
        sa.ForeignKeyConstraint(["metric_definition_id"], ["financial_metric_definitions.id"]),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index("ix_financial_facts_filing_id", "financial_facts", ["filing_id"])
    op.create_index("ix_financial_facts_fiscal_period_id", "financial_facts", ["fiscal_period_id"])
    op.create_index(
        "ix_financial_facts_metric_definition_id", "financial_facts", ["metric_definition_id"]
    )


def downgrade() -> None:
    """Remove Phase 2B financial reporting tables."""

    op.drop_index("ix_financial_facts_metric_definition_id", table_name="financial_facts")
    op.drop_index("ix_financial_facts_fiscal_period_id", table_name="financial_facts")
    op.drop_index("ix_financial_facts_filing_id", table_name="financial_facts")
    op.drop_table("financial_facts")
    op.drop_table("financial_metric_definitions")
    op.drop_index("ix_financial_filings_company_id", table_name="financial_filings")
    op.drop_table("financial_filings")
    op.drop_index("ix_fiscal_periods_company_id", table_name="fiscal_periods")
    op.drop_table("fiscal_periods")
