"""Canonical identity, provenance, quality, and append-only market-data models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inflector_database.base import Base


class TimestampMixin:
    """Creation and modification timestamps for operational identity records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Company(TimestampMixin, Base):
    """Canonical issuer identity, intentionally independent of trading symbols."""

    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    legal_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sector: Mapped[str] = mapped_column(String(120), nullable=False)
    industry: Mapped[str] = mapped_column(String(120), nullable=False)

    securities: Mapped[list[Security]] = relationship(
        back_populates="company", cascade="all, delete-orphan", lazy="selectin"
    )


class Security(TimestampMixin, Base):
    """A security issued by a canonical company."""

    __tablename__ = "securities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    isin: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    security_type: Mapped[str] = mapped_column(String(64), nullable=False, default="equity")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    company: Mapped[Company] = relationship(back_populates="securities")
    listings: Mapped[list[ExchangeListing]] = relationship(
        back_populates="security", cascade="all, delete-orphan", lazy="selectin"
    )


class ExchangeListing(TimestampMixin, Base):
    """A dated exchange-specific symbol for a security."""

    __tablename__ = "exchange_listings"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "exchange", "symbol", "valid_from", name="uq_listing_identity_period"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    security: Mapped[Security] = relationship(back_populates="listings")


class DataProvider(TimestampMixin, Base):
    """Registered identity of a data provider, intentionally without secrets."""

    __tablename__ = "data_providers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(80), nullable=False)
    licence_name: Mapped[str] = mapped_column(String(160), nullable=False)
    licence_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProviderDataset(TimestampMixin, Base):
    """A provider-owned dataset and its retention/redistribution policy."""

    __tablename__ = "provider_datasets"
    __table_args__ = (UniqueConstraint("provider_id", "code", name="uq_provider_dataset_code"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    licence_class: Mapped[str] = mapped_column(String(80), nullable=False)
    retention_days: Mapped[int | None] = mapped_column(nullable=True)
    redistributable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class IngestionRun(Base):
    """An auditable execution of one provider dataset ingestion."""

    __tablename__ = "ingestion_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, default=uuid4, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    records_received: Mapped[int] = mapped_column(nullable=False, default=0)
    records_accepted: Mapped[int] = mapped_column(nullable=False, default=0)
    records_quarantined: Mapped[int] = mapped_column(nullable=False, default=0)
    records_duplicated: Mapped[int] = mapped_column(nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceRecord(Base):
    """Immutable normalized observation metadata linked to archived raw bytes."""

    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint(
            "provider_dataset_id",
            "external_record_id",
            "content_sha256",
            name="uq_source_record_content",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    ingestion_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    raw_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    raw_payload_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DataQualityIssue(Base):
    """Auditable reason a source observation was quarantined or warned."""

    __tablename__ = "data_quality_issues"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    ingestion_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_records.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rule_code: Mapped[str] = mapped_column(String(120), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PriceBar(Base):
    """Append-only daily bar with source provenance and revision timestamps."""

    __tablename__ = "price_bars"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    open_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    high_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    low_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FiscalPeriod(Base):
    """Company-specific reported duration or instant period; never a derived TTM."""

    __tablename__ = "fiscal_periods"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    period_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    fiscal_year: Mapped[int] = mapped_column(nullable=False)
    fiscal_quarter: Mapped[int | None] = mapped_column(nullable=True)
    is_ytd: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class FinancialFiling(Base):
    """Immutable reporting header that can contain facts for multiple periods."""

    __tablename__ = "financial_filings"
    __table_args__ = (
        UniqueConstraint(
            "provider_dataset_id",
            "external_filing_id",
            "filing_scope",
            "available_at",
            "revision_at",
            name="uq_financial_filing_revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id"), nullable=False
    )
    external_filing_id: Mapped[str] = mapped_column(String(255), nullable=False)
    filing_type: Mapped[str] = mapped_column(String(64), nullable=False)
    filing_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    is_restatement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FinancialMetricDefinition(Base):
    """Controlled vocabulary for reported, rather than derived, financial metrics."""

    __tablename__ = "financial_metric_definitions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    statement_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    unit_category: Mapped[str] = mapped_column(String(32), nullable=False)
    semantic_type: Mapped[str] = mapped_column(String(16), nullable=False)


class FinancialFact(Base):
    """Immutable reported financial observation with exact source lineage."""

    __tablename__ = "financial_facts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    filing_id: Mapped[UUID] = mapped_column(
        ForeignKey("financial_filings.id"), nullable=False, index=True
    )
    fiscal_period_id: Mapped[UUID] = mapped_column(
        ForeignKey("fiscal_periods.id"), nullable=False, index=True
    )
    metric_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("financial_metric_definitions.id"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id"), unique=True, nullable=False
    )
    reported_value: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    reported_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_scale: Mapped[str] = mapped_column(String(32), nullable=False)
    reported_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CorporateAction(Base):
    """Append-only security-specific action; no adjustment factors are derived here."""

    __tablename__ = "corporate_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id"), nullable=False, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id"), unique=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ex_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ratio_numerator: Mapped[int | None] = mapped_column(nullable=True)
    ratio_denominator: Mapped[int | None] = mapped_column(nullable=True)
    cash_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    cash_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    cash_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subscription_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 8), nullable=True)
    subscription_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SecurityRelationship(Base):
    """Explicit historical succession between distinct immutable security identities."""

    __tablename__ = "security_relationships"
    __table_args__ = (
        UniqueConstraint(
            "predecessor_security_id",
            "successor_security_id",
            "relationship_type",
            name="uq_security_relationship",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    predecessor_security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id"), nullable=False, index=True
    )
    successor_security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_record_id: Mapped[UUID] = mapped_column(ForeignKey("source_records.id"), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
