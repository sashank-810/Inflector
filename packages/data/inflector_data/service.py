"""Single provider-to-canonical-record ingestion orchestration path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from inflector_core.providers import (
    IngestionEnvelope,
    MarketBarRecord,
    MarketDataProvider,
    ProviderBatch,
    UniverseProvider,
    UniverseRecord,
)
from inflector_data.archive import ArchivedRawObject, RawObjectStore
from inflector_data.validation import ValidationIssue, validate_price, validate_universe
from inflector_database.ingestion_repository import IngestionRepository
from inflector_database.models import IngestionRun, ProviderDataset, SourceRecord


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Stable outcome reported by the CLI and tests."""

    run_id: UUID
    status: str
    records_received: int
    records_accepted: int
    records_quarantined: int
    records_duplicated: int


class IngestionService:
    """Archives raw input before validating and normalizing any observation."""

    def __init__(self, session: Session, archive: RawObjectStore) -> None:
        self._session = session
        self._archive = archive
        self._repository = IngestionRepository(session)

    def ingest_universe(self, provider: UniverseProvider) -> IngestionResult:
        return self._ingest_universe(provider.fetch_universe())

    def ingest_market_data(self, provider: MarketDataProvider) -> IngestionResult:
        return self._ingest_market(provider.fetch_market_data())

    def _start(
        self, batch: ProviderBatch[UniverseRecord] | ProviderBatch[MarketBarRecord]
    ) -> tuple[ProviderDataset, IngestionRun, ArchivedRawObject]:
        dataset = self._repository.ensure_dataset(batch.provider)
        run = self._repository.create_run(dataset.id, datetime.now(UTC), batch.cursor)
        self._session.commit()
        return dataset, run, self._archive.put(batch.raw_payload)

    def _finish(
        self, run_id: UUID, accepted: int, quarantined: int, duplicated: int
    ) -> IngestionResult:
        run = self._session.get(IngestionRun, run_id)
        assert run is not None
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.records_received = accepted + quarantined + duplicated
        run.records_accepted = accepted
        run.records_quarantined = quarantined
        run.records_duplicated = duplicated
        self._session.commit()
        return IngestionResult(
            run_id, run.status, run.records_received, accepted, quarantined, duplicated
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
        envelope: IngestionEnvelope[UniverseRecord] | IngestionEnvelope[MarketBarRecord],
        dataset_id: UUID,
        run_id: UUID,
        raw_key: str,
        raw_sha256: str,
        parse_status: str,
    ):
        return self._repository.create_source(
            run_id=run_id,
            dataset_id=dataset_id,
            external_id=envelope.external_record_id,
            source_uri=envelope.source_uri,
            raw_object_key=raw_key,
            raw_sha256=raw_sha256,
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
        accepted = quarantined = duplicated = 0
        for envelope in batch.records:
            if self._repository.source_exists(
                dataset.id, envelope.external_record_id, envelope.content_sha256
            ):
                duplicated += 1
                continue
            source = self._source(
                envelope, dataset.id, run.id, raw.object_key, raw.content_sha256, "parsed"
            )
            issues = validate_universe(envelope.record)
            if issues:
                self._quarantine(run.id, source.id, issues)
                quarantined += 1
                continue
            source.validation_status = "accepted"
            self._repository.normalize_universe(envelope.record)
            accepted += 1
        return self._finish(run.id, accepted, quarantined, duplicated)

    def _ingest_market(self, batch: ProviderBatch[MarketBarRecord]) -> IngestionResult:
        dataset, run, raw = self._start(batch)
        accepted = quarantined = duplicated = 0
        for envelope in batch.records:
            if self._repository.source_exists(
                dataset.id, envelope.external_record_id, envelope.content_sha256
            ):
                duplicated += 1
                continue
            source = self._source(
                envelope,
                dataset.id,
                run.id,
                raw.object_key,
                raw.content_sha256,
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
                quarantined += 1
                continue
            record = envelope.record
            source.validation_status = "accepted"
            assert (
                security is not None
                and record.trading_date is not None
                and record.interval is not None
            )
            assert (
                record.open_price is not None
                and record.high_price is not None
                and record.low_price is not None
            )
            assert (
                record.close_price is not None
                and record.volume is not None
                and envelope.available_at is not None
            )
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
            accepted += 1
        return self._finish(run.id, accepted, quarantined, duplicated)
