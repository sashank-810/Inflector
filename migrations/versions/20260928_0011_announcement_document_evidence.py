"""Add append-only announcement and document evidence.

Revision ID: 20260928_0011
Revises: 20260928_0010
"""

import sqlalchemy as sa
from alembic import op

revision = "20260928_0011"
down_revision = "20260928_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("provider_category", sa.String(length=120), nullable=True),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("announcement_date", sa.Date(), nullable=True),
        sa.Column("exchange", sa.String(length=32), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index("ix_announcements_company_id", "announcements", ["company_id"])
    op.create_index("ix_announcements_security_id", "announcements", ["security_id"])
    op.create_index(
        "ix_announcements_provider_dataset_id", "announcements", ["provider_dataset_id"]
    )
    op.create_index("ix_announcements_announcement_date", "announcements", ["announcement_date"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("media_type", sa.String(length=120), nullable=True),
        sa.Column("document_uri", sa.Text(), nullable=False),
        sa.Column("document_content_sha256", sa.String(length=64), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id"),
    )
    op.create_index("ix_documents_company_id", "documents", ["company_id"])
    op.create_index("ix_documents_security_id", "documents", ["security_id"])
    op.create_index("ix_documents_provider_dataset_id", "documents", ["provider_dataset_id"])

    op.create_table(
        "announcement_documents",
        sa.Column("announcement_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["announcement_id"], ["announcements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("announcement_id", "document_id"),
    )


def downgrade() -> None:
    op.drop_table("announcement_documents")
    op.drop_index("ix_documents_provider_dataset_id", table_name="documents")
    op.drop_index("ix_documents_security_id", table_name="documents")
    op.drop_index("ix_documents_company_id", table_name="documents")
    op.drop_table("documents")
    op.drop_index("ix_announcements_announcement_date", table_name="announcements")
    op.drop_index("ix_announcements_provider_dataset_id", table_name="announcements")
    op.drop_index("ix_announcements_security_id", table_name="announcements")
    op.drop_index("ix_announcements_company_id", table_name="announcements")
    op.drop_table("announcements")
