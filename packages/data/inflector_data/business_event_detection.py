"""Deterministic Phase 6C-A event derivation from already selected PIT evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from inflector_core.business_event_rules import (
    BUSINESS_EVENT_TYPE_ORDER,
    BusinessEventRuleEngine,
    BusinessEventRuleMatch,
    BusinessEventRuleset,
)
from inflector_data.announcement_pit import PointInTimeAnnouncement, PointInTimeDocument
from inflector_data.document_services import ASSET_ACCEPTED_STATUSES, PAGE_SEPARATOR
from inflector_data.document_text import PointInTimeDocumentText
from inflector_database.business_event_repository import BusinessEventRepository
from inflector_database.models import BusinessEvent, BusinessEventEvidence

MAX_EVIDENCE_CONTEXT_CHARACTERS = 1000


class BusinessEventIntegrityError(ValueError):
    """Raised when persisted or supplied versioned event evidence is incoherent."""


@dataclass(frozen=True, slots=True)
class BusinessEventEvidenceResult:
    id: UUID
    evidence_kind: str
    rule_code: str
    evidence_fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class BusinessEventDetectionResult:
    id: UUID
    event_type: str
    detection_fingerprint_sha256: str
    evidence: tuple[BusinessEventEvidenceResult, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class _EvidenceCandidate:
    evidence_kind: str
    document_id: UUID | None
    document_asset_id: UUID | None
    text_extraction_id: UUID | None
    rule_code: str
    rule_semantic_version: str
    start_offset: int
    end_offset: int
    page_numbers: tuple[int, ...]
    page_text_sha256s: tuple[str, ...]
    excerpt_text: str
    excerpt_sha256: str
    evidence_fingerprint_sha256: str


class BusinessEventDetectionService:
    """Persist version-pinned rule matches without selecting source revisions."""

    def __init__(self, repository: BusinessEventRepository) -> None:
        self._repository = repository

    def detect(
        self,
        *,
        announcement: PointInTimeAnnouncement,
        document_texts: tuple[PointInTimeDocumentText, ...] = (),
        ruleset: BusinessEventRuleset | None = None,
        derived_at: datetime,
    ) -> tuple[BusinessEventDetectionResult, ...]:
        engine = ruleset or BusinessEventRuleEngine()
        derived_at_utc = _aware_utc(derived_at, field="derived_at")
        source_available_at = _aware_utc(
            announcement.available_at,
            field="announcement.available_at",
        )
        if announcement.source_record.validation_status != "accepted":
            raise BusinessEventIntegrityError("announcement source record is not accepted")
        if not engine.ruleset_code or not engine.ruleset_semantic_version:
            raise ValueError("ruleset identity must be non-empty")

        self._validate_announcement(announcement)

        documents = {document.id: document for document in announcement.documents}
        candidates: list[tuple[str, _EvidenceCandidate]] = []
        candidates.extend(
            self._headline_candidates(
                announcement=announcement,
                matches=engine.match(announcement.headline),
            )
        )
        for value in document_texts:
            document = documents.get(value.document.id)
            if document is None:
                raise BusinessEventIntegrityError(
                    "document text does not belong to the selected announcement"
                )
            self._validate_document_text(
                announcement=announcement,
                expected_document=document,
                value=value,
            )
            if value.extraction.status != "success" or value.text is None:
                continue
            for page in value.pages:
                if page.text is None:
                    raise BusinessEventIntegrityError("successful extraction page has no text")
                for match in engine.match(page.text):
                    self._validate_match(match, len(page.text))
                    local_start, local_end = _context_bounds(
                        page.text,
                        match.start_offset,
                        match.end_offset,
                        line_local=True,
                    )
                    start_offset = page.start_offset + local_start
                    end_offset = page.start_offset + local_end
                    excerpt = value.text[start_offset:end_offset]
                    if excerpt != page.text[local_start:local_end]:
                        raise BusinessEventIntegrityError(
                            "document evidence offsets do not select the page context"
                        )
                    candidates.append(
                        (
                            match.event_type,
                            self._candidate(
                                announcement_id=announcement.id,
                                evidence_kind="document_text",
                                document_id=document.id,
                                document_asset_id=value.asset.id,
                                text_extraction_id=value.extraction.id,
                                match=match,
                                start_offset=start_offset,
                                end_offset=end_offset,
                                excerpt=excerpt,
                                page_numbers=(page.page_number,),
                                page_text_sha256s=(page.page_text_sha256,),
                            ),
                        )
                    )

        grouped: dict[str, dict[str, _EvidenceCandidate]] = {}
        for event_type, candidate in candidates:
            if event_type not in BUSINESS_EVENT_TYPE_ORDER:
                raise ValueError(f"unsupported business event type: {event_type}")
            grouped.setdefault(event_type, {})[candidate.evidence_fingerprint_sha256] = candidate

        results: list[BusinessEventDetectionResult] = []
        for event_type in BUSINESS_EVENT_TYPE_ORDER:
            event_candidates = grouped.get(event_type)
            if not event_candidates:
                continue
            ordered = tuple(sorted(event_candidates.values(), key=_candidate_sort_key))
            matched_rule_codes = tuple(dict.fromkeys(item.rule_code for item in ordered))
            fingerprint = _event_fingerprint(
                announcement=announcement,
                event_type=event_type,
                source_available_at=source_available_at,
                ruleset_code=engine.ruleset_code,
                ruleset_semantic_version=engine.ruleset_semantic_version,
                matched_rule_codes=matched_rule_codes,
                evidence_fingerprints=tuple(
                    item.evidence_fingerprint_sha256 for item in ordered
                ),
            )
            existing = self._repository.event(
                announcement_id=announcement.id,
                event_type=event_type,
                ruleset_code=engine.ruleset_code,
                ruleset_semantic_version=engine.ruleset_semantic_version,
            )
            if existing is not None:
                results.append(
                    self._reuse(
                        existing,
                        ordered,
                        fingerprint,
                        announcement=announcement,
                        source_available_at=source_available_at,
                        matched_rule_codes=matched_rule_codes,
                    )
                )
                continue
            event = self._repository.add_event(
                company_id=announcement.company_id,
                security_id=announcement.security_id,
                announcement_id=announcement.id,
                provider_dataset_id=announcement.provider_dataset_id,
                event_type=event_type,
                source_event_date=announcement.announcement_date,
                source_available_at=source_available_at,
                ruleset_code=engine.ruleset_code,
                ruleset_semantic_version=engine.ruleset_semantic_version,
                matched_rule_codes=matched_rule_codes,
                detection_fingerprint_sha256=fingerprint,
                derived_at=derived_at_utc,
            )
            evidence = tuple(self._persist_evidence(event, announcement, item) for item in ordered)
            results.append(_result(event, evidence, created=True))
        return tuple(results)

    def _validate_announcement(self, value: PointInTimeAnnouncement) -> None:
        lineage = self._repository.announcement_lineage(value.id)
        if lineage is None:
            raise BusinessEventIntegrityError("selected announcement is not persisted")
        announcement, source = lineage
        if (
            announcement.company_id != value.company_id
            or announcement.security_id != value.security_id
            or announcement.provider_dataset_id != value.provider_dataset_id
            or announcement.source_record_id != value.source_record.id
            or announcement.provider_category != value.provider_category
            or announcement.headline != value.headline
            or announcement.announcement_date != value.announcement_date
            or announcement.exchange != value.exchange
            or _database_utc(announcement.available_at)
            != _aware_utc(value.available_at, field="announcement.available_at")
            or _database_utc_or_none(announcement.revision_at)
            != _aware_utc_or_none(value.revision_at, field="announcement.revision_at")
            or _database_utc(announcement.ingested_at)
            != _aware_utc(value.ingested_at, field="announcement.ingested_at")
            or source.provider_dataset_id != value.provider_dataset_id
            or source.external_record_id != value.external_record_id
            or source.id != value.source_record.id
            or source.source_uri != value.source_record.source_uri
            or source.raw_object_key != value.source_record.raw_object_key
            or source.raw_payload_reference != value.source_record.raw_payload_reference
            or source.content_sha256 != value.source_record.content_sha256
            or source.validation_status != "accepted"
            or value.source_record.validation_status != "accepted"
        ):
            raise BusinessEventIntegrityError(
                "selected announcement does not match persisted accepted lineage"
            )
        persisted_documents = self._repository.announcement_documents(value.id)
        by_id = {document.id: document for document in value.documents}
        if len(by_id) != len(value.documents) or set(by_id) != {
            document.id for _, document, _ in persisted_documents
        }:
            raise BusinessEventIntegrityError(
                "selected announcement document membership is not canonical"
            )
        for relation, document, document_source in persisted_documents:
            selected = by_id[document.id]
            if (
                selected.company_id != document.company_id
                or selected.security_id != document.security_id
                or selected.provider_dataset_id != document.provider_dataset_id
                or selected.source_record.id != document.source_record_id
                or selected.document_type != document.document_type
                or selected.title != document.title
                or selected.language != document.language
                or selected.media_type != document.media_type
                or selected.document_uri != document.document_uri
                or selected.document_content_sha256 != document.document_content_sha256
                or selected.role != relation.role
                or _aware_utc(selected.available_at, field="document.available_at")
                != _database_utc(document.available_at)
                or _aware_utc_or_none(selected.revision_at, field="document.revision_at")
                != _database_utc_or_none(document.revision_at)
                or _aware_utc(selected.ingested_at, field="document.ingested_at")
                != _database_utc(document.ingested_at)
                or document_source.id != selected.source_record.id
                or document_source.provider_dataset_id != value.provider_dataset_id
                or document_source.external_record_id
                != selected.source_record.external_record_id
                or document_source.source_uri != selected.source_record.source_uri
                or document_source.raw_object_key != selected.source_record.raw_object_key
                or document_source.raw_payload_reference
                != selected.source_record.raw_payload_reference
                or document_source.content_sha256 != selected.source_record.content_sha256
                or document_source.validation_status != "accepted"
                or selected.source_record.validation_status != "accepted"
            ):
                raise BusinessEventIntegrityError(
                    "selected announcement document lineage is not canonical"
                )

    def _headline_candidates(
        self,
        *,
        announcement: PointInTimeAnnouncement,
        matches: tuple[BusinessEventRuleMatch, ...],
    ) -> list[tuple[str, _EvidenceCandidate]]:
        values: list[tuple[str, _EvidenceCandidate]] = []
        for match in matches:
            self._validate_match(match, len(announcement.headline))
            start_offset, end_offset = _context_bounds(
                announcement.headline,
                match.start_offset,
                match.end_offset,
                line_local=False,
            )
            excerpt = announcement.headline[start_offset:end_offset]
            values.append(
                (
                    match.event_type,
                    self._candidate(
                        announcement_id=announcement.id,
                        evidence_kind="announcement_headline",
                        document_id=None,
                        document_asset_id=None,
                        text_extraction_id=None,
                        match=match,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        excerpt=excerpt,
                        page_numbers=(),
                        page_text_sha256s=(),
                    ),
                )
            )
        return values

    @staticmethod
    def _validate_match(match: BusinessEventRuleMatch, text_length: int) -> None:
        if not 0 <= match.start_offset < match.end_offset <= text_length:
            raise BusinessEventIntegrityError("rule match offsets are outside source text")
        if not match.rule_code or not match.rule_semantic_version:
            raise BusinessEventIntegrityError("rule identity must be non-empty")

    def _validate_document_text(
        self,
        *,
        announcement: PointInTimeAnnouncement,
        expected_document: PointInTimeDocument,
        value: PointInTimeDocumentText,
    ) -> None:
        document = value.document
        source_available_at = _aware_utc(
            announcement.available_at,
            field="announcement.available_at",
        )
        if (
            document != expected_document
            or document.company_id != announcement.company_id
            or document.security_id != announcement.security_id
            or document.provider_dataset_id != announcement.provider_dataset_id
            or _aware_utc(document.available_at, field="document.available_at")
            != source_available_at
            or _aware_utc(value.source_available_at, field="source_available_at")
            != source_available_at
        ):
            raise BusinessEventIntegrityError(
                "document text context does not match the selected announcement"
            )
        if value.asset.document_id != document.id:
            raise BusinessEventIntegrityError("document asset belongs to a different document")
        if value.extraction.document_asset_id != value.asset.id:
            raise BusinessEventIntegrityError("text extraction belongs to a different asset")
        lineage = self._repository.extraction_lineage(
            document_asset_id=value.asset.id,
            text_extraction_id=value.extraction.id,
        )
        if lineage is None:
            raise BusinessEventIntegrityError("document extraction lineage is not persisted")
        asset, extraction = lineage
        if (
            asset.document_id != document.id
            or asset.content_sha256 != value.asset.content_sha256
            or asset.object_key != value.asset.object_key
            or asset.size_bytes != value.asset.size_bytes
            or asset.requested_uri != value.asset.requested_uri
            or asset.resolved_uri != value.asset.resolved_uri
            or asset.declared_media_type != value.asset.declared_media_type
            or asset.detected_media_type != value.asset.detected_media_type
            or _database_utc(asset.retrieved_at)
            != _aware_utc(value.asset.retrieved_at, field="asset.retrieved_at")
            or asset.status != value.asset.status
            or tuple(str(item) for item in asset.warnings_json) != value.asset.warnings
            or extraction.document_asset_id != asset.id
            or extraction.status != value.extraction.status
            or extraction.text_object_key != value.extraction.text_object_key
            or extraction.text_sha256 != value.extraction.text_sha256
            or extraction.character_count != value.extraction.character_count
            or extraction.page_count != value.extraction.page_count
            or extraction.extractor_code != value.extraction.extractor_code
            or extraction.extractor_semantic_version
            != value.extraction.extractor_semantic_version
            or extraction.extractor_runtime_version != value.extraction.extractor_runtime_version
            or tuple(str(item) for item in extraction.warnings_json) != value.extraction.warnings
            or _database_utc(extraction.extracted_at)
            != _aware_utc(
                value.extraction.extracted_at,
                field="extraction.extracted_at",
            )
        ):
            raise BusinessEventIntegrityError(
                "supplied extraction does not match persisted lineage"
            )
        if asset.status not in ASSET_ACCEPTED_STATUSES:
            raise BusinessEventIntegrityError("event evidence requires an accepted document asset")
        if extraction.status != "success":
            if value.text is not None:
                raise BusinessEventIntegrityError("non-success extraction supplied source text")
            return
        if value.text is None or value.extraction.text_sha256 is None:
            raise BusinessEventIntegrityError("successful extraction is missing canonical text")
        if sha256(value.text.encode("utf-8")).hexdigest() != value.extraction.text_sha256:
            raise BusinessEventIntegrityError("canonical document text hash mismatch")
        if value.extraction.character_count != len(value.text):
            raise BusinessEventIntegrityError("canonical document character count mismatch")
        if len(value.pages) != value.extraction.page_count:
            raise BusinessEventIntegrityError("canonical document page count mismatch")
        expected_page_map: list[dict[str, object]] = []
        for index, page in enumerate(value.pages):
            if page.page_number != index + 1 or page.text is None:
                raise BusinessEventIntegrityError("invalid ordered page evidence")
            if index == 0 and page.start_offset != 0:
                raise BusinessEventIntegrityError("first page must start at offset zero")
            if value.text[page.start_offset : page.end_offset] != page.text:
                raise BusinessEventIntegrityError("page offsets do not select canonical page text")
            if sha256(page.text.encode("utf-8")).hexdigest() != page.page_text_sha256:
                raise BusinessEventIntegrityError("page text hash mismatch")
            if index:
                previous_end = value.pages[index - 1].end_offset
                if (
                    page.start_offset != previous_end + len(PAGE_SEPARATOR)
                    or value.text[previous_end : page.start_offset] != PAGE_SEPARATOR
                ):
                    raise BusinessEventIntegrityError("page separator offsets are incoherent")
            expected_page_map.append(
                {
                    "page_number": page.page_number,
                    "start_offset": page.start_offset,
                    "end_offset": page.end_offset,
                    "page_text_sha256": page.page_text_sha256,
                }
            )
        if value.pages and value.pages[-1].end_offset != len(value.text):
            raise BusinessEventIntegrityError("final page must end at canonical text length")
        if extraction.page_map_json != expected_page_map:
            raise BusinessEventIntegrityError("supplied pages do not match persisted page map")

    @staticmethod
    def _candidate(
        *,
        announcement_id: UUID,
        evidence_kind: str,
        document_id: UUID | None,
        document_asset_id: UUID | None,
        text_extraction_id: UUID | None,
        match: BusinessEventRuleMatch,
        start_offset: int,
        end_offset: int,
        excerpt: str,
        page_numbers: tuple[int, ...],
        page_text_sha256s: tuple[str, ...],
    ) -> _EvidenceCandidate:
        excerpt_hash = sha256(excerpt.encode("utf-8")).hexdigest()
        fingerprint = _canonical_hash(
            {
                "announcement_id": str(announcement_id),
                "evidence_kind": evidence_kind,
                "document_id": None if document_id is None else str(document_id),
                "document_asset_id": (
                    None if document_asset_id is None else str(document_asset_id)
                ),
                "text_extraction_id": (
                    None if text_extraction_id is None else str(text_extraction_id)
                ),
                "rule_code": match.rule_code,
                "rule_semantic_version": match.rule_semantic_version,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "excerpt_sha256": excerpt_hash,
                "page_numbers": list(page_numbers),
                "page_text_sha256s": list(page_text_sha256s),
            }
        )
        return _EvidenceCandidate(
            evidence_kind=evidence_kind,
            document_id=document_id,
            document_asset_id=document_asset_id,
            text_extraction_id=text_extraction_id,
            rule_code=match.rule_code,
            rule_semantic_version=match.rule_semantic_version,
            start_offset=start_offset,
            end_offset=end_offset,
            page_numbers=page_numbers,
            page_text_sha256s=page_text_sha256s,
            excerpt_text=excerpt,
            excerpt_sha256=excerpt_hash,
            evidence_fingerprint_sha256=fingerprint,
        )

    def _reuse(
        self,
        event: BusinessEvent,
        candidates: tuple[_EvidenceCandidate, ...],
        fingerprint: str,
        *,
        announcement: PointInTimeAnnouncement,
        source_available_at: datetime,
        matched_rule_codes: tuple[str, ...],
    ) -> BusinessEventDetectionResult:
        if event.detection_fingerprint_sha256 != fingerprint:
            raise BusinessEventIntegrityError(
                "ruleset semantic version produced a different event fingerprint"
            )
        if (
            event.company_id != announcement.company_id
            or event.security_id != announcement.security_id
            or event.provider_dataset_id != announcement.provider_dataset_id
            or event.source_event_date != announcement.announcement_date
            or _database_utc(event.source_available_at) != source_available_at
            or tuple(str(item) for item in event.matched_rule_codes_json)
            != matched_rule_codes
            or event.status != "detected"
            or event.warnings_json
        ):
            raise BusinessEventIntegrityError("persisted event status is not canonical")
        evidence = tuple(self._repository.evidence_for_event(event.id))
        expected = sorted(item.evidence_fingerprint_sha256 for item in candidates)
        actual = sorted(item.evidence_fingerprint_sha256 for item in evidence)
        if actual != expected:
            raise BusinessEventIntegrityError(
                "persisted event evidence differs under the same ruleset version"
            )
        candidates_by_fingerprint = {
            item.evidence_fingerprint_sha256: item for item in candidates
        }
        for row in evidence:
            candidate = candidates_by_fingerprint[row.evidence_fingerprint_sha256]
            if (
                row.announcement_id != event.announcement_id
                or row.evidence_kind != candidate.evidence_kind
                or row.document_id != candidate.document_id
                or row.document_asset_id != candidate.document_asset_id
                or row.text_extraction_id != candidate.text_extraction_id
                or row.rule_code != candidate.rule_code
                or row.rule_semantic_version != candidate.rule_semantic_version
                or row.start_offset != candidate.start_offset
                or row.end_offset != candidate.end_offset
                or tuple(row.page_numbers_json) != candidate.page_numbers
                or tuple(row.page_text_sha256s_json) != candidate.page_text_sha256s
                or row.excerpt_text != candidate.excerpt_text
                or row.excerpt_sha256 != candidate.excerpt_sha256
                or _database_utc(row.source_available_at)
                != _database_utc(event.source_available_at)
                or sha256(row.excerpt_text.encode("utf-8")).hexdigest()
                != row.excerpt_sha256
            ):
                raise BusinessEventIntegrityError("persisted event evidence is not canonical")
        return _result(event, evidence, created=False)

    def _persist_evidence(
        self,
        event: BusinessEvent,
        announcement: PointInTimeAnnouncement,
        candidate: _EvidenceCandidate,
    ) -> BusinessEventEvidence:
        if sha256(candidate.excerpt_text.encode("utf-8")).hexdigest() != candidate.excerpt_sha256:
            raise BusinessEventIntegrityError("event excerpt hash mismatch")
        return self._repository.add_evidence(
            business_event_id=event.id,
            announcement_id=announcement.id,
            evidence_kind=candidate.evidence_kind,
            document_id=candidate.document_id,
            document_asset_id=candidate.document_asset_id,
            text_extraction_id=candidate.text_extraction_id,
            rule_code=candidate.rule_code,
            rule_semantic_version=candidate.rule_semantic_version,
            start_offset=candidate.start_offset,
            end_offset=candidate.end_offset,
            page_numbers=candidate.page_numbers,
            page_text_sha256s=candidate.page_text_sha256s,
            excerpt_text=candidate.excerpt_text,
            excerpt_sha256=candidate.excerpt_sha256,
            source_available_at=_aware_utc(
                announcement.available_at,
                field="announcement.available_at",
            ),
            evidence_fingerprint_sha256=candidate.evidence_fingerprint_sha256,
        )


def _context_bounds(
    text: str,
    match_start: int,
    match_end: int,
    *,
    line_local: bool,
) -> tuple[int, int]:
    if line_local:
        line_start = text.rfind("\n", 0, match_start) + 1
        next_newline = text.find("\n", match_end)
        line_end = len(text) if next_newline == -1 else next_newline
    else:
        line_start, line_end = 0, len(text)
    if line_end - line_start <= MAX_EVIDENCE_CONTEXT_CHARACTERS:
        return line_start, line_end
    match_length = match_end - match_start
    left_allowance = max(0, (MAX_EVIDENCE_CONTEXT_CHARACTERS - match_length) // 2)
    start = max(line_start, match_start - left_allowance)
    start = min(start, line_end - MAX_EVIDENCE_CONTEXT_CHARACTERS)
    return start, start + MAX_EVIDENCE_CONTEXT_CHARACTERS


def _candidate_sort_key(candidate: _EvidenceCandidate) -> tuple[object, ...]:
    kind_rank = 0 if candidate.evidence_kind == "announcement_headline" else 1
    return (
        kind_rank,
        "" if candidate.document_id is None else str(candidate.document_id),
        "" if candidate.text_extraction_id is None else str(candidate.text_extraction_id),
        candidate.start_offset,
        candidate.end_offset,
        candidate.rule_code,
        candidate.evidence_fingerprint_sha256,
    )


def _event_fingerprint(
    *,
    announcement: PointInTimeAnnouncement,
    event_type: str,
    source_available_at: datetime,
    ruleset_code: str,
    ruleset_semantic_version: str,
    matched_rule_codes: tuple[str, ...],
    evidence_fingerprints: tuple[str, ...],
) -> str:
    return _canonical_hash(
        {
            "company_id": str(announcement.company_id),
            "security_id": (
                None if announcement.security_id is None else str(announcement.security_id)
            ),
            "announcement_id": str(announcement.id),
            "provider_dataset_id": str(announcement.provider_dataset_id),
            "event_type": event_type,
            "source_event_date": (
                None
                if announcement.announcement_date is None
                else announcement.announcement_date.isoformat()
            ),
            "source_available_at": source_available_at.isoformat(),
            "ruleset_code": ruleset_code,
            "ruleset_semantic_version": ruleset_semantic_version,
            "matched_rule_codes": list(matched_rule_codes),
            "evidence_fingerprints": list(evidence_fingerprints),
        }
    )


def _canonical_hash(payload: dict[str, object]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _aware_utc_or_none(value: datetime | None, *, field: str) -> datetime | None:
    return None if value is None else _aware_utc(value, field=field)


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _database_utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _database_utc(value)


def _result(
    event: BusinessEvent,
    evidence: tuple[BusinessEventEvidence, ...],
    *,
    created: bool,
) -> BusinessEventDetectionResult:
    return BusinessEventDetectionResult(
        id=event.id,
        event_type=event.event_type,
        detection_fingerprint_sha256=event.detection_fingerprint_sha256,
        evidence=tuple(
            BusinessEventEvidenceResult(
                id=item.id,
                evidence_kind=item.evidence_kind,
                rule_code=item.rule_code,
                evidence_fingerprint_sha256=item.evidence_fingerprint_sha256,
            )
            for item in evidence
        ),
        created=created,
    )
