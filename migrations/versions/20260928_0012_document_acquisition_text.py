"""Add immutable document assets and deterministic text extractions.

Revision ID: 20260928_0012
Revises: 20260928_0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260928_0012"
down_revision = "20260928_0011"
branch_labels = None
depends_on = None


def _json() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "document_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("requested_uri", sa.Text(), nullable=False),
        sa.Column("resolved_uri", sa.Text(), nullable=True),
        sa.Column("declared_media_type", sa.String(length=120), nullable=True),
        sa.Column("detected_media_type", sa.String(length=120), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("warnings_json", _json(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id",
            "content_sha256",
            name="uq_document_asset_content",
        ),
    )
    op.create_index("ix_document_assets_document_id", "document_assets", ["document_id"])

    op.create_table(
        "document_text_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_asset_id", sa.Uuid(), nullable=False),
        sa.Column("extractor_code", sa.String(length=80), nullable=False),
        sa.Column("extractor_semantic_version", sa.String(length=80), nullable=False),
        sa.Column("extractor_runtime_version", sa.String(length=80), nullable=False),
        sa.Column("text_object_key", sa.Text(), nullable=True),
        sa.Column("text_sha256", sa.String(length=64), nullable=True),
        sa.Column("character_count", sa.BigInteger(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("page_map_json", _json(), nullable=False),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("warnings_json", _json(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_asset_id"], ["document_assets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_asset_id",
            "extractor_code",
            "extractor_semantic_version",
            "extractor_runtime_version",
            name="uq_document_text_extraction_identity",
        ),
    )
    op.create_index(
        "ix_document_text_extractions_document_asset_id",
        "document_text_extractions",
        ["document_asset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_text_extractions_document_asset_id",
        table_name="document_text_extractions",
    )
    op.drop_table("document_text_extractions")
    op.drop_index("ix_document_assets_document_id", table_name="document_assets")
    op.drop_table("document_assets")
