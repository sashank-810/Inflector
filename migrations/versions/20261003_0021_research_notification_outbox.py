"""Add immutable transport-neutral research notification outbox.

Revision ID: 20261003_0021
Revises: 20261003_0020
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0021"
down_revision = "20261003_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("notification_key_sha256", sa.String(64), nullable=False),
        sa.Column("alert_policy_code", sa.String(120), nullable=False),
        sa.Column("alert_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("payload_schema_version", sa.String(96), nullable=False),
        sa.Column("source_change_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_change_item_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("security_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_symbol", sa.String(64), nullable=True),
        sa.Column("current_symbol", sa.String(64), nullable=True),
        sa.Column("matched_trigger_codes_json", sa.JSON(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("delivery_status", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "delivery_status = 'pending'",
            name="ck_research_notification_outbox_pending",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["security_id"], ["securities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_change_item_id"],
            ["opportunity_change_items.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_change_run_id"],
            ["opportunity_change_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "notification_key_sha256", name="uq_research_notification_outbox_key"
        ),
    )
    for column in (
        "notification_key_sha256",
        "source_change_run_id",
        "source_change_item_id",
        "company_id",
        "security_id",
        "delivery_status",
    ):
        op.create_index(
            f"ix_research_notification_outbox_{column}",
            "research_notification_outbox",
            [column],
        )


def downgrade() -> None:
    for column in (
        "delivery_status",
        "security_id",
        "company_id",
        "source_change_item_id",
        "source_change_run_id",
        "notification_key_sha256",
    ):
        op.drop_index(
            f"ix_research_notification_outbox_{column}",
            table_name="research_notification_outbox",
        )
    op.drop_table("research_notification_outbox")
