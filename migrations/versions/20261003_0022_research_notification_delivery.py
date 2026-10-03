"""Add reliable research notification delivery lifecycle.

Revision ID: 20261003_0022
Revises: 20261003_0021
"""

import sqlalchemy as sa
from alembic import op

revision = "20261003_0022"
down_revision = "20261003_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_notification_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_key_sha256", sa.String(64), nullable=False),
        sa.Column("outbox_id", sa.Uuid(), nullable=False),
        sa.Column("notification_key_sha256", sa.String(64), nullable=False),
        sa.Column("delivery_policy_code", sa.String(120), nullable=False),
        sa.Column("delivery_policy_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("transport_code", sa.String(64), nullable=False),
        sa.Column("target_code", sa.String(64), nullable=False),
        sa.Column("rendering_version", sa.String(96), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_owner_token", sa.String(160), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(96), nullable=True),
        sa.Column("last_error_detail_json", sa.JSON(), nullable=False),
        sa.Column("provider_message_id", sa.String(128), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_research_notification_delivery_attempt_count"
        ),
        sa.CheckConstraint(
            "status IN ('pending','claimed','retry_wait','delivered','dead_letter')",
            name="ck_research_notification_delivery_status",
        ),
        sa.ForeignKeyConstraint(
            ["outbox_id"], ["research_notification_outbox.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_key_sha256", name="uq_research_notification_delivery_key"
        ),
    )
    for column in (
        "delivery_key_sha256",
        "outbox_id",
        "status",
        "next_attempt_at",
        "claim_expires_at",
    ):
        op.create_index(
            f"ix_research_notification_deliveries_{column}",
            "research_notification_deliveries",
            [column],
        )

    op.create_table(
        "research_notification_delivery_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_token", sa.String(160), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("telegram_error_code", sa.Integer(), nullable=True),
        sa.Column("provider_message_id", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(96), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_research_notification_delivery_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('delivered','retryable_failure','permanent_failure')",
            name="ck_research_notification_delivery_attempt_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["research_notification_deliveries.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_research_notification_delivery_attempt_number",
        ),
    )
    op.create_index(
        "ix_research_notification_delivery_attempts_delivery_id",
        "research_notification_delivery_attempts",
        ["delivery_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_research_notification_delivery_attempts_delivery_id",
        table_name="research_notification_delivery_attempts",
    )
    op.drop_table("research_notification_delivery_attempts")
    for column in (
        "claim_expires_at",
        "next_attempt_at",
        "status",
        "outbox_id",
        "delivery_key_sha256",
    ):
        op.drop_index(
            f"ix_research_notification_deliveries_{column}",
            table_name="research_notification_deliveries",
        )
    op.drop_table("research_notification_deliveries")
