"""Persist complete corporate-action identity terms.

Revision ID: 20260909_0006
Revises: 20260909_0005
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_0006"
down_revision = "20260909_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for _name, column in (
        ("exchange", sa.Column("exchange", sa.String(16), nullable=True)),
        ("old_symbol", sa.Column("old_symbol", sa.String(64), nullable=True)),
        ("new_symbol", sa.Column("new_symbol", sa.String(64), nullable=True)),
        ("successor_isin", sa.Column("successor_isin", sa.String(12), nullable=True)),
    ):
        op.add_column("corporate_actions", column)


def downgrade() -> None:
    for name in ("successor_isin", "new_symbol", "old_symbol", "exchange"):
        op.drop_column("corporate_actions", name)
