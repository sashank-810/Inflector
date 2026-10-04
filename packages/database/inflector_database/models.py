"""Canonical identity, provenance, facts, and immutable policy models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
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


class AttentionObservation(Base):
    """Append-only provider-reported external attention measurement."""

    __tablename__ = "attention_observations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    metric_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reported_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reported_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_code: Mapped[str] = mapped_column(String(120), nullable=False)
    methodology_version: Mapped[str] = mapped_column(String(120), nullable=False)
    measurement_definition_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    observation_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    window_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Announcement(Base):
    """Append-only public announcement metadata with immutable source provenance."""

    __tablename__ = "announcements"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    provider_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(Base):
    """Append-only document identity and location; document bytes remain external."""

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    document_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    document_uri: Mapped[str] = mapped_column(Text, nullable=False)
    document_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentAsset(Base):
    """Immutable content-addressed bytes acquired for one Phase 6A document."""

    __tablename__ = "document_assets"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "content_sha256",
            name="uq_document_asset_content",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_uri: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    declared_media_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detected_media_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    warnings_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentTextExtraction(Base):
    """Immutable deterministic text extraction identity and object lineage."""

    __tablename__ = "document_text_extractions"
    __table_args__ = (
        UniqueConstraint(
            "document_asset_id",
            "extractor_code",
            "extractor_semantic_version",
            "extractor_runtime_version",
            name="uq_document_text_extraction_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    extractor_code: Mapped[str] = mapped_column(String(80), nullable=False)
    extractor_semantic_version: Mapped[str] = mapped_column(String(80), nullable=False)
    extractor_runtime_version: Mapped[str] = mapped_column(String(80), nullable=False)
    text_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    character_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    page_map_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    warnings_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnnouncementDocument(Base):
    """Revision-specific relation between an announcement and its document set."""

    __tablename__ = "announcement_documents"

    announcement_id: Mapped[UUID] = mapped_column(
        ForeignKey("announcements.id", ondelete="RESTRICT"), primary_key=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)


class BusinessEvent(Base):
    """Immutable neutral event detected from one announcement revision."""

    __tablename__ = "business_events"
    __table_args__ = (
        UniqueConstraint(
            "announcement_id",
            "event_type",
            "ruleset_code",
            "ruleset_semantic_version",
            name="uq_business_event_announcement_type_ruleset",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    announcement_id: Mapped[UUID] = mapped_column(
        ForeignKey("announcements.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ruleset_code: Mapped[str] = mapped_column(String(80), nullable=False)
    ruleset_semantic_version: Mapped[str] = mapped_column(String(80), nullable=False)
    matched_rule_codes_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    warnings_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    detection_fingerprint_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    derived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BusinessEventEvidence(Base):
    """Exact source span supporting a deterministic business event."""

    __tablename__ = "business_event_evidence"
    __table_args__ = (
        UniqueConstraint(
            "business_event_id",
            "evidence_fingerprint_sha256",
            name="uq_business_event_evidence_fingerprint",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    business_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    announcement_id: Mapped[UUID] = mapped_column(
        ForeignKey("announcements.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    evidence_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    document_asset_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_assets.id", ondelete="RESTRICT"), nullable=True
    )
    text_extraction_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_text_extractions.id", ondelete="RESTRICT"), nullable=True
    )
    rule_code: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_semantic_version: Mapped[str] = mapped_column(String(80), nullable=False)
    start_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_numbers_json: Mapped[list[int]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    page_text_sha256s_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    excerpt_text: Mapped[str] = mapped_column(Text, nullable=False)
    excerpt_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BusinessEventQuantitativeDerivation(Base):
    """Immutable quantitative-rule outcome, including an explicit empty outcome."""

    __tablename__ = "business_event_quantitative_derivations"
    __table_args__ = (
        UniqueConstraint(
            "business_event_id",
            "ruleset_code",
            "ruleset_semantic_version",
            name="uq_business_event_quant_derivation_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    business_event_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ruleset_code: Mapped[str] = mapped_column(String(96), nullable=False)
    ruleset_semantic_version: Mapped[str] = mapped_column(String(96), nullable=False)
    source_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    available_fact_codes_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    warnings_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    derivation_fingerprint_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    derived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BusinessEventQuantitativeFact(Base):
    """Exact quantitative observation from one accepted event-evidence span."""

    __tablename__ = "business_event_quantitative_facts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    derivation_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_event_quantitative_derivations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    business_event_evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("business_event_evidence.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    fact_code: Mapped[str] = mapped_column(String(96), nullable=False)
    fact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_semantic_version: Mapped[str] = mapped_column(String(96), nullable=False)
    start_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reported_value: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    reported_scale: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reported_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    normalized_value: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    date_value: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    warnings_json: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    fact_fingerprint_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
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


class MarketDeliveryObservation(Base):
    """Append-only daily security delivery evidence from a distinct artifact."""

    __tablename__ = "market_delivery_observations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    series: Mapped[str] = mapped_column(String(16), nullable=False)
    total_traded_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivery_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reported_delivery_percentage: Mapped[Decimal | None] = mapped_column(
        ExactDecimal(), nullable=True
    )
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
    selected_security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True
    )
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


class OperationalRun(Base):
    """Persistent identity and lifecycle for one explicit production cycle."""

    __tablename__ = "operational_runs"
    __table_args__ = (
        UniqueConstraint("run_key_sha256", name="uq_operational_run_key"),
        CheckConstraint(
            "status IN ('planned','running','completed','completed_with_symbol_failures',"
            "'failed','stale')",
            name="ck_operational_run_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operations_profile_code: Mapped[str] = mapped_column(String(120), nullable=False)
    operations_profile_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    research_profile_code: Mapped[str] = mapped_column(String(120), nullable=False)
    research_profile_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_family: Mapped[str] = mapped_column(String(120), nullable=False)
    fiscal_year: Mapped[int] = mapped_column(nullable=False)
    fiscal_quarter: Mapped[int] = mapped_column(nullable=False)
    cycle_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    knowledge_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol_set_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ordered_symbols_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="planned")
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    stages: Mapped[list[OperationalRunStage]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )
    symbols: Mapped[list[OperationalRunSymbol]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class OperationalRunStage(Base):
    """Resumable state and bounded result history for one production stage."""

    __tablename__ = "operational_run_stages"
    __table_args__ = (
        UniqueConstraint("operational_run_id", "stage_name", name="uq_operational_run_stage"),
        CheckConstraint(
            "status IN ('planned','running','completed','failed','blocked','skipped')",
            name="ck_operational_stage_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    operational_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("operational_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_order: Mapped[int] = mapped_column(nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    attempt_history_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=list
    )
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[OperationalRun] = relationship(back_populates="stages")


class OperationalRunSymbol(Base):
    """Per-symbol outcome for isolation, status inspection, and burn-in comparison."""

    __tablename__ = "operational_run_symbols"
    __table_args__ = (
        UniqueConstraint("operational_run_id", "symbol", name="uq_operational_run_symbol"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    operational_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("operational_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    result_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    change_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[OperationalRun] = relationship(back_populates="symbols")


class BacktestRun(Base):
    """Immutable identity and bounded lifecycle for one historical evaluation."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("run_key_sha256", name="uq_backtest_run_key"),
        CheckConstraint(
            "status IN ('planned','snapshots_built','outcomes_built','completed','failed')",
            name="ck_backtest_run_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    backtest_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    backtest_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    availability_manifest_code: Mapped[str] = mapped_column(String(120), nullable=False)
    availability_manifest_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    scoring_configuration_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_configurations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scoring_configuration_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    research_profile_code: Mapped[str] = mapped_column(String(120), nullable=False)
    research_profile_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    financial_primitive_policy_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    financial_endpoint_policy_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    source_state_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_family: Mapped[str] = mapped_column(String(120), nullable=False)
    cutoff_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cutoff_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cutoff_cadence: Mapped[str] = mapped_column(String(64), nullable=False)
    universe_policy: Mapped[str] = mapped_column(String(64), nullable=False)
    benchmark_code: Mapped[str] = mapped_column(String(64), nullable=False)
    return_horizons_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    ordered_symbols_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    symbol_set_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    outcome_data_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    observations: Mapped[list[BacktestObservation]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class BacktestObservation(Base):
    """One audited universe decision and optional immutable V5 snapshot reference."""

    __tablename__ = "backtest_observations"
    __table_args__ = (
        UniqueConstraint(
            "backtest_run_id",
            "security_id",
            "knowledge_cutoff",
            name="uq_backtest_observation_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    backtest_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    company_id: Mapped[UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    security_id: Mapped[UUID] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    score_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    knowledge_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    observation_status: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_fiscal_year: Mapped[int | None] = mapped_column(nullable=True)
    selected_fiscal_quarter: Mapped[int | None] = mapped_column(nullable=True)
    selected_filing_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    snapshot_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    research_state_projection_version: Mapped[str] = mapped_column(String(96), nullable=False)
    research_state_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    research_state_changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[BacktestRun] = relationship(back_populates="observations")
    outcomes: Mapped[list[BacktestOutcome]] = relationship(
        back_populates="observation", cascade="all, delete-orphan", lazy="selectin"
    )


class BacktestOutcome(Base):
    """Append-only forward price outcome computed after a research state is frozen."""

    __tablename__ = "backtest_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "backtest_observation_id",
            "horizon_observations",
            name="uq_backtest_outcome_horizon",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    backtest_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_observations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    horizon_observations: Mapped[int] = mapped_column(nullable=False)
    outcome_status: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    entry_adjusted_close: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    exit_adjusted_close: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    security_return: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    benchmark_return: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    excess_return: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    outcome_data_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    provenance_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    observation: Mapped[BacktestObservation] = relationship(back_populates="outcomes")


class HistoricalUniverseRun(Base):
    """Immutable authoritative historical NSE cohort projection."""

    __tablename__ = "historical_universe_runs"
    __table_args__ = (
        UniqueConstraint("run_key_sha256", name="uq_historical_universe_run_key"),
        CheckConstraint(
            "status IN ('planned','completed','failed')",
            name="ck_historical_universe_run_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    universe_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    universe_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_state_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    member_count: Mapped[int] = mapped_column(nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    members: Mapped[list[HistoricalUniverseMemberRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class HistoricalUniverseMemberRecord(Base):
    """One historical artifact member with explicit identity-resolution state."""

    __tablename__ = "historical_universe_members"
    __table_args__ = (
        UniqueConstraint(
            "historical_universe_run_id",
            "member_fingerprint_sha256",
            name="uq_historical_universe_member_fingerprint",
        ),
        CheckConstraint(
            "membership_status IN "
            "('eligible','unresolved_identity','ambiguous_identity',"
            "'unsupported_security_type','excluded_exchange','excluded_series')",
            name="ck_historical_universe_member_status",
        ),
        CheckConstraint(
            "membership_status != 'eligible' OR "
            "(company_id IS NOT NULL AND security_id IS NOT NULL "
            "AND exchange_listing_id IS NOT NULL AND reason_code IS NULL)",
            name="ck_historical_universe_member_eligible_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    historical_universe_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("historical_universe_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    historical_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    historical_isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    series: Mapped[str] = mapped_column(String(16), nullable=False)
    membership_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    exchange_listing_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exchange_listings.id", ondelete="RESTRICT"), nullable=True
    )
    membership_status: Mapped[str] = mapped_column(String(48), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    source_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_records.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    member_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[HistoricalUniverseRun] = relationship(back_populates="members")


class MultibaggerLabelRun(Base):
    """Immutable evaluation run over frozen J observations and outcome data."""

    __tablename__ = "multibagger_label_runs"
    __table_args__ = (
        UniqueConstraint("run_key_sha256", name="uq_multibagger_label_run_key"),
        CheckConstraint(
            "status IN ('planned','completed','failed')",
            name="ck_multibagger_label_run_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    label_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_backtest_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_backtest_run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome_data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    corporate_action_provider_dataset_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    source_state_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ordered_contracts_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    algorithm_versions_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    labels: Mapped[list[MultibaggerOutcomeLabel]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class MultibaggerOutcomeLabel(Base):
    """Append-only factual contract label independent of score or rank."""

    __tablename__ = "multibagger_outcome_labels"
    __table_args__ = (
        UniqueConstraint(
            "multibagger_label_run_id",
            "backtest_observation_id",
            "contract_code",
            name="uq_multibagger_outcome_label_contract",
        ),
        UniqueConstraint(
            "label_fingerprint_sha256", name="uq_multibagger_outcome_label_fingerprint"
        ),
        CheckConstraint(
            "classification IN ('positive','negative','unmatured','unavailable')",
            name="ck_multibagger_outcome_label_classification",
        ),
        CheckConstraint(
            "(classification IN ('positive','negative') "
            "AND label_matured_at IS NOT NULL AND unavailable_reason IS NULL) OR "
            "(classification = 'unmatured' AND label_matured_at IS NULL "
            "AND unavailable_reason IS NULL) OR "
            "(classification = 'unavailable' AND unavailable_reason IS NOT NULL)",
            name="ck_multibagger_outcome_label_state_fields",
        ),
        CheckConstraint(
            "threshold_multiple > 1 AND horizon_calendar_years > 0 "
            "AND trading_observations_available >= 0",
            name="ck_multibagger_outcome_label_measurement_bounds",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    multibagger_label_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("multibagger_label_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    backtest_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("backtest_observations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    contract_code: Mapped[str] = mapped_column(String(32), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    unavailable_reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    threshold_multiple: Mapped[Decimal] = mapped_column(ExactDecimal(), nullable=False)
    horizon_calendar_years: Mapped[int] = mapped_column(nullable=False)
    entry_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    adjusted_entry_close: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    horizon_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_threshold_hit_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    trading_observations_available: Mapped[int] = mapped_column(nullable=False, default=0)
    peak_adjusted_close: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    peak_price_multiple: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    maximum_forward_price_return: Mapped[Decimal | None] = mapped_column(
        ExactDecimal(), nullable=True
    )
    endpoint_adjusted_close: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    endpoint_return: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    calendar_days_to_threshold: Mapped[int | None] = mapped_column(nullable=True)
    trading_observations_to_threshold: Mapped[int | None] = mapped_column(nullable=True)
    maximum_drawdown: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    listing_valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    label_matured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_data_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    label_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[MultibaggerLabelRun] = relationship(back_populates="labels")


class OpportunityDiscoveryRun(Base):
    """Immutable identity and completed audit for one current discovery ranking."""

    __tablename__ = "opportunity_discovery_runs"
    __table_args__ = (
        UniqueConstraint("run_key_sha256", name="uq_opportunity_discovery_run_key"),
        CheckConstraint(
            "status IN ('planned','completed','failed')",
            name="ck_opportunity_discovery_run_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    discovery_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    discovery_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scoring_configuration_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_configurations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scoring_configuration_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    research_profile_code: Mapped[str] = mapped_column(String(120), nullable=False)
    research_profile_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    financial_primitive_policy_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    financial_endpoint_policy_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    model_family: Mapped[str] = mapped_column(String(120), nullable=False)
    discovery_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    universe_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    ordered_symbols_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    symbol_set_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_snapshot_set_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    snapshot_selection_version: Mapped[str] = mapped_column(String(96), nullable=False)
    ranking_version: Mapped[str] = mapped_column(String(96), nullable=False)
    inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[OpportunityDiscoveryItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class OpportunityDiscoveryItem(Base):
    """One frozen selected-snapshot decision and optional dense V5 score rank."""

    __tablename__ = "opportunity_discovery_items"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_discovery_run_id",
            "symbol",
            name="uq_opportunity_discovery_item_symbol",
        ),
        CheckConstraint(
            "(rankable = true AND unranked_reason IS NULL AND score_rank IS NOT NULL "
            "AND display_order IS NOT NULL) OR "
            "(rankable = false AND unranked_reason IS NOT NULL AND score_rank IS NULL "
            "AND display_order IS NULL)",
            name="ck_opportunity_discovery_item_rankability",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    opportunity_discovery_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunity_discovery_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    score_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    rankable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    unranked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score_rank: Mapped[int | None] = mapped_column(nullable=True)
    display_order: Mapped[int | None] = mapped_column(nullable=True)
    snapshot_age_days: Mapped[int | None] = mapped_column(nullable=True)
    freshness_state: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_snapshot_fingerprint_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[OpportunityDiscoveryRun] = relationship(back_populates="items")
    score_snapshot: Mapped[ScoreSnapshot | None] = relationship(lazy="selectin")


class OpportunityChangeRun(Base):
    """Immutable comparison of two completed opportunity discovery runs."""

    __tablename__ = "opportunity_change_runs"
    __table_args__ = (
        UniqueConstraint("run_key_sha256", name="uq_opportunity_change_run_key"),
        CheckConstraint(
            "status IN ('planned','completed','failed','incompatible_runs')",
            name="ck_opportunity_change_run_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    change_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    change_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_discovery_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunity_discovery_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    current_discovery_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunity_discovery_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    baseline_run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_run_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_snapshot_set_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    current_snapshot_set_checksum_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    comparison_version: Mapped[str] = mapped_column(String(96), nullable=False)
    inputs_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[OpportunityChangeItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )


class OpportunityChangeItem(Base):
    """Canonical factual research-state change for one matched K identity."""

    __tablename__ = "opportunity_change_items"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_change_run_id",
            "identity_key",
            name="uq_opportunity_change_item_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    opportunity_change_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunity_change_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    identity_key: Mapped[str] = mapped_column(String(160), nullable=False)
    identity_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    baseline_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_discovery_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("opportunity_discovery_items.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    current_discovery_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("opportunity_discovery_items.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    baseline_score_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    current_score_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("score_snapshots.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    change_codes_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    baseline_rankable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    current_rankable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    baseline_unranked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_unranked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_final_score: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    current_final_score: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    score_delta: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    baseline_score_rank: Mapped[int | None] = mapped_column(nullable=True)
    current_score_rank: Mapped[int | None] = mapped_column(nullable=True)
    rank_delta: Mapped[int | None] = mapped_column(nullable=True)
    baseline_confidence: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    current_confidence: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    confidence_delta: Mapped[Decimal | None] = mapped_column(ExactDecimal(), nullable=True)
    components_gained_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    components_lost_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    component_change_detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[OpportunityChangeRun] = relationship(back_populates="items")


class ResearchNotificationOutbox(Base):
    """Immutable transport-neutral notification projected from one L item."""

    __tablename__ = "research_notification_outbox"
    __table_args__ = (
        UniqueConstraint(
            "notification_key_sha256", name="uq_research_notification_outbox_key"
        ),
        CheckConstraint(
            "delivery_status = 'pending'",
            name="ck_research_notification_outbox_pending",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    notification_key_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    alert_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    alert_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_schema_version: Mapped[str] = mapped_column(String(96), nullable=False)
    source_change_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunity_change_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_change_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("opportunity_change_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    security_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("securities.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    baseline_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched_trigger_codes_json: Mapped[list[object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    payload_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    delivery_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    source_change_run: Mapped[OpportunityChangeRun] = relationship(lazy="selectin")
    source_change_item: Mapped[OpportunityChangeItem] = relationship(lazy="selectin")


class ResearchNotificationDelivery(Base):
    """Mutable delivery lifecycle for one immutable N event and logical target."""

    __tablename__ = "research_notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "delivery_key_sha256", name="uq_research_notification_delivery_key"
        ),
        CheckConstraint(
            "status IN ('pending','claimed','retry_wait','delivered','dead_letter')",
            name="ck_research_notification_delivery_status",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_research_notification_delivery_attempt_count"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    delivery_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    outbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_notification_outbox.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    notification_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_policy_code: Mapped[str] = mapped_column(String(120), nullable=False)
    delivery_policy_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    transport_code: Mapped[str] = mapped_column(String(64), nullable=False)
    target_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rendering_version: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    claim_owner_token: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    last_error_detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    outbox: Mapped[ResearchNotificationOutbox] = relationship(lazy="selectin")
    attempts: Mapped[list[ResearchNotificationDeliveryAttempt]] = relationship(
        back_populates="delivery", lazy="selectin"
    )


class ResearchNotificationDeliveryAttempt(Base):
    """Append-only audit of one started Telegram delivery attempt."""

    __tablename__ = "research_notification_delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "attempt_number",
            name="uq_research_notification_delivery_attempt_number",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('delivered','retryable_failure','permanent_failure')",
            name="ck_research_notification_delivery_attempt_outcome",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_research_notification_delivery_attempt_number_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_notification_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(nullable=False)
    worker_token: Mapped[str] = mapped_column(String(160), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    http_status: Mapped[int | None] = mapped_column(nullable=True)
    telegram_error_code: Mapped[int | None] = mapped_column(nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    detail_json: Mapped[dict[str, object]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False, default=dict
    )

    delivery: Mapped[ResearchNotificationDelivery] = relationship(
        back_populates="attempts"
    )
