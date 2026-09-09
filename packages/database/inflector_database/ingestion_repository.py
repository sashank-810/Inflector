"""Persistence operations used by the data-ingestion service."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.providers import ProviderMetadata, UniverseRecord
from inflector_database.models import (
    Company,
    DataProvider,
    DataQualityIssue,
    ExchangeListing,
    IngestionRun,
    PriceBar,
    ProviderDataset,
    Security,
    SourceRecord,
)


class IngestionRepository:
    """Small repository boundary for ingestion persistence."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_dataset(self, metadata: ProviderMetadata) -> ProviderDataset:
        provider = self.session.scalar(
            select(DataProvider).where(DataProvider.code == metadata.provider_code)
        )
        if provider is None:
            provider = DataProvider(
                code=metadata.provider_code,
                provider_type=metadata.provider_type,
                licence_name=metadata.licence_class,
                licence_reference=metadata.licence_reference,
            )
            self.session.add(provider)
            self.session.flush()
        dataset = self.session.scalar(
            select(ProviderDataset).where(
                ProviderDataset.provider_id == provider.id,
                ProviderDataset.code == metadata.dataset_code,
            )
        )
        if dataset is None:
            dataset = ProviderDataset(
                provider_id=provider.id,
                code=metadata.dataset_code,
                licence_class=metadata.licence_class,
                retention_days=metadata.retention_days,
                redistributable=metadata.redistributable,
            )
            self.session.add(dataset)
            self.session.flush()
        return dataset

    def create_run(
        self, dataset_id: UUID, started_at: datetime, cursor: str | None
    ) -> IngestionRun:
        run = IngestionRun(
            provider_dataset_id=dataset_id, status="running", started_at=started_at, cursor=cursor
        )
        self.session.add(run)
        self.session.flush()
        return run

    def source_exists(self, dataset_id: UUID, external_id: str, content_sha256: str) -> bool:
        return (
            self.session.scalar(
                select(SourceRecord.id).where(
                    SourceRecord.provider_dataset_id == dataset_id,
                    SourceRecord.external_record_id == external_id,
                    SourceRecord.content_sha256 == content_sha256,
                )
            )
            is not None
        )

    def create_source(
        self,
        *,
        run_id: UUID,
        dataset_id: UUID,
        external_id: str,
        source_uri: str,
        raw_object_key: str,
        raw_sha256: str,
        content_sha256: str,
        retrieved_at: datetime,
        reported_at: datetime | None,
        published_at: datetime | None,
        available_at: datetime | None,
        revision_at: datetime | None,
        parse_status: str,
    ) -> SourceRecord:
        source = SourceRecord(
            ingestion_run_id=run_id,
            provider_dataset_id=dataset_id,
            external_record_id=external_id,
            source_uri=source_uri,
            raw_object_key=raw_object_key,
            raw_content_sha256=raw_sha256,
            content_sha256=content_sha256,
            retrieved_at=retrieved_at,
            reported_at=reported_at,
            published_at=published_at,
            available_at=available_at,
            revision_at=revision_at,
            parse_status=parse_status,
            validation_status="pending",
        )
        self.session.add(source)
        self.session.flush()
        return source

    def add_issue(
        self, run_id: UUID, source_id: UUID, rule_code: str, severity: str, message: str
    ) -> None:
        self.session.add(
            DataQualityIssue(
                ingestion_run_id=run_id,
                source_record_id=source_id,
                rule_code=rule_code,
                severity=severity,
                message=message,
                status="open",
            )
        )

    def security_by_isin(self, isin: str) -> Security | None:
        return self.session.scalar(select(Security).where(Security.isin == isin))

    def normalize_universe(self, record: UniverseRecord) -> None:
        security = self.security_by_isin(record.isin)
        if security is None:
            company = Company(
                legal_name=record.legal_name,
                display_name=record.display_name,
                sector=record.sector,
                industry=record.industry,
            )
            self.session.add(company)
            self.session.flush()
            security = Security(
                company_id=company.id,
                isin=record.isin,
                security_type=record.security_type,
                status=record.security_status,
            )
            self.session.add(security)
            self.session.flush()
        listing = self.session.scalar(
            select(ExchangeListing).where(
                ExchangeListing.security_id == security.id,
                ExchangeListing.exchange == record.exchange,
                ExchangeListing.symbol == record.symbol,
                ExchangeListing.valid_from == record.valid_from,
            )
        )
        if listing is None:
            self.session.add(
                ExchangeListing(
                    security_id=security.id,
                    exchange=record.exchange,
                    symbol=record.symbol,
                    valid_from=record.valid_from,
                    valid_to=record.valid_to,
                    status=record.listing_status,
                )
            )

    def add_price(
        self,
        *,
        security_id: UUID,
        source_id: UUID,
        trading_date: date,
        interval: str,
        open_price: Decimal,
        high_price: Decimal,
        low_price: Decimal,
        close_price: Decimal,
        volume: int,
        available_at: datetime,
        revision_at: datetime | None,
    ) -> None:
        self.session.add(
            PriceBar(
                security_id=security_id,
                source_record_id=source_id,
                trading_date=trading_date,
                interval=interval,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume,
                available_at=available_at,
                revision_at=revision_at,
            )
        )
