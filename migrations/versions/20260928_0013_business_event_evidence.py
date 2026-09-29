"""Add deterministic business events and their citable evidence.

Revision ID: 20260928_0013
Revises: 20260928_0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260928_0013"
down_revision = "20260928_0012"
branch_labels = None
depends_on = None


def _json() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "business_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("announcement_id", sa.Uuid(), nullable=False),
        sa.Column("provider_dataset_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("source_event_date", sa.Date(), nullable=True),
        sa.Column("source_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ruleset_code", sa.String(length=80), nullable=False),
        sa.Column("ruleset_semantic_version", sa.String(length=80), nullable=False),
        sa.Column("matched_rule_codes_json", _json(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("warnings_json", _json(), nullable=False),
        sa.Column("detection_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("derived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["announcement_id"], ["announcements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["provider_dataset_id"], ["provider_datasets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "announcement_id",
            "event_type",
            "ruleset_code",
            "ruleset_semantic_version",
            name="uq_business_event_announcement_type_ruleset",
        ),
        sa.UniqueConstraint("detection_fingerprint_sha256"),
    )
    for column in (
        "company_id",
        "security_id",
        "announcement_id",
        "provider_dataset_id",
        "source_available_at",
        "detection_fingerprint_sha256",
    ):
        op.create_index(f"ix_business_events_{column}", "business_events", [column])

    op.create_table(
        "business_event_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_event_id", sa.Uuid(), nullable=False),
        sa.Column("announcement_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_kind", sa.String(length=48), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("document_asset_id", sa.Uuid(), nullable=True),
        sa.Column("text_extraction_id", sa.Uuid(), nullable=True),
        sa.Column("rule_code", sa.String(length=120), nullable=False),
        sa.Column("rule_semantic_version", sa.String(length=80), nullable=False),
        sa.Column("start_offset", sa.BigInteger(), nullable=False),
        sa.Column("end_offset", sa.BigInteger(), nullable=False),
        sa.Column("page_numbers_json", _json(), nullable=False),
        sa.Column("page_text_sha256s_json", _json(), nullable=False),
        sa.Column("excerpt_text", sa.Text(), nullable=False),
        sa.Column("excerpt_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["business_event_id"], ["business_events.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["announcement_id"], ["announcements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_asset_id"], ["document_assets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["text_extraction_id"],
            ["document_text_extractions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_event_id",
            "evidence_fingerprint_sha256",
            name="uq_business_event_evidence_fingerprint",
        ),
    )
    op.create_index(
        "ix_business_event_evidence_business_event_id",
        "business_event_evidence",
        ["business_event_id"],
    )
    op.create_index(
        "ix_business_event_evidence_announcement_id",
        "business_event_evidence",
        ["announcement_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_business_event_evidence_announcement_id",
        table_name="business_event_evidence",
    )
    op.drop_index(
        "ix_business_event_evidence_business_event_id",
        table_name="business_event_evidence",
    )
    op.drop_table("business_event_evidence")
    for column in reversed(
        (
            "company_id",
            "security_id",
            "announcement_id",
            "provider_dataset_id",
            "source_available_at",
            "detection_fingerprint_sha256",
        )
    ):
        op.drop_index(f"ix_business_events_{column}", table_name="business_events")
    op.drop_table("business_events")
