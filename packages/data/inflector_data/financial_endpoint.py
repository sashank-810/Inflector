"""Deterministic latest PIT-visible financial-endpoint selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_database.models import FinancialFact, FinancialFiling, FiscalPeriod, SourceRecord


@dataclass(frozen=True, slots=True)
class FinancialEndpointPolicy:
    financial_endpoint_policy_code: str
    policy_version: str
    source_family: str
    supported_period_kinds: tuple[str, ...]
    ordering_fields: tuple[str, ...]
    scope_selection: str
    future_period_handling: str
    ambiguity_handling: str
    completeness_handling: str
    revision_handling: str
    no_endpoint_handling: str
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class FinancialEndpointResult:
    company_id: UUID
    status: str
    filing_scope: str | None
    fiscal_year: int | None
    fiscal_quarter: int | None
    period_end: date | None
    knowledge_cutoff: datetime
    available_at: datetime | None
    filing_external_ids: tuple[str, ...]
    source_record_ids: tuple[UUID, ...]
    excluded_future_candidates: int
    policy_code: str
    policy_checksum_sha256: str


def load_financial_endpoint_policy(path: Path) -> FinancialEndpointPolicy:
    """Load the narrow, immutable endpoint selection contract."""

    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("financial endpoint policy is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("financial endpoint policy root must be an object")
    expected = {
        "financial_endpoint_policy_code",
        "policy_version",
        "source_family",
        "supported_period_kinds",
        "ordering_fields",
        "scope_selection",
        "future_period_handling",
        "ambiguity_handling",
        "completeness_handling",
        "revision_handling",
        "no_endpoint_handling",
    }
    if set(value) != expected:
        raise ValueError("financial endpoint policy fields are incomplete or unsupported")
    kinds = _strings(value, "supported_period_kinds")
    if kinds != ("quarter", "half_year", "nine_month", "annual"):
        raise ValueError("financial endpoint period kinds must preserve reported NSE periods")
    ordering = _strings(value, "ordering_fields")
    if ordering != ("period_end", "fiscal_year", "fiscal_quarter"):
        raise ValueError("financial endpoint ordering must use authoritative fiscal metadata")
    exact = {
        "scope_selection": "research_profile_priority_first",
        "future_period_handling": "exclude",
        "ambiguity_handling": "fail_closed",
        "completeness_handling": "ignored",
        "revision_handling": "existing_pit_reader",
        "no_endpoint_handling": "issuer_unavailable",
    }
    for field, expected_value in exact.items():
        if _text(value, field) != expected_value:
            raise ValueError(f"financial endpoint policy {field} is unsupported")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return FinancialEndpointPolicy(
        financial_endpoint_policy_code=_text(value, "financial_endpoint_policy_code"),
        policy_version=_text(value, "policy_version"),
        source_family=_text(value, "source_family"),
        supported_period_kinds=kinds,
        ordering_fields=ordering,
        scope_selection=exact["scope_selection"],
        future_period_handling=exact["future_period_handling"],
        ambiguity_handling=exact["ambiguity_handling"],
        completeness_handling=exact["completeness_handling"],
        revision_handling=exact["revision_handling"],
        no_endpoint_handling=exact["no_endpoint_handling"],
        checksum_sha256=sha256(canonical).hexdigest(),
    )


class FinancialEndpointResolver:
    """Resolve chronology only; never inspect values, coverage, components, or scores."""

    def __init__(self, session: Session, policy: FinancialEndpointPolicy) -> None:
        self._session = session
        self._policy = policy

    def resolve(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope_priority: tuple[str, ...],
        knowledge_cutoff: datetime,
    ) -> FinancialEndpointResult:
        cutoff = _aware_utc(knowledge_cutoff)
        rows = list(
            self._session.execute(
                select(FinancialFact, FiscalPeriod, FinancialFiling, SourceRecord)
                .join(FiscalPeriod, FinancialFact.fiscal_period_id == FiscalPeriod.id)
                .join(FinancialFiling, FinancialFact.filing_id == FinancialFiling.id)
                .join(SourceRecord, FinancialFact.source_record_id == SourceRecord.id)
                .where(
                    FinancialFiling.provider_dataset_id == provider_dataset_id,
                    FinancialFiling.company_id == company_id,
                    FinancialFiling.available_at <= cutoff,
                    FiscalPeriod.company_id == company_id,
                    FinancialFact.available_at <= cutoff,
                    SourceRecord.provider_dataset_id == provider_dataset_id,
                    SourceRecord.validation_status == "accepted",
                )
            ).tuples()
        )
        future_count = sum(1 for _, period, _, _ in rows if period.period_end > cutoff.date())
        eligible = [
            row
            for row in rows
            if row[1].period_kind in self._policy.supported_period_kinds
            and row[1].fiscal_quarter in {1, 2, 3, 4}
            and row[1].period_end <= cutoff.date()
        ]
        for scope in filing_scope_priority:
            scoped = [row for row in eligible if row[2].filing_scope == scope]
            if not scoped:
                continue
            result = self._select_scope(
                company_id=company_id,
                scope=scope,
                rows=scoped,
                cutoff=cutoff,
                future_count=future_count,
            )
            return result
        return self._result(
            company_id=company_id,
            status="no_pit_visible_financial_endpoint",
            cutoff=cutoff,
            future_count=future_count,
        )

    def _select_scope(
        self,
        *,
        company_id: UUID,
        scope: str,
        rows: list[tuple[FinancialFact, FiscalPeriod, FinancialFiling, SourceRecord]],
        cutoff: datetime,
        future_count: int,
    ) -> FinancialEndpointResult:
        latest_period_end = max(period.period_end for _, period, _, _ in rows)
        latest_rows = [row for row in rows if row[1].period_end == latest_period_end]
        coordinates = {(row[1].fiscal_year, row[1].fiscal_quarter) for row in latest_rows}
        if len(coordinates) != 1:
            return self._result(
                company_id=company_id,
                status="ambiguous_financial_endpoint",
                cutoff=cutoff,
                future_count=future_count,
            )
        fiscal_year, fiscal_quarter = next(iter(coordinates))
        assert fiscal_quarter is not None
        coordinate_ends = {
            period.period_end
            for _, period, _, _ in rows
            if (period.fiscal_year, period.fiscal_quarter) == (fiscal_year, fiscal_quarter)
        }
        if len(coordinate_ends) != 1:
            return self._result(
                company_id=company_id,
                status="ambiguous_financial_endpoint",
                cutoff=cutoff,
                future_count=future_count,
            )
        endpoint_rows = [
            row
            for row in latest_rows
            if (row[1].fiscal_year, row[1].fiscal_quarter) == (fiscal_year, fiscal_quarter)
        ]
        available_at = min(_as_utc(fact.available_at) for fact, _, _, _ in endpoint_rows)
        return FinancialEndpointResult(
            company_id=company_id,
            status="selected",
            filing_scope=scope,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            period_end=latest_period_end,
            knowledge_cutoff=cutoff,
            available_at=available_at,
            filing_external_ids=tuple(
                sorted({filing.external_filing_id for _, _, filing, _ in endpoint_rows})
            ),
            source_record_ids=tuple(
                sorted({source.id for _, _, _, source in endpoint_rows}, key=str)
            ),
            excluded_future_candidates=future_count,
            policy_code=self._policy.financial_endpoint_policy_code,
            policy_checksum_sha256=self._policy.checksum_sha256,
        )

    def _result(
        self,
        *,
        company_id: UUID,
        status: str,
        cutoff: datetime,
        future_count: int,
    ) -> FinancialEndpointResult:
        return FinancialEndpointResult(
            company_id=company_id,
            status=status,
            filing_scope=None,
            fiscal_year=None,
            fiscal_quarter=None,
            period_end=None,
            knowledge_cutoff=cutoff,
            available_at=None,
            filing_external_ids=(),
            source_record_ids=(),
            excluded_future_candidates=future_count,
            policy_code=self._policy.financial_endpoint_policy_code,
            policy_checksum_sha256=self._policy.checksum_sha256,
        )


def endpoint_metadata(result: FinancialEndpointResult) -> dict[str, object]:
    return {
        "financial_endpoint_status": result.status,
        "selected_fiscal_year": result.fiscal_year,
        "selected_fiscal_quarter": result.fiscal_quarter,
        "selected_filing_scope": result.filing_scope,
        "selected_period_end": result.period_end,
        "financial_endpoint_available_at": result.available_at,
        "financial_endpoint_knowledge_cutoff": result.knowledge_cutoff,
        "financial_endpoint_policy": result.policy_code,
        "financial_endpoint_policy_checksum": result.policy_checksum_sha256,
        "financial_endpoint_filing_ids": list(result.filing_external_ids),
        "financial_endpoint_source_record_ids": list(result.source_record_ids),
        "excluded_future_financial_endpoints": result.excluded_future_candidates,
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("knowledge_cutoff must be timezone-aware")
    return value.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _text(value: dict[str, Any], field: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip() or item != item.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return item


def _strings(value: dict[str, Any], field: str) -> tuple[str, ...]:
    item = value.get(field)
    if not isinstance(item, list) or not item:
        raise ValueError(f"{field} must be a non-empty list")
    result = tuple(entry for entry in item if isinstance(entry, str) and entry.strip() == entry)
    if len(result) != len(item) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate values")
    return result
