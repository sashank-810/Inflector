"""Explicit-version reads for quantitative business-event derivations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_FACT_KINDS,
    BUSINESS_EVENT_QUANT_FACT_ORDER,
    EVENT_FACT_COMPATIBILITY,
)
from inflector_data.business_event_pit import PointInTimeBusinessEvent
from inflector_database.models import (
    BusinessEvent,
    BusinessEventEvidence,
    BusinessEventQuantitativeDerivation,
    BusinessEventQuantitativeFact,
)


class BusinessEventQuantitativeReadIntegrityError(ValueError):
    """Raised when persisted quantitative output is not source-coherent."""


@dataclass(frozen=True, slots=True)
class BusinessEventQuantitativeFactView:
    id: UUID
    business_event_evidence_id: UUID
    fact_code: str
    fact_kind: str
    rule_code: str
    rule_semantic_version: str
    start_offset: int
    end_offset: int
    raw_text: str
    raw_text_sha256: str
    reported_value: Decimal | None
    reported_scale: str | None
    reported_unit: str | None
    reported_currency: str | None
    normalized_value: Decimal | None
    normalized_unit: str | None
    date_value: date | None
    source_available_at: datetime
    warnings: tuple[str, ...]
    fact_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class PointInTimeBusinessEventQuantitativeDerivation:
    id: UUID
    event: PointInTimeBusinessEvent
    ruleset_code: str
    ruleset_semantic_version: str
    source_available_at: datetime
    available_fact_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    derivation_fingerprint_sha256: str
    derived_at: datetime
    facts: tuple[BusinessEventQuantitativeFactView, ...]


class PointInTimeBusinessEventQuantitativeReader:
    """Read one exact ruleset outcome for an already PIT-selected event."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def facts_for_event(
        self,
        *,
        event: PointInTimeBusinessEvent,
        ruleset_code: str,
        ruleset_semantic_version: str,
    ) -> PointInTimeBusinessEventQuantitativeDerivation | None:
        if not ruleset_code or not ruleset_semantic_version:
            raise ValueError("quantitative ruleset identity must be non-empty")
        persisted_event = self._session.get(BusinessEvent, event.id)
        if (
            persisted_event is None
            or persisted_event.detection_fingerprint_sha256
            != event.detection_fingerprint_sha256
            or persisted_event.event_type != event.event_type
            or _as_utc(persisted_event.source_available_at) != event.source_available_at
        ):
            raise BusinessEventQuantitativeReadIntegrityError(
                "business event does not match persisted identity"
            )
        derivation = self._session.scalar(
            select(BusinessEventQuantitativeDerivation).where(
                BusinessEventQuantitativeDerivation.business_event_id == event.id,
                BusinessEventQuantitativeDerivation.ruleset_code == ruleset_code,
                BusinessEventQuantitativeDerivation.ruleset_semantic_version
                == ruleset_semantic_version,
            )
        )
        if derivation is None:
            return None
        if _as_utc(derivation.source_available_at) != event.source_available_at:
            raise BusinessEventQuantitativeReadIntegrityError(
                "quantitative derivation knowledge time mismatch"
            )
        evidence = {value.id: value for value in event.evidence}
        facts = list(
            self._session.scalars(
                select(BusinessEventQuantitativeFact).where(
                    BusinessEventQuantitativeFact.derivation_id == derivation.id
                )
            )
        )
        rank = {code: index for index, code in enumerate(BUSINESS_EVENT_QUANT_FACT_ORDER)}
        facts.sort(
            key=lambda value: (
                rank.get(value.fact_code, 99),
                str(value.business_event_evidence_id),
                value.start_offset,
                value.end_offset,
                value.rule_code,
                value.fact_fingerprint_sha256,
            )
        )
        views: list[BusinessEventQuantitativeFactView] = []
        for fact in facts:
            source = evidence.get(fact.business_event_evidence_id)
            persisted_evidence = self._session.get(
                BusinessEventEvidence, fact.business_event_evidence_id
            )
            if (
                source is None
                or persisted_evidence is None
                or persisted_evidence.business_event_id != event.id
                or fact.fact_code not in EVENT_FACT_COMPATIBILITY[event.event_type]
                or fact.fact_kind not in BUSINESS_EVENT_QUANT_FACT_KINDS
                or not source.start_offset
                <= fact.start_offset
                < fact.end_offset
                <= source.end_offset
                or source.excerpt_text[
                    fact.start_offset - source.start_offset : fact.end_offset
                    - source.start_offset
                ]
                != fact.raw_text
                or sha256(fact.raw_text.encode("utf-8")).hexdigest()
                != fact.raw_text_sha256
                or _as_utc(fact.source_available_at) != event.source_available_at
            ):
                raise BusinessEventQuantitativeReadIntegrityError(
                    "quantitative fact is not coherent with event evidence"
                )
            views.append(_fact_view(fact))
        available_codes = tuple(str(value) for value in derivation.available_fact_codes_json)
        expected_codes = tuple(
            code
            for code in BUSINESS_EVENT_QUANT_FACT_ORDER
            if any(f.fact_code == code for f in facts)
        )
        if available_codes != expected_codes:
            raise BusinessEventQuantitativeReadIntegrityError(
                "available quantitative fact codes are not canonical"
            )
        return PointInTimeBusinessEventQuantitativeDerivation(
            id=derivation.id,
            event=event,
            ruleset_code=derivation.ruleset_code,
            ruleset_semantic_version=derivation.ruleset_semantic_version,
            source_available_at=_as_utc(derivation.source_available_at),
            available_fact_codes=available_codes,
            warnings=tuple(str(value) for value in derivation.warnings_json),
            derivation_fingerprint_sha256=derivation.derivation_fingerprint_sha256,
            derived_at=_as_utc(derivation.derived_at),
            facts=tuple(views),
        )


def _fact_view(value: BusinessEventQuantitativeFact) -> BusinessEventQuantitativeFactView:
    return BusinessEventQuantitativeFactView(
        id=value.id,
        business_event_evidence_id=value.business_event_evidence_id,
        fact_code=value.fact_code,
        fact_kind=value.fact_kind,
        rule_code=value.rule_code,
        rule_semantic_version=value.rule_semantic_version,
        start_offset=value.start_offset,
        end_offset=value.end_offset,
        raw_text=value.raw_text,
        raw_text_sha256=value.raw_text_sha256,
        reported_value=value.reported_value,
        reported_scale=value.reported_scale,
        reported_unit=value.reported_unit,
        reported_currency=value.reported_currency,
        normalized_value=value.normalized_value,
        normalized_unit=value.normalized_unit,
        date_value=value.date_value,
        source_available_at=_as_utc(value.source_available_at),
        warnings=tuple(str(item) for item in value.warnings_json),
        fact_fingerprint_sha256=value.fact_fingerprint_sha256,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
