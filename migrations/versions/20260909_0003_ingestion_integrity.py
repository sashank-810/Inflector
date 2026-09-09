"""Persist record locators for raw archived provider payloads.

Revision ID: 20260909_0003
Revises: 20260909_0002
Create Date: 2026-09-09 00:30:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0003"
down_revision = "20260909_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the record-in-object locator without fabricating legacy values."""

    op.add_column("source_records", sa.Column("raw_payload_reference", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove the Phase 2A.1 record locator."""

    op.drop_column("source_records", "raw_payload_reference")
