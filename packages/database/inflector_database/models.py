"""Canonical identity, provenance, facts, and immutable policy models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from inflector_database.base import Base


class ExactDecimal(TypeDecorator[Decimal]):
    """Persist exact wide decimals, using text where SQLite lacks exact NUMERIC."""

    impl = Numeric(50, 28)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(80))
        return dialect.type_descriptor(Numeric(50, 28))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> object:
        if value is None:
            return None
        return format(value, "f") if dialect.name == "sqlite" else value

    def process_result_value(self, value: object, dialect: Dialect) -> Decimal | None:
        del dialect
        return None if value is None else Decimal(str(value))


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
    market_cap: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    delivery_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivery_percentage: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BenchmarkSeries(Base):
    """Provider-dataset-local benchmark identity without hidden reconciliation."""

    __tablename__ = "benchmark_series"
    __table_args__ = (
        UniqueConstraint("provider_dataset_id", "code", name="uq_benchmark_series_dataset_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BenchmarkBar(Base):
    """Append-only raw benchmark bar with source and knowledge-time provenance."""

    __tablename__ = "benchmark_bars"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    benchmark_series_id: Mapped[UUID] = mapped_column(
        ForeignKey("benchmark_series.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    open_value: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    high_value: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    low_value: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    close_value: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
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
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)
    old_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    successor_isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
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


class ModelVersion(Base):
    """Immutable identity for one version of model/code semantics."""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint(
            "model_family",
            "semantic_version",
            name="uq_model_version_family_semantic",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    model_family: Mapped[str] = mapped_column(String(120), nullable=False)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scoring_configurations: Mapped[list[ScoringConfiguration]] = relationship(
        back_populates="model_version"
    )


class ScoringConfiguration(Base):
    """Immutable versioned policy document associated with one model version."""

    __tablename__ = "scoring_configurations"
    __table_args__ = (
        UniqueConstraint(
            "model_version_id",
            "configuration_name",
            "configuration_version",
            name="uq_scoring_configuration_version",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    model_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    configuration_name: Mapped[str] = mapped_column(String(120), nullable=False)
    configuration_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    configuration_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    model_version: Mapped[ModelVersion] = relationship(back_populates="scoring_configurations")


class ScoreSnapshot(Base):
    """Immutable partial scoring audit at one explicit PIT knowledge cutoff."""

    __tablename__ = "score_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    model_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scoring_configuration_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_configurations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    configuration_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    knowledge_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ending_fiscal_year: Mapped[int] = mapped_column(nullable=False)
    ending_fiscal_quarter: Mapped[int] = mapped_column(nullable=False)
    selected_provider_dataset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=True
    )
    selected_filing_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot_status: Mapped[str] = mapped_column(String(64), nullable=False)
    eligibility_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    eligibility_inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    eligibility_reasons_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    eligibility_warnings_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    financial_core_coverage: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    confidence_inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    confidence_details_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    top_level_component_weight_coverage: Mapped[Decimal] = mapped_column(
        ExactDecimal(), nullable=False
    )
    available_component_codes_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    missing_component_codes_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    context_resolution_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    input_manifest_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    fingerprint_payload_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    snapshot_fingerprint_sha256: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    final_score: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(96), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    components: Mapped[list[ScoreComponent]] = relationship(
        back_populates="score_snapshot", lazy="selectin"
    )


class ScoreComponent(Base):
    """Immutable audit record for one available top-level component."""

    __tablename__ = "score_components"
    __table_args__ = (
        UniqueConstraint("score_snapshot_id", "component_code", name="uq_score_component_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    score_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    component_code: Mapped[str] = mapped_column(String(96), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    configured_top_level_weight: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    subfactor_weight_coverage: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    final_contribution: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(96), nullable=False)
    missing_subfactors_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    warnings_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    score_snapshot: Mapped[ScoreSnapshot] = relationship(back_populates="components")
    explanations: Mapped[list[ScoreExplanation]] = relationship(
        back_populates="score_component", lazy="selectin"
    )


class ScoreExplanation(Base):
    """Structured mathematical explanation for one available subfactor."""

    __tablename__ = "score_explanations"
    __table_args__ = (
        UniqueConstraint("score_component_id", "factor_code", name="uq_score_explanation_factor"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    score_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    score_component_id: Mapped[UUID] = mapped_column(
        ForeignKey("score_components.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    factor_code: Mapped[str] = mapped_column(String(96), nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False)
    raw_value: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    raw_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    normalized_score: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    configured_weight: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    effective_weight: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    component_contribution: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    input_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(120), nullable=False)
    template_code: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_manifest_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    score_component: Mapped[ScoreComponent] = relationship(back_populates="explanations")
