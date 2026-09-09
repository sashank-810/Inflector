"""Single provider-to-canonical-record ingestion orchestration path."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_core.providers import (
    FinancialRecord,
    FinancialsProvider,
    IngestionEnvelope,
    MarketBarRecord,
    MarketDataProvider,
    ProviderBatch,
    UniverseProvider,
    UniverseRecord,
)
from inflector_data.archive import ArchivedRawObject, RawObjectStore
from inflector_data.financials import METRICS, normalize_financial, validate_financial
from inflector_data.validation import ValidationIssue, validate_price, validate_universe
from inflector_database.ingestion_repository import IngestionRepository
from inflector_database.models import (
    FinancialFact,
    IngestionRun,
    PriceBar,
    ProviderDataset,
    SourceRecord,
)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Stable outcome reported by the CLI and tests."""

    run_id: UUID
    status: str
    records_received: int
    records_accepted: int
    records_quarantined: int
    records_duplicated: int


@dataclass(slots=True)
class _Counters:
    """Known record outcomes retained even when the active transaction rolls back."""

    received: int
    accepted: int = 0
    quarantined: int = 0
    duplicated: int = 0


class IngestionService:
    """Archive first, then atomically normalize or terminally fail an audit run."""

    def __init__(self, session: Session, archive: RawObjectStore) -> None:
        self._session = session
        self._archive = archive
        self._repository = IngestionRepository(session)

    def ingest_universe(self, provider: UniverseProvider) -> IngestionResult:
        return self._ingest_universe(provider.fetch_universe())

    def ingest_market_data(self, provider: MarketDataProvider) -> IngestionResult:
        return self._ingest_market(provider.fetch_market_data())

    def ingest_financials(self, provider: FinancialsProvider) -> IngestionResult:
        return self._ingest_financials(provider.fetch_financials())

    def _start(
        self,
        batch: ProviderBatch[UniverseRecord]
        | ProviderBatch[MarketBarRecord]
        | ProviderBatch[FinancialRecord],
    ) -> tuple[ProviderDataset, IngestionRun, ArchivedRawObject]:
        """Archive durably before committing the audit run that references its processing."""

        raw = self._archive.put(batch.raw_payload)
        dataset = self._repository.ensure_dataset(batch.provider)
        run = self._repository.create_run(dataset.id, datetime.now(UTC), batch.cursor)
        self._session.commit()
        return dataset, run, raw

    def _completed(self, run_id: UUID, counters: _Counters) -> IngestionResult:
        run = self._session.get(IngestionRun, run_id)
        assert run is not None
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        self._apply_counters(run, counters)
        self._session.commit()
        return self._result(run, counters)

    def _failed(self, run_id: UUID, counters: _Counters, error: Exception) -> None:
        """Rollback facts, then independently commit an auditable terminal run."""

        self._session.rollback()
        run = self._session.get(IngestionRun, run_id)
        assert run is not None
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.error_message = f"{type(error).__name__}: {error}"
        # The active normalization transaction was rolled back: no accepted or
        # quarantined outcomes are durable, even if they were observed before
        # the exception. Received/duplicate counts remain knowable.
        durable_counters = _Counters(counters.received, duplicated=counters.duplicated)
        self._apply_counters(run, durable_counters)
        self._session.commit()

    @staticmethod
    def _apply_counters(run: IngestionRun, counters: _Counters) -> None:
        run.records_received = counters.received
        run.records_accepted = counters.accepted
        run.records_quarantined = counters.quarantined
        run.records_duplicated = counters.duplicated

    @staticmethod
    def _result(run: IngestionRun, counters: _Counters) -> IngestionResult:
        return IngestionResult(
            run.id,
            run.status,
            counters.received,
            counters.accepted,
            counters.quarantined,
            counters.duplicated,
        )

    def _quarantine(self, run_id: UUID, source_id: UUID, issues: list[ValidationIssue]) -> None:
        source = self._session.get(SourceRecord, source_id)
        assert source is not None
        source.validation_status = "quarantined"
        for issue in issues:
            self._repository.add_issue(
                run_id, source_id, issue.rule_code, issue.severity, issue.message
            )

    def _source(
        self,
        envelope: IngestionEnvelope[UniverseRecord]
        | IngestionEnvelope[MarketBarRecord]
        | IngestionEnvelope[FinancialRecord],
        dataset_id: UUID,
        run_id: UUID,
        raw: ArchivedRawObject,
        parse_status: str,
    ) -> SourceRecord:
        return self._repository.create_source(
            run_id=run_id,
            dataset_id=dataset_id,
            external_id=envelope.external_record_id,
            source_uri=envelope.source_uri,
            raw_object_key=raw.object_key,
            raw_payload_reference=envelope.raw_payload_reference,
            raw_sha256=raw.content_sha256,
            content_sha256=envelope.content_sha256,
            retrieved_at=envelope.retrieved_at,
            reported_at=envelope.reported_at,
            published_at=envelope.published_at,
            available_at=envelope.available_at,
            revision_at=envelope.revision_at,
            parse_status=parse_status,
        )

    def _ingest_universe(self, batch: ProviderBatch[UniverseRecord]) -> IngestionResult:
        dataset, run, raw = self._start(batch)
        counters = _Counters(received=len(batch.records))
        try:
            for envelope in batch.records:
                if self._repository.source_exists(
                    dataset.id, envelope.external_record_id, envelope.content_sha256
                ):
                    counters.duplicated += 1
                    continue
                source = self._source(envelope, dataset.id, run.id, raw, "parsed")
                issues = validate_universe(envelope.record)
                if issues:
                    self._quarantine(run.id, source.id, issues)
                    counters.quarantined += 1
                    continue
                source.validation_status = "accepted"
                self._repository.normalize_universe(envelope.record)
                counters.accepted += 1
            return self._completed(run.id, counters)
        except Exception as error:
            self._failed(run.id, counters, error)
            raise

    def _ingest_market(self, batch: ProviderBatch[MarketBarRecord]) -> IngestionResult:
        dataset, run, raw = self._start(batch)
        counters = _Counters(received=len(batch.records))
        try:
            for envelope in batch.records:
                if self._repository.source_exists(
                    dataset.id, envelope.external_record_id, envelope.content_sha256
                ):
                    counters.duplicated += 1
                    continue
                source = self._source(
                    envelope,
                    dataset.id,
                    run.id,
                    raw,
                    "failed" if envelope.record.parse_errors else "parsed",
                )
                issues = validate_price(envelope.record)
                security = (
                    self._repository.security_by_isin(envelope.record.security_isin)
                    if envelope.record.security_isin
                    else None
                )
                if security is None and not any(
                    issue.rule_code == "missing_security_identity" for issue in issues
                ):
                    issues.append(
                        ValidationIssue(
                            "unknown_security", "security ISIN is not in the canonical universe"
                        )
                    )
                if envelope.available_at is None:
                    issues.append(
                        ValidationIssue(
                            "missing_available_at", "available_at is required for price facts"
                        )
                    )
                if issues:
                    self._quarantine(run.id, source.id, issues)
                    counters.quarantined += 1
                    continue
                record = envelope.record
                assert security is not None and record.trading_date is not None
                assert record.interval is not None and envelope.available_at is not None
                assert record.open_price is not None and record.high_price is not None
                assert record.low_price is not None and record.close_price is not None
                assert record.volume is not None
                existing = self._repository.economic_prices(
                    dataset_id=dataset.id,
                    security_id=security.id,
                    trading_date=record.trading_date,
                    interval=record.interval,
                )
                if self._equivalent(existing, record):
                    source.validation_status = "duplicate_economic"
                    counters.duplicated += 1
                    continue
                if existing and not self._is_later_revision(existing, envelope):
                    self._quarantine(
                        run.id,
                        source.id,
                        [
                            ValidationIssue(
                                "ambiguous_price_revision",
                                (
                                    "changed values lack a strictly later availability "
                                    "or revision time"
                                ),
                            )
                        ],
                    )
                    counters.quarantined += 1
                    continue
                source.validation_status = "accepted"
                self._repository.add_price(
                    security_id=security.id,
                    source_id=source.id,
                    trading_date=record.trading_date,
                    interval=record.interval,
                    open_price=record.open_price,
                    high_price=record.high_price,
                    low_price=record.low_price,
                    close_price=record.close_price,
                    volume=record.volume,
                    available_at=envelope.available_at,
                    revision_at=envelope.revision_at,
                )
                counters.accepted += 1
            return self._completed(run.id, counters)
        except Exception as error:
            self._failed(run.id, counters, error)
            raise

    def _ingest_financials(self, batch: ProviderBatch[FinancialRecord]) -> IngestionResult:
        dataset, run, raw = self._start(batch)
        counters = _Counters(received=len(batch.records))
        try:
            self._repository.ensure_metric_definitions(METRICS)
            for envelope in batch.records:
                if self._repository.source_exists(
                    dataset.id, envelope.external_record_id, envelope.content_sha256
                ):
                    counters.duplicated += 1
                    continue
                source = self._source(
                    envelope,
                    dataset.id,
                    run.id,
                    raw,
                    "failed" if envelope.record.parse_errors else "parsed",
                )
                issues = validate_financial(envelope.record)
                company = (
                    self._repository.company_by_legal_name(envelope.record.company_legal_name)
                    if envelope.record.company_legal_name
                    else None
                )
                metric = (
                    self._repository.metric_by_code(envelope.record.metric_code)
                    if envelope.record.metric_code
                    else None
                )
                if company is None and not any(
                    issue.rule_code == "missing_company_identity" for issue in issues
                ):
                    issues.append(
                        ValidationIssue(
                            "unknown_company", "company is not in the canonical universe"
                        )
                    )
                if metric is None and not any(
                    issue.rule_code == "missing_metric_code" for issue in issues
                ):
                    issues.append(
                        ValidationIssue(
                            "unknown_metric", "metric code is not in the controlled dictionary"
                        )
                    )
                if envelope.available_at is None:
                    issues.append(
                        ValidationIssue(
                            "missing_available_at", "available_at is required for financial facts"
                        )
                    )
                if issues:
                    self._quarantine(run.id, source.id, issues)
                    counters.quarantined += 1
                    continue
                record = envelope.record
                assert (
                    company is not None and metric is not None and envelope.available_at is not None
                )
                period = self._repository.ensure_period(company.id, record)
                filing = self._repository.ensure_filing(
                    company.id,
                    dataset.id,
                    record,
                    envelope.published_at,
                    envelope.available_at,
                    envelope.revision_at,
                )
                normalized = normalize_financial(record)
                existing = self._repository.financial_facts(
                    company_id=company.id,
                    period_id=period.id,
                    scope=record.filing_scope or "",
                    metric_id=metric.id,
                )
                if self._financial_equivalent(existing, record, normalized.value):
                    source.validation_status = "duplicate_economic"
                    counters.duplicated += 1
                    continue
                if existing and not self._financial_later(existing, envelope):
                    self._quarantine(
                        run.id,
                        source.id,
                        [
                            ValidationIssue(
                                "ambiguous_financial_revision",
                                (
                                    "changed financial value lacks a strictly later availability "
                                    "or revision time"
                                ),
                            )
                        ],
                    )
                    counters.quarantined += 1
                    continue
                source.validation_status = "accepted"
                self._repository.add_financial_fact(
                    filing_id=filing.id,
                    period_id=period.id,
                    metric_id=metric.id,
                    source_id=source.id,
                    record=record,
                    normalized_value=normalized.value,
                    normalized_unit=normalized.unit,
                    available_at=envelope.available_at,
                    revision_at=envelope.revision_at,
                )
                counters.accepted += 1
            return self._completed(run.id, counters)
        except Exception as error:
            self._failed(run.id, counters, error)
            raise

    @staticmethod
    def _financial_equivalent(
        existing: Sequence[FinancialFact], record: FinancialRecord, normalized_value: object
    ) -> bool:
        return any(
            (
                fact.normalized_value == normalized_value
                if normalized_value is not None
                else (
                    fact.reported_value == record.reported_value
                    and fact.reported_unit == record.reported_unit
                    and fact.reported_scale == record.reported_scale
                    and fact.reported_currency == record.reported_currency
                )
            )
            and fact.reported_unit == record.reported_unit
            for fact in existing
        )

    @staticmethod
    def _financial_later(
        existing: Sequence[FinancialFact], envelope: IngestionEnvelope[FinancialRecord]
    ) -> bool:
        assert envelope.available_at is not None
        new_order = (envelope.available_at, envelope.revision_at or envelope.available_at)
        previous_order = max(
            (
                IngestionService._utc(getattr(fact, "available_at")),
                IngestionService._utc(
                    getattr(fact, "revision_at") or getattr(fact, "available_at")
                ),
            )
            for fact in existing
        )
        return new_order > previous_order

    @staticmethod
    def _equivalent(existing: list[PriceBar], record: MarketBarRecord) -> bool:
        """Economic equality ignores provider-local IDs and source metadata."""

        return any(
            (
                price.open_price,
                price.high_price,
                price.low_price,
                price.close_price,
                price.volume,
            )
            == (
                record.open_price,
                record.high_price,
                record.low_price,
                record.close_price,
                record.volume,
            )
            for price in existing
        )

    @staticmethod
    def _is_later_revision(
        existing: list[PriceBar], envelope: IngestionEnvelope[MarketBarRecord]
    ) -> bool:
        """Require a strict provider-supplied ordering for changed economic values."""

        assert envelope.available_at is not None
        new_order = (envelope.available_at, envelope.revision_at or envelope.available_at)
        previous_order = max(
            (
                IngestionService._utc(price.available_at),
                IngestionService._utc(price.revision_at or price.available_at),
            )
            for price in existing
        )
        return new_order > previous_order

    @staticmethod
    def _utc(value: datetime) -> datetime:
        """SQLite test storage returns naive timestamps; database contract defines them as UTC."""

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
