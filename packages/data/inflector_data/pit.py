"""Deterministic point-in-time reads over accepted immutable financial facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_database.models import (
    FinancialFact,
    FinancialFiling,
    FinancialMetricDefinition,
    FiscalPeriod,
    SourceRecord,
)


@dataclass(frozen=True, slots=True)
class FiscalPeriodFilter:
    """Optional exact stored-period semantics for a PIT series query."""

    period_kind: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    is_ytd: bool | None = None

    def matches(self, period: FiscalPeriod) -> bool:
        """Return whether an already-stored fiscal period matches every supplied field."""

        return all(
            expected is None or actual == expected
            for actual, expected in (
                (period.period_kind, self.period_kind),
                (period.period_start, self.period_start),
                (period.period_end, self.period_end),
                (period.fiscal_year, self.fiscal_year),
                (period.fiscal_quarter, self.fiscal_quarter),
                (period.is_ytd, self.is_ytd),
            )
        )


@dataclass(frozen=True, slots=True)
class FinancialPeriodView:
    """Stored fiscal-period identity returned without calendar-quarter inference."""

    id: UUID
    period_kind: str
    period_start: date
    period_end: date
    fiscal_year: int
    fiscal_quarter: int | None
    is_ytd: bool


@dataclass(frozen=True, slots=True)
class FinancialFilingView:
    """Immutable filing lineage associated with one selected fact."""

    id: UUID
    external_filing_id: str
    filing_type: str
    filing_scope: str
    is_restatement: bool
    published_at: datetime | None
    available_at: datetime
    revision_at: datetime | None


@dataclass(frozen=True, slots=True)
class SourceRecordView:
    """Raw-source provenance for a selected canonical financial fact."""

    id: UUID
    external_record_id: str
    source_uri: str
    raw_object_key: str
    raw_payload_reference: str | None
    content_sha256: str
    validation_status: str


@dataclass(frozen=True, slots=True)
class PointInTimeFinancialFact:
    """A financial observation selected by a declared knowledge cutoff."""

    id: UUID
    provider_dataset_id: UUID
    company_id: UUID
    metric_code: str
    reported_value: Decimal
    reported_unit: str
    reported_scale: str
    reported_currency: str | None
    normalized_value: Decimal | None
    normalized_unit: str | None
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    fiscal_period: FinancialPeriodView
    filing: FinancialFilingView
    source_record: SourceRecordView


_FactRow = tuple[
    FinancialFact,
    FiscalPeriod,
    FinancialFiling,
    FinancialMetricDefinition,
    SourceRecord,
]


class PointInTimeFinancialReader:
    """Read accepted financial evidence without reconciling providers or scopes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def financial_fact_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        fiscal_period_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
    ) -> PointInTimeFinancialFact | None:
        """Select one economic fact using only evidence available at ``as_of``."""

        cutoff = self._knowledge_cutoff(as_of)
        rows = self._rows_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=cutoff,
            fiscal_period_id=fiscal_period_id,
        )
        return self._view(rows[0]) if rows else None

    def financial_series_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
        period_filter: FiscalPeriodFilter | None = None,
    ) -> list[PointInTimeFinancialFact]:
        """Return one revision per stored fiscal period, as known at ``as_of``.

        Results are ordered by period end, period start, then period kind. The
        ordering preserves reported period identity; it does not turn YTD and
        quarterly facts into a derived series.
        """

        cutoff = self._knowledge_cutoff(as_of)
        rows = self._rows_as_of(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_code=metric_code,
            as_of=cutoff,
        )
        selected_by_period: dict[UUID, _FactRow] = {}
        for row in rows:
            fact, period, *_ = row
            if period_filter is not None and not period_filter.matches(period):
                continue
            selected_by_period.setdefault(fact.fiscal_period_id, row)
        selected = [self._view(row) for row in selected_by_period.values()]
        return sorted(
            selected,
            key=lambda fact: (
                fact.fiscal_period.period_end,
                fact.fiscal_period.period_start,
                fact.fiscal_period.period_kind,
                str(fact.fiscal_period.id),
            ),
        )

    def _rows_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_code: str,
        as_of: datetime,
        fiscal_period_id: UUID | None = None,
    ) -> list[_FactRow]:
        """Fetch eligible facts ordered newest-first within each economic identity."""

        statement = (
            select(
                FinancialFact,
                FiscalPeriod,
                FinancialFiling,
                FinancialMetricDefinition,
                SourceRecord,
            )
            .join(FiscalPeriod, FinancialFact.fiscal_period_id == FiscalPeriod.id)
            .join(FinancialFiling, FinancialFact.filing_id == FinancialFiling.id)
            .join(
                FinancialMetricDefinition,
                FinancialFact.metric_definition_id == FinancialMetricDefinition.id,
            )
            .join(SourceRecord, FinancialFact.source_record_id == SourceRecord.id)
            .where(
                FinancialFiling.provider_dataset_id == provider_dataset_id,
                FinancialFiling.company_id == company_id,
                FinancialFiling.filing_scope == filing_scope,
                FinancialMetricDefinition.code == metric_code,
                FinancialFact.available_at <= as_of,
                SourceRecord.validation_status == "accepted",
            )
            .order_by(
                FinancialFact.available_at.desc(),
                func.coalesce(FinancialFact.revision_at, FinancialFact.available_at).desc(),
                FinancialFact.ingested_at.desc(),
                FinancialFact.id.desc(),
            )
        )
        if fiscal_period_id is not None:
            statement = statement.where(FinancialFact.fiscal_period_id == fiscal_period_id)
        return list(self._session.execute(statement).tuples())

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        """Reject ambiguous timestamps and normalize explicit offsets to UTC."""

        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)

    @staticmethod
    def _view(row: _FactRow) -> PointInTimeFinancialFact:
        fact, period, filing, metric, source = row
        return PointInTimeFinancialFact(
            id=fact.id,
            provider_dataset_id=filing.provider_dataset_id,
            company_id=filing.company_id,
            metric_code=metric.code,
            reported_value=fact.reported_value,
            reported_unit=fact.reported_unit,
            reported_scale=fact.reported_scale,
            reported_currency=fact.reported_currency,
            normalized_value=fact.normalized_value,
            normalized_unit=fact.normalized_unit,
            available_at=PointInTimeFinancialReader._as_utc(fact.available_at),
            revision_at=PointInTimeFinancialReader._as_utc_or_none(fact.revision_at),
            ingested_at=PointInTimeFinancialReader._as_utc(fact.ingested_at),
            fiscal_period=FinancialPeriodView(
                id=period.id,
                period_kind=period.period_kind,
                period_start=period.period_start,
                period_end=period.period_end,
                fiscal_year=period.fiscal_year,
                fiscal_quarter=period.fiscal_quarter,
                is_ytd=period.is_ytd,
            ),
            filing=FinancialFilingView(
                id=filing.id,
                external_filing_id=filing.external_filing_id,
                filing_type=filing.filing_type,
                filing_scope=filing.filing_scope,
                is_restatement=filing.is_restatement,
                published_at=PointInTimeFinancialReader._as_utc_or_none(filing.published_at),
                available_at=PointInTimeFinancialReader._as_utc(filing.available_at),
                revision_at=PointInTimeFinancialReader._as_utc_or_none(filing.revision_at),
            ),
            source_record=SourceRecordView(
                id=source.id,
                external_record_id=source.external_record_id,
                source_uri=source.source_uri,
                raw_object_key=source.raw_object_key,
                raw_payload_reference=source.raw_payload_reference,
                content_sha256=source.content_sha256,
                validation_status=source.validation_status,
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize SQLite's timezone-less test values without guessing their meaning.

        PostgreSQL returns UTC-aware timestamps. SQLite does not retain timezone
        metadata for its `DateTime(timezone=True)` test representation, so those
        values are interpreted as UTC because ingestion accepts UTC only.
        """

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _as_utc_or_none(value: datetime | None) -> datetime | None:
        return None if value is None else PointInTimeFinancialReader._as_utc(value)
