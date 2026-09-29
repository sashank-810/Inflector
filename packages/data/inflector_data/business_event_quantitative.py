"""Derive immutable explicit quantities from Phase 6C-A evidence spans only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_FACT_KINDS,
    BUSINESS_EVENT_QUANT_FACT_ORDER,
    EVENT_FACT_COMPATIBILITY,
    BusinessEventQuantitativeMatch,
    BusinessEventQuantitativeRuleEngine,
    BusinessEventQuantitativeRuleset,
)
from inflector_data.archive import RawObjectStore
from inflector_data.business_event_pit import (
    BusinessEventEvidenceView,
    PointInTimeBusinessEvent,
    PointInTimeBusinessEventReader,
)
from inflector_data.document_services import read_integrity_checked
from inflector_database.business_event_quantitative_repository import (
    BusinessEventQuantitativeRepository,
)
from inflector_database.models import (
    BusinessEventQuantitativeDerivation,
    BusinessEventQuantitativeFact,
)


class BusinessEventQuantitativeIntegrityError(ValueError):
    """Raised when source lineage or version-pinned quantitative output drifts."""


@dataclass(frozen=True, slots=True)
class BusinessEventQuantitativeFactResult:
    id: UUID
    business_event_evidence_id: UUID
    fact_code: str
    fact_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class BusinessEventQuantitativeDerivationResult:
    id: UUID
    derivation_fingerprint_sha256: str
    available_fact_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    facts: tuple[BusinessEventQuantitativeFactResult, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class _FactCandidate:
    business_event_evidence_id: UUID
    evidence_fingerprint: str
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
    warnings: tuple[str, ...]
    fact_fingerprint_sha256: str


class BusinessEventQuantitativeDerivationService:
    """Extract only from validated, persisted event-evidence excerpts."""

    def __init__(
        self,
        repository: BusinessEventQuantitativeRepository,
        object_store: RawObjectStore,
    ) -> None:
        self._repository = repository
        self._object_store = object_store

    def derive(
        self,
        *,
        event: PointInTimeBusinessEvent,
        ruleset: BusinessEventQuantitativeRuleset | None = None,
        derived_at: datetime,
    ) -> BusinessEventQuantitativeDerivationResult:
        engine = ruleset or BusinessEventQuantitativeRuleEngine()
        if not engine.ruleset_code or not engine.ruleset_semantic_version:
            raise ValueError("quantitative ruleset identity must be non-empty")
        derived_at_utc = _aware_utc(derived_at, field="derived_at")
        source_available_at = _aware_utc(
            event.source_available_at, field="event.source_available_at"
        )
        self._validate_event(event)

        deduplicated: dict[str, _FactCandidate] = {}
        for evidence in event.evidence:
            source_text = self._validated_source_text(event, evidence)
            if source_text[evidence.start_offset : evidence.end_offset] != evidence.excerpt_text:
                raise BusinessEventQuantitativeIntegrityError(
                    "event evidence does not select its immutable source excerpt"
                )
            for matched in engine.match(event_type=event.event_type, text=evidence.excerpt_text):
                candidate = self._candidate(
                    event=event,
                    evidence=evidence,
                    matched=matched,
                    ruleset_code=engine.ruleset_code,
                    ruleset_semantic_version=engine.ruleset_semantic_version,
                )
                deduplicated[candidate.fact_fingerprint_sha256] = candidate

        facts = tuple(sorted(deduplicated.values(), key=_fact_sort_key))
        available_fact_codes = tuple(
            code
            for code in BUSINESS_EVENT_QUANT_FACT_ORDER
            if any(fact.fact_code == code for fact in facts)
        )
        warnings: tuple[str, ...] = ()
        fingerprint = _derivation_fingerprint(
            event=event,
            source_available_at=source_available_at,
            ruleset_code=engine.ruleset_code,
            ruleset_semantic_version=engine.ruleset_semantic_version,
            facts=facts,
            available_fact_codes=available_fact_codes,
            warnings=warnings,
        )
        existing = self._repository.derivation(
            business_event_id=event.id,
            ruleset_code=engine.ruleset_code,
            ruleset_semantic_version=engine.ruleset_semantic_version,
        )
        if existing is not None:
            return self._reuse(existing, facts, fingerprint, event)
        derivation = self._repository.add_derivation(
            business_event_id=event.id,
            ruleset_code=engine.ruleset_code,
            ruleset_semantic_version=engine.ruleset_semantic_version,
            source_available_at=source_available_at,
            available_fact_codes=available_fact_codes,
            warnings=warnings,
            derivation_fingerprint_sha256=fingerprint,
            derived_at=derived_at_utc,
        )
        persisted = tuple(self._persist_fact(derivation.id, event, fact) for fact in facts)
        return _result(derivation, persisted, created=True)

    def _validate_event(self, event: PointInTimeBusinessEvent) -> None:
        persisted = self._repository.event(event.id)
        announcement = self._repository.announcement(event.announcement.id)
        if persisted is None or announcement is None:
            raise BusinessEventQuantitativeIntegrityError("business event lineage is missing")
        if (
            persisted.company_id != event.company_id
            or persisted.security_id != event.security_id
            or persisted.announcement_id != event.announcement.id
            or persisted.provider_dataset_id != event.provider_dataset_id
            or persisted.event_type != event.event_type
            or persisted.source_event_date != event.source_event_date
            or _database_utc(persisted.source_available_at)
            != _aware_utc(event.source_available_at, field="event.source_available_at")
            or persisted.ruleset_code != event.ruleset_code
            or persisted.ruleset_semantic_version != event.ruleset_semantic_version
            or persisted.detection_fingerprint_sha256
            != event.detection_fingerprint_sha256
            or persisted.status != "detected"
            or tuple(str(value) for value in persisted.matched_rule_codes_json)
            != event.matched_rule_codes
            or tuple(str(value) for value in persisted.warnings_json) != event.warnings
            or announcement.company_id != event.company_id
            or announcement.security_id != event.security_id
            or announcement.provider_dataset_id != event.provider_dataset_id
            or announcement.headline != event.announcement.headline
            or announcement.announcement_date != event.announcement.announcement_date
            or _database_utc(announcement.available_at)
            != _aware_utc(event.announcement.available_at, field="announcement.available_at")
        ):
            raise BusinessEventQuantitativeIntegrityError(
                "supplied business event does not match persisted lineage"
            )
        reread = PointInTimeBusinessEventReader(self._repository.session).events_for_announcement(
            announcement=event.announcement,
            ruleset_code=event.ruleset_code,
            ruleset_semantic_version=event.ruleset_semantic_version,
        )
        selected = next((value for value in reread if value.id == event.id), None)
        if selected != event:
            raise BusinessEventQuantitativeIntegrityError(
                "supplied business event evidence set is not canonical"
            )

    def _validated_source_text(
        self,
        event: PointInTimeBusinessEvent,
        evidence: BusinessEventEvidenceView,
    ) -> str:
        if (
            evidence.source_available_at != event.source_available_at
            or sha256(evidence.excerpt_text.encode("utf-8")).hexdigest()
            != evidence.excerpt_sha256
        ):
            raise BusinessEventQuantitativeIntegrityError("event evidence hash/time mismatch")
        if evidence.evidence_kind == "announcement_headline":
            return event.announcement.headline
        if evidence.evidence_kind != "document_text":
            raise BusinessEventQuantitativeIntegrityError("unsupported event evidence kind")
        if (
            evidence.document is None
            or evidence.document_asset is None
            or evidence.text_extraction is None
            or evidence.document_asset.document_id != evidence.document.id
            or evidence.text_extraction.document_asset_id != evidence.document_asset.id
            or evidence.text_extraction.status != "success"
            or evidence.text_extraction.text_object_key is None
            or evidence.text_extraction.text_sha256 is None
        ):
            raise BusinessEventQuantitativeIntegrityError(
                "document event evidence lacks successful extraction lineage"
            )
        lineage = self._repository.extraction_lineage(
            asset_id=evidence.document_asset.id,
            extraction_id=evidence.text_extraction.id,
        )
        if lineage is None:
            raise BusinessEventQuantitativeIntegrityError(
                "document asset/extraction lineage is missing"
            )
        asset, extraction = lineage
        if (
            asset.document_id != evidence.document.id
            or asset.content_sha256 != evidence.document_asset.content_sha256
            or asset.object_key != evidence.document_asset.object_key
            or extraction.document_asset_id != asset.id
            or extraction.text_object_key != evidence.text_extraction.text_object_key
            or extraction.text_sha256 != evidence.text_extraction.text_sha256
            or extraction.status != "success"
        ):
            raise BusinessEventQuantitativeIntegrityError(
                "persisted document extraction lineage changed"
            )
        if extraction.text_object_key is None or extraction.text_sha256 is None:
            raise BusinessEventQuantitativeIntegrityError(
                "persisted successful extraction lacks text object lineage"
            )
        read_integrity_checked(
            self._object_store,
            object_key=asset.object_key,
            expected_sha256=asset.content_sha256,
        )
        content = read_integrity_checked(
            self._object_store,
            object_key=extraction.text_object_key,
            expected_sha256=extraction.text_sha256,
        )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BusinessEventQuantitativeIntegrityError(
                "canonical extraction text is not UTF-8"
            ) from error
        if extraction.character_count != len(text):
            raise BusinessEventQuantitativeIntegrityError(
                "canonical extraction character count mismatch"
            )
        for page_number, page_hash in zip(
            evidence.page_numbers, evidence.page_text_sha256s, strict=True
        ):
            raw_page = next(
                (
                    value
                    for value in extraction.page_map_json
                    if value.get("page_number") == page_number
                ),
                None,
            )
            if raw_page is None:
                raise BusinessEventQuantitativeIntegrityError("evidence page is missing")
            start = raw_page.get("start_offset")
            end = raw_page.get("end_offset")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or raw_page.get("page_text_sha256") != page_hash
                or sha256(text[start:end].encode("utf-8")).hexdigest() != page_hash
            ):
                raise BusinessEventQuantitativeIntegrityError("evidence page hash mismatch")
        return text

    @staticmethod
    def _candidate(
        *,
        event: PointInTimeBusinessEvent,
        evidence: BusinessEventEvidenceView,
        matched: BusinessEventQuantitativeMatch,
        ruleset_code: str,
        ruleset_semantic_version: str,
    ) -> _FactCandidate:
        if matched.fact_code not in EVENT_FACT_COMPATIBILITY[event.event_type]:
            raise BusinessEventQuantitativeIntegrityError(
                "quantitative fact is incompatible with business event type"
            )
        if matched.fact_kind not in BUSINESS_EVENT_QUANT_FACT_KINDS:
            raise BusinessEventQuantitativeIntegrityError("unknown quantitative fact kind")
        if not (
            0 <= matched.local_start_offset < matched.local_end_offset <= len(evidence.excerpt_text)
        ):
            raise BusinessEventQuantitativeIntegrityError("quantitative match offsets are invalid")
        raw_text = evidence.excerpt_text[
            matched.local_start_offset : matched.local_end_offset
        ]
        if raw_text != matched.raw_text:
            raise BusinessEventQuantitativeIntegrityError(
                "quantitative raw text does not match evidence substring"
            )
        absolute_start = evidence.start_offset + matched.local_start_offset
        absolute_end = evidence.start_offset + matched.local_end_offset
        if not evidence.start_offset <= absolute_start < absolute_end <= evidence.end_offset:
            raise BusinessEventQuantitativeIntegrityError(
                "quantitative fact extends outside event evidence"
            )
        _validate_representation(matched)
        raw_hash = sha256(raw_text.encode("utf-8")).hexdigest()
        payload = {
            "business_event_id": str(event.id),
            "business_event_detection_fingerprint": event.detection_fingerprint_sha256,
            "business_event_evidence_id": str(evidence.id),
            "evidence_fingerprint": evidence.evidence_fingerprint_sha256,
            "quantitative_ruleset_code": ruleset_code,
            "quantitative_ruleset_semantic_version": ruleset_semantic_version,
            "fact_code": matched.fact_code,
            "fact_kind": matched.fact_kind,
            "rule_code": matched.rule_code,
            "rule_semantic_version": matched.rule_semantic_version,
            "start_offset": absolute_start,
            "end_offset": absolute_end,
            "raw_text_sha256": raw_hash,
            "reported_value": _decimal_text(matched.reported_value),
            "reported_scale": matched.reported_scale,
            "reported_unit": matched.reported_unit,
            "reported_currency": matched.reported_currency,
            "normalized_value": _decimal_text(matched.normalized_value),
            "normalized_unit": matched.normalized_unit,
            "date_value": (
                None if matched.date_value is None else matched.date_value.isoformat()
            ),
            "warnings": list(matched.warnings),
        }
        fingerprint = _canonical_hash(payload)
        return _FactCandidate(
            business_event_evidence_id=evidence.id,
            evidence_fingerprint=evidence.evidence_fingerprint_sha256,
            fact_code=matched.fact_code,
            fact_kind=matched.fact_kind,
            rule_code=matched.rule_code,
            rule_semantic_version=matched.rule_semantic_version,
            start_offset=absolute_start,
            end_offset=absolute_end,
            raw_text=raw_text,
            raw_text_sha256=raw_hash,
            reported_value=matched.reported_value,
            reported_scale=matched.reported_scale,
            reported_unit=matched.reported_unit,
            reported_currency=matched.reported_currency,
            normalized_value=matched.normalized_value,
            normalized_unit=matched.normalized_unit,
            date_value=matched.date_value,
            warnings=matched.warnings,
            fact_fingerprint_sha256=fingerprint,
        )

    def _persist_fact(
        self,
        derivation_id: UUID,
        event: PointInTimeBusinessEvent,
        fact: _FactCandidate,
    ) -> BusinessEventQuantitativeFact:
        return self._repository.add_fact(
            derivation_id=derivation_id,
            business_event_evidence_id=fact.business_event_evidence_id,
            fact_code=fact.fact_code,
            fact_kind=fact.fact_kind,
            rule_code=fact.rule_code,
            rule_semantic_version=fact.rule_semantic_version,
            start_offset=fact.start_offset,
            end_offset=fact.end_offset,
            raw_text=fact.raw_text,
            raw_text_sha256=fact.raw_text_sha256,
            reported_value=fact.reported_value,
            reported_scale=fact.reported_scale,
            reported_unit=fact.reported_unit,
            reported_currency=fact.reported_currency,
            normalized_value=fact.normalized_value,
            normalized_unit=fact.normalized_unit,
            date_value=fact.date_value,
            source_available_at=event.source_available_at,
            warnings=fact.warnings,
            fact_fingerprint_sha256=fact.fact_fingerprint_sha256,
        )

    def _reuse(
        self,
        derivation: BusinessEventQuantitativeDerivation,
        candidates: tuple[_FactCandidate, ...],
        fingerprint: str,
        event: PointInTimeBusinessEvent,
    ) -> BusinessEventQuantitativeDerivationResult:
        if derivation.derivation_fingerprint_sha256 != fingerprint:
            raise BusinessEventQuantitativeIntegrityError(
                "quantitative ruleset version produced a different derivation fingerprint"
            )
        persisted = tuple(self._repository.facts(derivation.id))
        expected = {value.fact_fingerprint_sha256: value for value in candidates}
        if set(expected) != {value.fact_fingerprint_sha256 for value in persisted}:
            raise BusinessEventQuantitativeIntegrityError(
                "persisted quantitative facts differ under the same ruleset version"
            )
        if (
            derivation.business_event_id != event.id
            or _database_utc(derivation.source_available_at) != event.source_available_at
            or tuple(str(value) for value in derivation.available_fact_codes_json)
            != tuple(
                code
                for code in BUSINESS_EVENT_QUANT_FACT_ORDER
                if any(candidate.fact_code == code for candidate in candidates)
            )
            or derivation.warnings_json
        ):
            raise BusinessEventQuantitativeIntegrityError(
                "persisted quantitative derivation is not canonical"
            )
        for fact in persisted:
            candidate = expected[fact.fact_fingerprint_sha256]
            if not _fact_matches_candidate(fact, candidate, event.source_available_at):
                raise BusinessEventQuantitativeIntegrityError(
                    "persisted quantitative fact is not canonical"
                )
        return _result(derivation, persisted, created=False)


def _validate_representation(value: BusinessEventQuantitativeMatch) -> None:
    if value.fact_kind == "date":
        valid = (
            value.date_value is not None
            and value.reported_value is None
            and value.reported_scale is None
            and value.reported_unit is None
            and value.reported_currency is None
            and value.normalized_value is None
            and value.normalized_unit is None
        )
    elif value.fact_kind == "monetary":
        valid = (
            value.date_value is None
            and value.reported_value is not None
            and value.reported_scale is not None
            and value.reported_unit is None
            and value.reported_currency in {"INR", "USD", "EUR"}
            and value.normalized_value is not None
            and value.normalized_unit == "currency_major"
        )
    elif value.fact_kind == "capacity":
        valid = (
            value.date_value is None
            and value.reported_value is not None
            and value.reported_scale is None
            and value.reported_unit is not None
            and value.reported_currency is None
            and value.normalized_value is not None
            and value.normalized_unit
            in {"tonnes_per_annum", "tonnes_per_day", "megawatt", "kilolitres_per_day"}
        )
    else:
        valid = (
            value.fact_kind == "fraction"
            and value.date_value is None
            and value.reported_value is not None
            and value.reported_scale is None
            and value.reported_unit == "percent"
            and value.reported_currency is None
            and value.normalized_value is not None
            and value.normalized_unit == "fraction"
        )
    if not valid:
        raise BusinessEventQuantitativeIntegrityError(
            "quantitative fact representation is incoherent"
        )


def _derivation_fingerprint(
    *,
    event: PointInTimeBusinessEvent,
    source_available_at: datetime,
    ruleset_code: str,
    ruleset_semantic_version: str,
    facts: tuple[_FactCandidate, ...],
    available_fact_codes: tuple[str, ...],
    warnings: tuple[str, ...],
) -> str:
    return _canonical_hash(
        {
            "business_event_id": str(event.id),
            "business_event_detection_fingerprint": event.detection_fingerprint_sha256,
            "source_available_at": source_available_at.isoformat(),
            "quantitative_ruleset_code": ruleset_code,
            "quantitative_ruleset_semantic_version": ruleset_semantic_version,
            "ordered_fact_fingerprints": [
                value.fact_fingerprint_sha256 for value in facts
            ],
            "available_fact_codes": list(available_fact_codes),
            "warnings": list(warnings),
        }
    )


def _fact_sort_key(value: _FactCandidate) -> tuple[object, ...]:
    return (
        BUSINESS_EVENT_QUANT_FACT_ORDER.index(value.fact_code),
        str(value.business_event_evidence_id),
        value.start_offset,
        value.end_offset,
        value.rule_code,
        value.fact_fingerprint_sha256,
    )


def _fact_matches_candidate(
    fact: BusinessEventQuantitativeFact,
    candidate: _FactCandidate,
    source_available_at: datetime,
) -> bool:
    return (
        fact.business_event_evidence_id == candidate.business_event_evidence_id
        and fact.fact_code == candidate.fact_code
        and fact.fact_kind == candidate.fact_kind
        and fact.rule_code == candidate.rule_code
        and fact.rule_semantic_version == candidate.rule_semantic_version
        and fact.start_offset == candidate.start_offset
        and fact.end_offset == candidate.end_offset
        and fact.raw_text == candidate.raw_text
        and fact.raw_text_sha256 == candidate.raw_text_sha256
        and fact.reported_value == candidate.reported_value
        and fact.reported_scale == candidate.reported_scale
        and fact.reported_unit == candidate.reported_unit
        and fact.reported_currency == candidate.reported_currency
        and fact.normalized_value == candidate.normalized_value
        and fact.normalized_unit == candidate.normalized_unit
        and fact.date_value == candidate.date_value
        and _database_utc(fact.source_available_at) == source_available_at
        and tuple(str(value) for value in fact.warnings_json) == candidate.warnings
        and sha256(fact.raw_text.encode("utf-8")).hexdigest() == fact.raw_text_sha256
    )


def _canonical_hash(payload: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _result(
    derivation: BusinessEventQuantitativeDerivation,
    facts: tuple[BusinessEventQuantitativeFact, ...],
    *,
    created: bool,
) -> BusinessEventQuantitativeDerivationResult:
    return BusinessEventQuantitativeDerivationResult(
        id=derivation.id,
        derivation_fingerprint_sha256=derivation.derivation_fingerprint_sha256,
        available_fact_codes=tuple(
            str(value) for value in derivation.available_fact_codes_json
        ),
        warnings=tuple(str(value) for value in derivation.warnings_json),
        facts=tuple(
            BusinessEventQuantitativeFactResult(
                id=value.id,
                business_event_evidence_id=value.business_event_evidence_id,
                fact_code=value.fact_code,
                fact_fingerprint_sha256=value.fact_fingerprint_sha256,
            )
            for value in facts
        ),
        created=created,
    )
