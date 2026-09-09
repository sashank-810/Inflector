"""Persistence operations used by the data-ingestion service."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.providers import FinancialRecord, ProviderMetadata, UniverseRecord
from inflector_database.models import (
    Company,
    DataProvider,
    DataQualityIssue,
    ExchangeListing,
    FinancialFact,
    FinancialFiling,
    FinancialMetricDefinition,
    FiscalPeriod,
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
        raw_payload_reference: str,
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
            raw_payload_reference=raw_payload_reference,
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

    def company_by_legal_name(self, legal_name: str) -> Company | None:
        return self.session.scalar(select(Company).where(Company.legal_name == legal_name))

    def metric_by_code(self, code: str) -> FinancialMetricDefinition | None:
        return self.session.scalar(
            select(FinancialMetricDefinition).where(FinancialMetricDefinition.code == code)
        )

    def ensure_metric_definitions(self, definitions: tuple[tuple[str, str, str, str], ...]) -> None:
        existing = set(self.session.scalars(select(FinancialMetricDefinition.code)))
        for code, statement_kind, unit_category, semantic_type in definitions:
            if code not in existing:
                self.session.add(
                    FinancialMetricDefinition(
                        code=code,
                        statement_kind=statement_kind,
                        unit_category=unit_category,
                        semantic_type=semantic_type,
                    )
                )
        self.session.flush()

    def ensure_period(self, company_id: UUID, record: FinancialRecord) -> FiscalPeriod:
        assert (
            record.period_kind and record.period_start and record.period_end and record.fiscal_year
        )
        period = self.session.scalar(
            select(FiscalPeriod).where(
                FiscalPeriod.company_id == company_id,
                FiscalPeriod.period_kind == record.period_kind,
                FiscalPeriod.period_start == record.period_start,
                FiscalPeriod.period_end == record.period_end,
                FiscalPeriod.fiscal_year == record.fiscal_year,
                FiscalPeriod.fiscal_quarter == record.fiscal_quarter,
                FiscalPeriod.is_ytd == record.is_ytd,
            )
        )
        if period is None:
            period = FiscalPeriod(
                company_id=company_id,
                period_kind=record.period_kind,
                period_start=record.period_start,
                period_end=record.period_end,
                fiscal_year=record.fiscal_year,
                fiscal_quarter=record.fiscal_quarter,
                is_ytd=record.is_ytd,
            )
            self.session.add(period)
            self.session.flush()
        return period

    def ensure_filing(
        self,
        company_id: UUID,
        dataset_id: UUID,
        record: FinancialRecord,
        published_at: datetime | None,
        available_at: datetime,
        revision_at: datetime | None,
    ) -> FinancialFiling:
        assert record.filing_external_id and record.filing_type and record.filing_scope
        filing = self.session.scalar(
            select(FinancialFiling).where(
                FinancialFiling.provider_dataset_id == dataset_id,
                FinancialFiling.external_filing_id == record.filing_external_id,
                FinancialFiling.filing_scope == record.filing_scope,
                FinancialFiling.available_at == available_at,
                FinancialFiling.revision_at == revision_at,
            )
        )
        if filing is None:
            filing = FinancialFiling(
                company_id=company_id,
                provider_dataset_id=dataset_id,
                external_filing_id=record.filing_external_id,
                filing_type=record.filing_type,
                filing_scope=record.filing_scope,
                is_restatement=record.is_restatement,
                published_at=published_at,
                available_at=available_at,
                revision_at=revision_at,
            )
            self.session.add(filing)
            self.session.flush()
        return filing

    def financial_facts(
        self, *, company_id: UUID, period_id: UUID, scope: str, metric_id: UUID
    ) -> list[FinancialFact]:
        return list(
            self.session.scalars(
                select(FinancialFact)
                .join(FinancialFiling)
                .where(
                    FinancialFiling.company_id == company_id,
                    FinancialFact.fiscal_period_id == period_id,
                    FinancialFiling.filing_scope == scope,
                    FinancialFact.metric_definition_id == metric_id,
                )
            )
        )

    def add_financial_fact(
        self,
        *,
        filing_id: UUID,
        period_id: UUID,
        metric_id: UUID,
        source_id: UUID,
        record: FinancialRecord,
        normalized_value: Decimal | None,
        normalized_unit: str | None,
        available_at: datetime,
        revision_at: datetime | None,
    ) -> None:
        assert record.reported_value is not None and record.reported_unit and record.reported_scale
        self.session.add(
            FinancialFact(
                filing_id=filing_id,
                fiscal_period_id=period_id,
                metric_definition_id=metric_id,
                source_record_id=source_id,
                reported_value=record.reported_value,
                reported_unit=record.reported_unit,
                reported_scale=record.reported_scale,
                reported_currency=record.reported_currency,
                normalized_value=normalized_value,
                normalized_unit=normalized_unit,
                available_at=available_at,
                revision_at=revision_at,
            )
        )

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

    def economic_prices(
        self, *, dataset_id: UUID, security_id: UUID, trading_date: date, interval: str
    ) -> list[PriceBar]:
        """Return all immutable revisions of one provider-dataset economic bar."""

        return list(
            self.session.scalars(
                select(PriceBar)
                .join(SourceRecord, PriceBar.source_record_id == SourceRecord.id)
                .where(
                    SourceRecord.provider_dataset_id == dataset_id,
                    PriceBar.security_id == security_id,
                    PriceBar.trading_date == trading_date,
                    PriceBar.interval == interval,
                )
                .order_by(PriceBar.available_at, PriceBar.revision_at, PriceBar.ingested_at)
            )
        )
