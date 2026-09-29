"""Add deterministic quantitative business-event derivations and facts.

Revision ID: 20260929_0014
Revises: 20260928_0013
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

revision = "20260929_0014"
down_revision = "20260928_0013"
branch_labels = None
depends_on = None


def _json() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _exact_decimal() -> TypeEngine[object]:
    return sa.Numeric(50, 28).with_variant(sa.String(length=80), "sqlite")


def upgrade() -> None:
    op.create_table(
        "business_event_quantitative_derivations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("business_event_id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_code", sa.String(length=96), nullable=False),
        sa.Column("ruleset_semantic_version", sa.String(length=96), nullable=False),
        sa.Column("source_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_fact_codes_json", _json(), nullable=False),
        sa.Column("warnings_json", _json(), nullable=False),
        sa.Column("derivation_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("derived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["business_event_id"], ["business_events.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_event_id",
            "ruleset_code",
            "ruleset_semantic_version",
            name="uq_business_event_quant_derivation_identity",
        ),
        sa.UniqueConstraint("derivation_fingerprint_sha256"),
    )
    for column in (
        "business_event_id",
        "source_available_at",
        "derivation_fingerprint_sha256",
    ):
        op.create_index(
            f"ix_business_event_quantitative_derivations_{column}",
            "business_event_quantitative_derivations",
            [column],
        )

    op.create_table(
        "business_event_quantitative_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("derivation_id", sa.Uuid(), nullable=False),
        sa.Column("business_event_evidence_id", sa.Uuid(), nullable=False),
        sa.Column("fact_code", sa.String(length=96), nullable=False),
        sa.Column("fact_kind", sa.String(length=32), nullable=False),
        sa.Column("rule_code", sa.String(length=120), nullable=False),
        sa.Column("rule_semantic_version", sa.String(length=96), nullable=False),
        sa.Column("start_offset", sa.BigInteger(), nullable=False),
        sa.Column("end_offset", sa.BigInteger(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("raw_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("reported_value", _exact_decimal(), nullable=True),
        sa.Column("reported_scale", sa.String(length=32), nullable=True),
        sa.Column("reported_unit", sa.String(length=64), nullable=True),
        sa.Column("reported_currency", sa.String(length=3), nullable=True),
        sa.Column("normalized_value", _exact_decimal(), nullable=True),
        sa.Column("normalized_unit", sa.String(length=64), nullable=True),
        sa.Column("date_value", sa.Date(), nullable=True),
        sa.Column("source_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("warnings_json", _json(), nullable=False),
        sa.Column("fact_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["derivation_id"],
            ["business_event_quantitative_derivations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["business_event_evidence_id"],
            ["business_event_evidence.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_fingerprint_sha256"),
    )
    for column in (
        "derivation_id",
        "business_event_evidence_id",
        "fact_fingerprint_sha256",
    ):
        op.create_index(
            f"ix_business_event_quantitative_facts_{column}",
            "business_event_quantitative_facts",
            [column],
        )


def downgrade() -> None:
    for column in reversed(
        (
            "derivation_id",
            "business_event_evidence_id",
            "fact_fingerprint_sha256",
        )
    ):
        op.drop_index(
            f"ix_business_event_quantitative_facts_{column}",
            table_name="business_event_quantitative_facts",
        )
    op.drop_table("business_event_quantitative_facts")
    for column in reversed(
        (
            "business_event_id",
            "source_available_at",
            "derivation_fingerprint_sha256",
        )
    ):
        op.drop_index(
            f"ix_business_event_quantitative_derivations_{column}",
            table_name="business_event_quantitative_derivations",
        )
    op.drop_table("business_event_quantitative_derivations")
