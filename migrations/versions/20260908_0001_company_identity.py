"""Create canonical company identity tables.

Revision ID: 20260908_0001
Revises:
Create Date: 2026-09-08 00:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260908_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the Phase 1 company, security, and listing tables."""

    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("sector", sa.String(length=120), nullable=False),
        sa.Column("industry", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("legal_name"),
    )
    op.create_table(
        "securities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("security_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("isin"),
    )
    op.create_index("ix_securities_company_id", "securities", ["company_id"])
    op.create_table(
        "exchange_listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("security_id", sa.Uuid(), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "security_id", "exchange", "symbol", "valid_from", name="uq_listing_identity_period"
        ),
    )
    op.create_index("ix_exchange_listings_security_id", "exchange_listings", ["security_id"])


def downgrade() -> None:
    """Remove Phase 1 identity tables in dependency order."""

    op.drop_index("ix_exchange_listings_security_id", table_name="exchange_listings")
    op.drop_table("exchange_listings")
    op.drop_index("ix_securities_company_id", table_name="securities")
    op.drop_table("securities")
    op.drop_table("companies")
