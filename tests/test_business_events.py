"""Phase 6C-A deterministic neutral business-event acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
    BUSINESS_EVENT_TYPE_ORDER,
    BusinessEventRuleEngine,
    BusinessEventRuleMatch,
)
from inflector_core.document_processing import FetchedDocument
from inflector_data.announcement_pit import PointInTimeAnnouncementReader
from inflector_data.archive import LocalRawObjectStore
from inflector_data.business_event_detection import (
    BusinessEventDetectionService,
    BusinessEventIntegrityError,
)
from inflector_data.business_event_pit import PointInTimeBusinessEventReader
from inflector_data.document_extractors import PlainTextExtractor
from inflector_data.document_fetchers import MockDocumentFetcher
from inflector_data.document_services import (
    PAGE_SEPARATOR,
    DocumentAcquisitionService,
    DocumentTextExtractionService,
)
from inflector_data.document_text import (
    DocumentExtractionIdentity,
    DocumentTextReader,
    PointInTimeDocumentText,
)
from inflector_database.business_event_repository import BusinessEventRepository
from inflector_database.models import (
    Announcement,
    AnnouncementDocument,
    BusinessEvent,
    BusinessEventEvidence,
    Company,
    DataProvider,
    Document,
    DocumentTextExtraction,
    IngestionRun,
    ProviderDataset,
    Security,
    SourceRecord,
)

T1 = datetime(2028, 1, 8, 10, tzinfo=UTC)
T2 = datetime(2028, 1, 8, 12, tzinfo=UTC)
DERIVED = datetime(2028, 2, 1, 9, tzinfo=UTC)


def _foundation(session: Session) -> tuple[Company, ProviderDataset, IngestionRun]:
    company = Company(
        legal_name=f"Fictional Event Company {uuid4()}",
        display_name="Fictional Event Company",
        sector="Synthetic",
        industry="Rule testing",
    )
    provider = DataProvider(
        code=f"synthetic_events_{uuid4().hex}",
        provider_type="synthetic",
        licence_name="synthetic-development-only",
    )
    session.add_all((company, provider))
    session.flush()
    dataset = ProviderDataset(
        provider_id=provider.id,
        code="announcements",
        licence_class="synthetic-development-only",
        redistributable=False,
    )
    session.add(dataset)
    session.flush()
    run = IngestionRun(
        provider_dataset_id=dataset.id,
        status="completed",
        started_at=T1,
        finished_at=T2,
    )
    session.add(run)
    session.flush()
    return company, dataset, run


def _source(
    session: Session,
    store: LocalRawObjectStore,
    *,
    dataset: ProviderDataset,
    run: IngestionRun,
    external_id: str,
    available_at: datetime,
    payload: bytes,
) -> SourceRecord:
    archived = store.put(payload)
    source = SourceRecord(
        ingestion_run_id=run.id,
        provider_dataset_id=dataset.id,
        external_record_id=external_id,
        source_uri=f"synthetic://events/{external_id}",
        raw_object_key=archived.object_key,
        raw_payload_reference=f"record:{external_id}",
        raw_content_sha256=archived.content_sha256,
        content_sha256=sha256(payload).hexdigest(),
        retrieved_at=DERIVED,
        reported_at=None,
        published_at=available_at,
        available_at=available_at,
        revision_at=None,
        parse_status="parsed",
        validation_status="accepted",
    )
    session.add(source)
    session.flush()
    return source


def _add_announcement(
    session: Session,
    store: LocalRawObjectStore,
    *,
    company: Company,
    dataset: ProviderDataset,
    run: IngestionRun,
    external_id: str,
    headline: str,
    available_at: datetime,
    document_texts: tuple[str, ...] = (),
    security_id: UUID | None = None,
) -> Announcement:
    source = _source(
        session,
        store,
        dataset=dataset,
        run=run,
        external_id=external_id,
        available_at=available_at,
        payload=f"metadata:{external_id}:{headline}".encode(),
    )
    announcement = Announcement(
        company_id=company.id,
        security_id=security_id,
        provider_dataset_id=dataset.id,
        source_record_id=source.id,
        provider_category="raw_provider_category",
        headline=headline,
        announcement_date=date(2028, 1, 8),
        exchange=None,
        available_at=available_at,
        revision_at=None,
    )
    session.add(announcement)
    session.flush()
    acquisition = DocumentAcquisitionService(session, store)
    extraction = DocumentTextExtractionService(session, store, clock=lambda: DERIVED)
    extractor = PlainTextExtractor()
    for index, text in enumerate(document_texts, start=1):
        document_source = _source(
            session,
            store,
            dataset=dataset,
            run=run,
            external_id=f"{external_id}#document:{index}:{uuid4().hex}",
            available_at=available_at,
            payload=f"document-metadata:{index}".encode(),
        )
        uri = f"synthetic://events/{external_id}/{index}.txt"
        document = Document(
            company_id=company.id,
            security_id=security_id,
            provider_dataset_id=dataset.id,
            source_record_id=document_source.id,
            document_type="exchange_filing",
            title=f"Fictional attachment {index}",
            language="en",
            media_type="text/plain",
            document_uri=uri,
            document_content_sha256=None,
            available_at=available_at,
            revision_at=None,
        )
        session.add(document)
        session.flush()
        session.add(
            AnnouncementDocument(
                announcement_id=announcement.id,
                document_id=document.id,
                role="primary" if index == 1 else "attachment",
            )
        )
        content = text.encode("utf-8")
        fetched = FetchedDocument(
            requested_uri=uri,
            resolved_uri=uri,
            content=content,
            media_type="text/plain",
            retrieved_at=DERIVED,
        )
        asset = acquisition.acquire(
            document_id=document.id,
            fetcher=MockDocumentFetcher({uri: (fetched,)}),
        )
        extraction.extract(document_asset_id=asset.asset_id, extractor=extractor)
    session.flush()
    return announcement


def _selected(
    session: Session,
    *,
    dataset: ProviderDataset,
    external_id: str,
    as_of: datetime,
):
    value = PointInTimeAnnouncementReader(session).announcement_as_of(
        provider_dataset_id=dataset.id,
        external_record_id=external_id,
        as_of=as_of,
    )
    assert value is not None
    return value


def _texts(
    session: Session,
    store: LocalRawObjectStore,
    announcement,
) -> tuple[PointInTimeDocumentText, ...]:
    extractor = PlainTextExtractor()
    identity = DocumentExtractionIdentity(
        extractor.extractor_code,
        extractor.extractor_semantic_version,
        extractor.extractor_runtime_version,
    )
    reader = DocumentTextReader(session, store)
    values = tuple(
        reader.text_for_document(document=document, extraction_identity=identity)
        for document in announcement.documents
    )
    assert all(value is not None for value in values)
    return tuple(value for value in values if value is not None)


def _detect(
    session: Session,
    announcement,
    texts: tuple[PointInTimeDocumentText, ...] = (),
):
    return BusinessEventDetectionService(BusinessEventRepository(session)).detect(
        announcement=announcement,
        document_texts=texts,
        derived_at=DERIVED,
    )


@pytest.mark.parametrize(
    ("event_type", "text"),
    (
        ("order_award", "The company has received an order from a customer."),
        ("capacity_expansion", "The board announced a capacity expansion at the plant."),
        (
            "commercial_commencement",
            "The company commenced commercial production at its fictional unit.",
        ),
        ("capex_announcement", "The board approved capital expenditure for the unit."),
        (
            "acquisition_agreement",
            "The company entered into a share purchase agreement with the seller.",
        ),
        ("regulatory_approval", "The company received approval from US FDA."),
    ),
)
def test_v1_rules_detect_each_exact_neutral_event_type(event_type: str, text: str) -> None:
    matches = BusinessEventRuleEngine().match(text)
    assert {match.event_type for match in matches} == {event_type}
    for match in matches:
        assert text[match.start_offset : match.end_offset]
        assert match.rule_code.endswith("_v1")


@pytest.mark.parametrize(
    "text",
    (
        "The company may receive an order.",
        "This is a potential order opportunity.",
        "The order book increased during the year.",
        "Capacity utilisation improved.",
        "The company expects to commence commercial production.",
        "The company may acquire a company.",
        "The company is exploring acquisition opportunities.",
        "The board approved the proposal.",
        "Approval may be sought from RBI.",
        "Historical capital expenditure was INR 10 crore.",
    ),
)
def test_v1_rules_reject_required_near_misses(text: str) -> None:
    assert BusinessEventRuleEngine().match(text) == ()


def test_headline_and_documents_collapse_to_one_event_with_distinct_evidence(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="MULTI-EVIDENCE",
        headline="Fictional company has received an order",
        available_at=T1,
        document_texts=(
            "Disclosure line: the company received an order.\nUnrelated line.",
            "The company secured a contract from a fictional customer.",
        ),
    )
    announcement = _selected(
        session, dataset=dataset, external_id="MULTI-EVIDENCE", as_of=T1
    )
    first = _detect(session, announcement, _texts(session, store, announcement))
    second = _detect(session, announcement, _texts(session, store, announcement))

    assert len(first) == 1
    assert first[0].event_type == "order_award"
    assert len(first[0].evidence) == 3
    assert first[0].created
    assert not second[0].created
    assert second[0].id == first[0].id
    assert second[0].detection_fingerprint_sha256 == first[0].detection_fingerprint_sha256
    assert [item.evidence_fingerprint_sha256 for item in second[0].evidence] == [
        item.evidence_fingerprint_sha256 for item in first[0].evidence
    ]
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 1
    assert session.scalar(select(func.count()).select_from(BusinessEventEvidence)) == 3


def test_two_types_keep_only_relevant_evidence(session: Session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="TWO-TYPES",
        headline="General fictional disclosure",
        available_at=T1,
        document_texts=(
            "The company received an order.\nThe board approved capex for the plant.",
        ),
    )
    announcement = _selected(session, dataset=dataset, external_id="TWO-TYPES", as_of=T1)
    results = _detect(session, announcement, _texts(session, store, announcement))
    assert [result.event_type for result in results] == ["order_award", "capex_announcement"]
    assert all(len(result.evidence) == 1 for result in results)
    rows = list(session.scalars(select(BusinessEventEvidence)))
    assert {row.rule_code for row in rows} == {
        "order_award_received_order_v1",
        "capex_approved_v1",
    }


def test_document_rules_are_page_local_and_do_not_cross_separator(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="PAGE-LOCAL",
        headline="Neutral disclosure",
        available_at=T1,
        document_texts=("placeholder",),
    )
    announcement = _selected(session, dataset=dataset, external_id="PAGE-LOCAL", as_of=T1)
    original = _texts(session, store, announcement)[0]
    page_one = "The company received an"
    page_two = "order from a fictional customer."
    combined = page_one + PAGE_SEPARATOR + page_two
    archived = store.put(combined.encode())
    extraction = session.get(DocumentTextExtraction, original.extraction.id)
    assert extraction is not None
    extraction.text_object_key = archived.object_key
    extraction.text_sha256 = archived.content_sha256
    extraction.character_count = len(combined)
    extraction.page_count = 2
    extraction.page_map_json = [
        {
            "page_number": 1,
            "start_offset": 0,
            "end_offset": len(page_one),
            "page_text_sha256": sha256(page_one.encode()).hexdigest(),
        },
        {
            "page_number": 2,
            "start_offset": len(page_one) + len(PAGE_SEPARATOR),
            "end_offset": len(combined),
            "page_text_sha256": sha256(page_two.encode()).hexdigest(),
        },
    ]
    session.flush()
    split_text = _texts(session, store, announcement)
    assert _detect(session, announcement, split_text) == ()


def test_headline_only_and_document_only_evidence(session: Session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="HEADLINE-ONLY",
        headline="Company has received an order",
        available_at=T1,
    )
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="DOCUMENT-ONLY",
        headline="General operational update",
        available_at=T1,
        document_texts=("The company has commenced commercial production.",),
    )
    headline = _selected(session, dataset=dataset, external_id="HEADLINE-ONLY", as_of=T1)
    document = _selected(session, dataset=dataset, external_id="DOCUMENT-ONLY", as_of=T1)
    headline_result = _detect(session, headline)
    document_result = _detect(session, document, _texts(session, store, document))
    assert headline_result[0].evidence[0].evidence_kind == "announcement_headline"
    headline_evidence = session.get(
        BusinessEventEvidence,
        headline_result[0].evidence[0].id,
    )
    assert headline_evidence is not None
    assert headline_evidence.start_offset == 0
    assert headline_evidence.end_offset == len(headline.headline)
    assert headline_evidence.excerpt_text == headline.headline
    assert headline_evidence.document_id is None
    assert headline_evidence.page_numbers_json == []
    assert headline_evidence.page_text_sha256s_json == []
    assert document_result[0].event_type == "commercial_commencement"
    assert document_result[0].evidence[0].evidence_kind == "document_text"


def test_non_success_extraction_is_unavailable_not_negative_evidence(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="NO-TEXT",
        headline="Neutral disclosure",
        available_at=T1,
        document_texts=("The company received an order.",),
    )
    announcement = _selected(session, dataset=dataset, external_id="NO-TEXT", as_of=T1)
    original = _texts(session, store, announcement)[0]
    row = session.get(DocumentTextExtraction, original.extraction.id)
    assert row is not None
    row.status = "no_extractable_text"
    row.text_object_key = None
    row.text_sha256 = None
    row.character_count = None
    row.warnings_json = ["no_extractable_text"]
    session.flush()
    unavailable = replace(
        original,
        extraction=replace(
            original.extraction,
            status="no_extractable_text",
            text_object_key=None,
            text_sha256=None,
            character_count=None,
            warnings=("no_extractable_text",),
        ),
        text=None,
        pages=tuple(replace(page, text=None) for page in original.pages),
    )
    assert _detect(session, announcement, (unavailable,)) == ()


def test_correction_removes_old_event_and_type_change_selects_only_new_revision(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="CORRECTION-REMOVE",
        headline="Company has received an order",
        available_at=T1,
    )
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="CORRECTION-REMOVE",
        headline="Company issued a general operational update",
        available_at=T2,
    )
    old = _selected(session, dataset=dataset, external_id="CORRECTION-REMOVE", as_of=T1)
    corrected = _selected(session, dataset=dataset, external_id="CORRECTION-REMOVE", as_of=T2)
    assert _detect(session, old)[0].event_type == "order_award"
    assert _detect(session, corrected) == ()

    reader = PointInTimeBusinessEventReader(session)
    before = reader.company_events_as_of(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        as_of=T1,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )
    after = reader.company_events_as_of(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        as_of=T2,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )
    assert [event.event_type for event in before] == ["order_award"]
    assert after == ()
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 1

    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="CORRECTION-TYPE",
        headline="Company has received an order",
        available_at=T1,
    )
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="CORRECTION-TYPE",
        headline="Company announced a capacity expansion",
        available_at=T2,
    )
    original_type = _selected(session, dataset=dataset, external_id="CORRECTION-TYPE", as_of=T1)
    corrected_type = _selected(session, dataset=dataset, external_id="CORRECTION-TYPE", as_of=T2)
    _detect(session, original_type)
    _detect(session, corrected_type)
    visible = reader.company_events_as_of(
        provider_dataset_id=dataset.id,
        company_id=company.id,
        as_of=T2,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )
    assert [event.event_type for event in visible] == ["capacity_expansion"]


def test_provider_and_announcement_identity_are_not_string_deduplicated(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company_one, dataset_one, run_one = _foundation(session)
    company_two, dataset_two, run_two = _foundation(session)
    for company, dataset, run in (
        (company_one, dataset_one, run_one),
        (company_two, dataset_two, run_two),
    ):
        _add_announcement(
            session,
            store,
            company=company,
            dataset=dataset,
            run=run,
            external_id="123",
            headline="Company has received an order",
            available_at=T1,
        )
    _add_announcement(
        session,
        store,
        company=company_one,
        dataset=dataset_one,
        run=run_one,
        external_id="124",
        headline="Company has received an order",
        available_at=T1,
    )
    selected = (
        _selected(session, dataset=dataset_one, external_id="123", as_of=T1),
        _selected(session, dataset=dataset_two, external_id="123", as_of=T1),
        _selected(session, dataset=dataset_one, external_id="124", as_of=T1),
    )
    identifiers = {_detect(session, announcement)[0].id for announcement in selected}
    assert len(identifiers) == 3


def test_security_pit_api_does_not_gate_on_mutable_security_status(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    security = Security(
        company_id=company.id,
        isin=f"IN{uuid4().hex[:10].upper()}",
        security_type="equity",
        status="inactive",
    )
    session.add(security)
    session.flush()
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="SECURITY-EVENT",
        headline="Company has received an order",
        available_at=T1,
        security_id=security.id,
    )
    announcement = _selected(session, dataset=dataset, external_id="SECURITY-EVENT", as_of=T1)
    _detect(session, announcement)
    events = PointInTimeBusinessEventReader(session).security_events_as_of(
        provider_dataset_id=dataset.id,
        security_id=security.id,
        as_of=T1,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )
    assert [event.event_type for event in events] == ["order_award"]


def test_document_membership_lineage_offsets_hashes_and_operational_time_separation(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="LINEAGE",
        headline="Neutral disclosure",
        available_at=T1,
        document_texts=(
            "First line.\nThe company entered into an agreement to acquire a fictional asset.\n",
        ),
    )
    announcement = _selected(session, dataset=dataset, external_id="LINEAGE", as_of=T1)
    texts = _texts(session, store, announcement)
    _detect(session, announcement, texts)
    event = PointInTimeBusinessEventReader(session).events_for_announcement(
        announcement=announcement,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )[0]
    evidence = event.evidence[0]
    assert event.event_type == "acquisition_agreement"
    assert event.source_available_at == T1
    assert event.derived_at == DERIVED
    assert evidence.source_available_at == T1
    assert evidence.excerpt_text == (
        "The company entered into an agreement to acquire a fictional asset."
    )
    assert sha256(evidence.excerpt_text.encode()).hexdigest() == evidence.excerpt_sha256
    assert evidence.document is not None
    assert evidence.document.source_record.raw_object_key
    assert evidence.document_asset is not None
    assert evidence.text_extraction is not None
    assert evidence.document_asset.object_key != evidence.document.source_record.raw_object_key
    assert evidence.text_extraction.text_object_key
    assert evidence.page_numbers == (1,)
    assert evidence.page_text_sha256s == (texts[0].pages[0].page_text_sha256,)
    assert texts[0].text is not None
    assert texts[0].text[evidence.start_offset : evidence.end_offset] == evidence.excerpt_text

    foreign_company, foreign_dataset, foreign_run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=foreign_company,
        dataset=foreign_dataset,
        run=foreign_run,
        external_id="FOREIGN",
        headline="Neutral",
        available_at=T1,
        document_texts=("The company received an order.",),
    )
    foreign = _selected(session, dataset=foreign_dataset, external_id="FOREIGN", as_of=T1)
    with pytest.raises(BusinessEventIntegrityError, match="does not belong"):
        _detect(session, announcement, _texts(session, store, foreign))


def test_same_ruleset_version_cannot_change_semantics(session: Session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="IMMUTABLE-RULESET",
        headline="Company has received an order",
        available_at=T1,
    )
    announcement = _selected(
        session, dataset=dataset, external_id="IMMUTABLE-RULESET", as_of=T1
    )
    _detect(session, announcement)

    class ChangedRuleset:
        ruleset_code = BUSINESS_EVENT_RULESET_CODE
        ruleset_semantic_version = BUSINESS_EVENT_RULESET_VERSION

        def match(self, text: str) -> tuple[BusinessEventRuleMatch, ...]:
            start = text.lower().index("received")
            return (
                BusinessEventRuleMatch(
                    event_type="order_award",
                    rule_code="changed_rule_without_version_bump_v1",
                    rule_semantic_version="business_event_rule_v1",
                    start_offset=start,
                    end_offset=start + len("received"),
                ),
            )

    with pytest.raises(BusinessEventIntegrityError, match="different event fingerprint"):
        BusinessEventDetectionService(BusinessEventRepository(session)).detect(
            announcement=announcement,
            ruleset=ChangedRuleset(),
            derived_at=DERIVED,
        )


def test_ruleset_identity_and_exact_vocabulary() -> None:
    engine = BusinessEventRuleEngine()
    assert engine.ruleset_code == "business_event_rules"
    assert engine.ruleset_semantic_version == "business_event_rules_v1"
    assert BUSINESS_EVENT_TYPE_ORDER == (
        "order_award",
        "capacity_expansion",
        "commercial_commencement",
        "capex_announcement",
        "acquisition_agreement",
        "regulatory_approval",
    )
