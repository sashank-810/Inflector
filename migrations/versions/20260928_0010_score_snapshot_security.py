"""Add selected security identity for cross-domain score snapshots.

Revision ID: 20260928_0010
Revises: 20260919_0009
"""

import sqlalchemy as sa
from alembic import op

revision = "20260928_0010"
down_revision = "20260919_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("score_snapshots") as batch_op:
        batch_op.add_column(sa.Column("selected_security_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_score_snapshots_selected_security_id_securities",
            "securities",
            ["selected_security_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("score_snapshots") as batch_op:
        batch_op.drop_constraint(
            "fk_score_snapshots_selected_security_id_securities",
            type_="foreignkey",
        )
        batch_op.drop_column("selected_security_id")
