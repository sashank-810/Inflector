"""Phase 6C-A deterministic neutral business-event acceptance tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inflector_core.business_event_quantitative_rules import (
    BUSINESS_EVENT_QUANT_RULESET_CODE,
    BUSINESS_EVENT_QUANT_RULESET_VERSION,
    BusinessEventQuantitativeMatch,
    BusinessEventQuantitativeRuleEngine,
)
from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
    BUSINESS_EVENT_TYPE_ORDER,
    BusinessEventRuleEngine,
    BusinessEventRuleMatch,
)
from inflector_core.document_processing import FetchedDocument
from inflector_core.providers import ProviderMetadata
from inflector_data.announcement_pit import PointInTimeAnnouncementReader
from inflector_data.archive import LocalRawObjectStore
from inflector_data.business_event_detection import (
    BusinessEventDetectionService,
    BusinessEventIntegrityError,
)
from inflector_data.business_event_features import (
    BusinessEventFeaturePrimitives,
    ResolvedQuantitativeObservation,
)
from inflector_data.business_event_pit import PointInTimeBusinessEventReader
from inflector_data.business_event_quantitative import (
    BusinessEventQuantitativeDerivationService,
    BusinessEventQuantitativeIntegrityError,
)
from inflector_data.business_event_quantitative_pit import (
    PointInTimeBusinessEventQuantitativeReader,
)
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
from inflector_data.period_normalization import FiscalQuarterNormalizer
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_data.ttm import TrailingTwelveMonthNormalizer
from inflector_database.business_event_quantitative_repository import (
    BusinessEventQuantitativeRepository,
)
from inflector_database.business_event_repository import BusinessEventRepository
from inflector_database.models import (
    Announcement,
    AnnouncementDocument,
    BusinessEvent,
    BusinessEventEvidence,
    BusinessEventQuantitativeDerivation,
    BusinessEventQuantitativeFact,
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
FIXTURES = Path(__file__).parent / "fixtures"


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


def _pit_event(session: Session, announcement, event_type: str):
    events = PointInTimeBusinessEventReader(session).events_for_announcement(
        announcement=announcement,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )
    return next(event for event in events if event.event_type == event_type)


def _derive_quant(session: Session, store: LocalRawObjectStore, event):
    return BusinessEventQuantitativeDerivationService(
        BusinessEventQuantitativeRepository(session), store
    ).derive(event=event, derived_at=DERIVED)


@pytest.mark.parametrize(
    ("text", "value", "scale", "currency", "normalized"),
    (
        ("order worth ₹250 crore", "250", "crore", "INR", "2500000000"),
        ("order valued at INR 1,250 crore", "1250", "crore", "INR", "12500000000"),
        ("contract value of Rs. 75 lakh", "75", "lakh", "INR", "7500000"),
        ("purchase order of INR 10 million", "10", "million", "INR", "10000000"),
        ("contract valued at USD 10 million", "10", "million", "USD", "10000000"),
        ("order worth INR 12,34,567", "1234567", "unit", "INR", "1234567"),
        ("order worth INR 1,234,567", "1234567", "unit", "INR", "1234567"),
    ),
)
def test_quantitative_order_money_is_exact_decimal(
    text: str, value: str, scale: str, currency: str, normalized: str
) -> None:
    matches = BusinessEventQuantitativeRuleEngine().match(
        event_type="order_award", text=text
    )
    assert len(matches) == 1
    match = matches[0]
    assert match.fact_code == "order_value"
    assert match.reported_value == Decimal(value)
    assert match.reported_scale == scale
    assert match.reported_currency == currency
    assert match.normalized_value == Decimal(normalized)
    assert match.normalized_unit == "currency_major"
    assert text[match.local_start_offset : match.local_end_offset] == match.raw_text


@pytest.mark.parametrize(
    "text",
    (
        "Company received an order. Its total order book is ₹5,000 crore.",
        "Company received an order. Company revenue is ₹500 crore.",
        "potential order of ₹100 crore",
        "order worth approximately ₹100 crore",
        "order worth ₹100-120 crore",
        "order worth INR 1,23,4,567",
    ),
)
def test_quantitative_order_money_rejects_unrelated_ambiguous_or_malformed_values(
    text: str,
) -> None:
    assert BusinessEventQuantitativeRuleEngine().match(
        event_type="order_award", text=text
    ) == ()


def test_quantitative_capex_values_and_negatives() -> None:
    engine = BusinessEventQuantitativeRuleEngine()
    first = engine.match(event_type="capex_announcement", text="approved capex of ₹500 crore")
    second = engine.match(
        event_type="capex_announcement",
        text="capital expenditure plan of INR 2.5 billion",
    )
    assert first[0].normalized_value == Decimal("5000000000")
    assert second[0].normalized_value == Decimal("2500000000.0")
    for text in (
        "historical capital expenditure was ₹500 crore",
        "capex may be approximately ₹500 crore",
        "approved proposal unrelated to capex worth ₹500 crore",
    ):
        assert engine.match(event_type="capex_announcement", text=text) == ()


@pytest.mark.parametrize(
    ("text", "before", "after", "unit"),
    (
        ("increase capacity from 1 MTPA to 1.5 MTPA", "1000000", "1500000.0", "tonnes_per_annum"),
        (
            "capacity will increase from 1000 KTPA to 1.5 MTPA",
            "1000000",
            "1500000.0",
            "tonnes_per_annum",
        ),
        ("expanded capacity from 100 MW to 150 MW", "100", "150", "megawatt"),
        ("capacity increased from 1 GW to 1500 MW", "1000", "1500", "megawatt"),
    ),
)
def test_quantitative_capacity_pairs_normalize_without_derived_delta(
    text: str, before: str, after: str, unit: str
) -> None:
    matches = BusinessEventQuantitativeRuleEngine().match(
        event_type="capacity_expansion", text=text
    )
    assert [value.fact_code for value in matches] == ["capacity_before", "capacity_after"]
    assert matches[0].normalized_value == Decimal(before)
    assert matches[1].normalized_value == Decimal(after)
    assert {value.normalized_unit for value in matches} == {unit}
    assert all(value.fact_code != "additional_capacity" for value in matches)


def test_quantitative_capacity_additional_is_explicit_and_incompatible_pair_is_rejected() -> None:
    engine = BusinessEventQuantitativeRuleEngine()
    first = engine.match(
        event_type="capacity_expansion", text="additional capacity of 500 KTPA"
    )
    second = engine.match(
        event_type="capacity_expansion", text="capacity expansion by 50 MW"
    )
    assert first[0].fact_code == "additional_capacity"
    assert first[0].normalized_value == Decimal("500000")
    assert second[0].normalized_value == Decimal("50")
    assert engine.match(
        event_type="capacity_expansion", text="increase capacity from 1 MTPA to 100 MW"
    ) == ()


def test_quantitative_acquisition_consideration_and_stake() -> None:
    engine = BusinessEventQuantitativeRuleEngine()
    matches = engine.match(
        event_type="acquisition_agreement",
        text="agreed to acquire a 51% stake for a consideration of ₹250 crore",
    )
    by_code = {value.fact_code: value for value in matches}
    assert by_code["acquisition_stake_fraction"].reported_value == Decimal("51")
    assert by_code["acquisition_stake_fraction"].normalized_value == Decimal("0.51")
    assert by_code["acquisition_consideration"].normalized_value == Decimal("2500000000")
    assert engine.match(
        event_type="acquisition_agreement", text="agreed to acquire a 101% stake"
    ) == ()


@pytest.mark.parametrize(
    "raw",
    ("15 September 2026", "15 Sep 2026", "September 15, 2026", "Sep 15, 2026", "2026-09-15"),
)
def test_quantitative_commencement_dates_are_strict(raw: str) -> None:
    text = f"commercial production commenced on {raw}"
    matches = BusinessEventQuantitativeRuleEngine().match(
        event_type="commercial_commencement", text=text
    )
    assert len(matches) == 1
    assert matches[0].date_value == date(2026, 9, 15)
    assert matches[0].raw_text == raw
    assert matches[0].reported_value is None


@pytest.mark.parametrize("raw", ("15/09/2026", "09/15/2026", "next month", "Q3 FY27"))
def test_quantitative_commencement_dates_reject_ambiguous_formats(raw: str) -> None:
    assert BusinessEventQuantitativeRuleEngine().match(
        event_type="commercial_commencement",
        text=f"commercial production commenced on {raw}",
    ) == ()


def test_full_document_pipeline_produces_citable_order_value(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    disclosure = "Fictional Engineering Limited has received an order worth ₹250 crore."
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="QUANT-PIPELINE",
        headline="Fictional operational disclosure",
        available_at=T1,
        document_texts=(disclosure,),
    )
    announcement = _selected(session, dataset=dataset, external_id="QUANT-PIPELINE", as_of=T1)
    _detect(session, announcement, _texts(session, store, announcement))
    event = _pit_event(session, announcement, "order_award")
    result = _derive_quant(session, store, event)
    reread = PointInTimeBusinessEventQuantitativeReader(session).facts_for_event(
        event=event,
        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
    )
    assert result.available_fact_codes == ("order_value",)
    assert reread is not None and len(reread.facts) == 1
    fact = reread.facts[0]
    assert fact.reported_value == Decimal("250")
    assert fact.reported_scale == "crore"
    assert fact.reported_currency == "INR"
    assert fact.normalized_value == Decimal("2500000000")
    assert fact.normalized_unit == "currency_major"
    assert fact.raw_text == "₹250 crore"
    assert disclosure[fact.start_offset : fact.end_offset] == fact.raw_text
    assert fact.source_available_at == T1
    assert reread.source_available_at == T1
    assert reread.derived_at == DERIVED


def test_zero_fact_derivation_persists_and_is_idempotent(session: Session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="ZERO-FACT",
        headline="Fictional Engineering Limited has received an order.",
        available_at=T1,
    )
    announcement = _selected(session, dataset=dataset, external_id="ZERO-FACT", as_of=T1)
    _detect(session, announcement)
    event = _pit_event(session, announcement, "order_award")
    first = _derive_quant(session, store, event)
    second = _derive_quant(session, store, event)
    assert first.created and not second.created
    assert first.id == second.id
    assert first.derivation_fingerprint_sha256 == second.derivation_fingerprint_sha256
    assert first.facts == second.facts == ()
    assert session.scalar(
        select(func.count()).select_from(BusinessEventQuantitativeDerivation)
    ) == 1
    assert session.scalar(select(func.count()).select_from(BusinessEventQuantitativeFact)) == 0


def test_quantity_outside_event_evidence_context_is_intentionally_invisible(
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
        external_id="NO-WHOLE-DOCUMENT",
        headline="Neutral disclosure",
        available_at=T1,
        document_texts=("Company has received an order.\nValue: INR 500 crore.",),
    )
    announcement = _selected(
        session, dataset=dataset, external_id="NO-WHOLE-DOCUMENT", as_of=T1
    )
    _detect(session, announcement, _texts(session, store, announcement))
    event = _pit_event(session, announcement, "order_award")
    result = _derive_quant(session, store, event)
    assert result.facts == ()


def test_cross_evidence_equal_values_remain_independent_observations(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session)
    text = "Company has received an order worth ₹250 crore."
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="REPEATED-VALUE",
        headline=text,
        available_at=T1,
        document_texts=(text,),
    )
    announcement = _selected(session, dataset=dataset, external_id="REPEATED-VALUE", as_of=T1)
    _detect(session, announcement, _texts(session, store, announcement))
    event = _pit_event(session, announcement, "order_award")
    result = _derive_quant(session, store, event)
    assert len(result.facts) == 2
    facts = list(session.scalars(select(BusinessEventQuantitativeFact)))
    assert {fact.normalized_value for fact in facts} == {Decimal("2500000000")}
    assert len({fact.business_event_evidence_id for fact in facts}) == 2


def test_multiple_explicit_order_values_are_observations_not_a_total() -> None:
    matches = BusinessEventQuantitativeRuleEngine().match(
        event_type="order_award",
        text="order worth ₹100 crore and contract worth ₹200 crore",
    )
    assert [value.normalized_value for value in matches] == [
        Decimal("1000000000"),
        Decimal("2000000000"),
    ]


def test_quantitative_corrections_change_remove_or_empty_only_selected_revision(
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
        external_id="VALUE-CHANGE",
        headline="Company has received an order worth ₹100 crore.",
        available_at=T1,
    )
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="VALUE-CHANGE",
        headline="Company has received an order worth ₹120 crore.",
        available_at=T2,
    )
    original = _selected(session, dataset=dataset, external_id="VALUE-CHANGE", as_of=T1)
    corrected = _selected(session, dataset=dataset, external_id="VALUE-CHANGE", as_of=T2)
    _detect(session, original)
    _detect(session, corrected)
    original_event = _pit_event(session, original, "order_award")
    corrected_event = _pit_event(session, corrected, "order_award")
    _derive_quant(session, store, original_event)
    _derive_quant(session, store, corrected_event)
    quant_reader = PointInTimeBusinessEventQuantitativeReader(session)
    original_facts = quant_reader.facts_for_event(
        event=original_event,
        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
    )
    corrected_facts = quant_reader.facts_for_event(
        event=corrected_event,
        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
    )
    assert original_facts is not None and corrected_facts is not None
    assert original_facts.facts[0].normalized_value == Decimal("1000000000")
    assert corrected_facts.facts[0].normalized_value == Decimal("1200000000")

    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="VALUE-REMOVED",
        headline="Company has received an order worth ₹100 crore.",
        available_at=T1,
    )
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="VALUE-REMOVED",
        headline="Company has received an order.",
        available_at=T2,
    )
    value_old = _selected(session, dataset=dataset, external_id="VALUE-REMOVED", as_of=T1)
    value_new = _selected(session, dataset=dataset, external_id="VALUE-REMOVED", as_of=T2)
    _detect(session, value_old)
    _detect(session, value_new)
    value_old_event = _pit_event(session, value_old, "order_award")
    value_new_event = _pit_event(session, value_new, "order_award")
    _derive_quant(session, store, value_old_event)
    empty = _derive_quant(session, store, value_new_event)
    assert empty.facts == ()

    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="EVENT-REMOVED",
        headline="Company has received an order worth ₹100 crore.",
        available_at=T1,
    )
    _add_announcement(
        session,
        store,
        company=company,
        dataset=dataset,
        run=run,
        external_id="EVENT-REMOVED",
        headline="Company issued a general operational update.",
        available_at=T2,
    )
    removed_old = _selected(session, dataset=dataset, external_id="EVENT-REMOVED", as_of=T1)
    removed_new = _selected(session, dataset=dataset, external_id="EVENT-REMOVED", as_of=T2)
    _detect(session, removed_old)
    assert _detect(session, removed_new) == ()
    removed_event = _pit_event(session, removed_old, "order_award")
    _derive_quant(session, store, removed_event)
    assert PointInTimeBusinessEventReader(session).events_for_announcement(
        announcement=removed_new,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    ) == ()
    assert session.scalar(
        select(func.count()).select_from(BusinessEventQuantitativeDerivation)
    ) == 5


def test_quantitative_same_version_semantic_drift_fails_closed(
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
        external_id="QUANT-IMMUTABLE",
        headline="Company has received an order worth ₹100 crore",
        available_at=T1,
    )
    announcement = _selected(session, dataset=dataset, external_id="QUANT-IMMUTABLE", as_of=T1)
    _detect(session, announcement)
    event = _pit_event(session, announcement, "order_award")
    _derive_quant(session, store, event)

    class ChangedRuleset:
        ruleset_code = BUSINESS_EVENT_QUANT_RULESET_CODE
        ruleset_semantic_version = BUSINESS_EVENT_QUANT_RULESET_VERSION

        def match(
            self, *, event_type: str, text: str
        ) -> tuple[BusinessEventQuantitativeMatch, ...]:
            del event_type
            start = text.index("₹100 crore")
            return (
                BusinessEventQuantitativeMatch(
                    fact_code="order_value",
                    fact_kind="monetary",
                    rule_code="changed_without_version_bump_v1",
                    rule_semantic_version="business_event_quantitative_rule_v1",
                    local_start_offset=start,
                    local_end_offset=start + len("₹100 crore"),
                    raw_text="₹100 crore",
                    reported_value=Decimal("100"),
                    reported_scale="crore",
                    reported_unit=None,
                    reported_currency="INR",
                    normalized_value=Decimal("1000000000"),
                    normalized_unit="currency_major",
                    date_value=None,
                    warnings=(),
                ),
            )

    with pytest.raises(BusinessEventQuantitativeIntegrityError, match="different derivation"):
        BusinessEventQuantitativeDerivationService(
            BusinessEventQuantitativeRepository(session), store
        ).derive(event=event, ruleset=ChangedRuleset(), derived_at=DERIVED)


def test_full_event_feature_pipeline_uses_event_time_ttm_and_preserves_lineage(
    session: Session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    service = IngestionService(session, store)
    universe_metadata = ProviderMetadata(
        "event_feature_universe", "csv", "universe", "synthetic-development-only"
    )
    financial_metadata = ProviderMetadata(
        "event_feature_financials", "csv", "financials", "synthetic-development-only"
    )
    service.ingest_universe(
        CSVUniverseProvider(
            FIXTURES / "universe_synthetic.csv", universe_metadata, DERIVED
        )
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "business_event_feature_financials_synthetic.csv",
            financial_metadata,
            DERIVED,
        )
    )
    service.ingest_financials(
        CSVFinancialsProvider(
            FIXTURES / "business_event_feature_financials_restatement_synthetic.csv",
            financial_metadata,
            datetime(2028, 3, 1, tzinfo=UTC),
        )
    )
    company = session.scalar(
        select(Company).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    financial_dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(
            DataProvider.code == financial_metadata.provider_code,
            ProviderDataset.code == financial_metadata.dataset_code,
        )
    )
    assert company is not None and financial_dataset_id is not None

    event_provider = DataProvider(
        code=f"event_feature_announcements_{uuid4().hex}",
        provider_type="synthetic",
        licence_name="synthetic-development-only",
    )
    session.add(event_provider)
    session.flush()
    event_dataset = ProviderDataset(
        provider_id=event_provider.id,
        code="announcements",
        licence_class="synthetic-development-only",
        redistributable=False,
    )
    session.add(event_dataset)
    session.flush()
    event_run = IngestionRun(
        provider_dataset_id=event_dataset.id,
        status="completed",
        started_at=T1,
        finished_at=T2,
    )
    session.add(event_run)
    session.flush()
    disclosure = "Fictional Engineering Limited has received an order worth ₹250 crore."
    _add_announcement(
        session,
        store,
        company=company,
        dataset=event_dataset,
        run=event_run,
        external_id="EVENT-FEATURE-PIPELINE",
        headline="Fictional operational disclosure",
        available_at=T1,
        document_texts=(disclosure,),
    )
    announcement = _selected(
        session,
        dataset=event_dataset,
        external_id="EVENT-FEATURE-PIPELINE",
        as_of=T1,
    )
    _detect(session, announcement, _texts(session, store, announcement))
    event = _pit_event(session, announcement, "order_award")
    _derive_quant(session, store, event)
    quantitative = PointInTimeBusinessEventQuantitativeReader(session).facts_for_event(
        event=event,
        ruleset_code=BUSINESS_EVENT_QUANT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_QUANT_RULESET_VERSION,
    )
    assert quantitative is not None

    ttm_normalizer = TrailingTwelveMonthNormalizer(
        FiscalQuarterNormalizer(PointInTimeFinancialReader(session))
    )
    later_ttms = ttm_normalizer.ttm_series_as_of(
        provider_dataset_id=financial_dataset_id,
        company_id=company.id,
        filing_scope="standalone",
        metric_code="revenue",
        as_of=datetime(2028, 3, 1, tzinfo=UTC),
    )
    assert later_ttms[-1].value == Decimal("9000000000")

    bundle = BusinessEventFeaturePrimitives(ttm_normalizer).features_as_of(
        event=event,
        quantitative_derivation=quantitative,
        financial_provider_dataset_id=financial_dataset_id,
        filing_scope="standalone",
        as_of=datetime(2028, 3, 1, tzinfo=UTC),
    )
    assert bundle.materiality_financial_cutoff == T1
    assert bundle.event_time_ttm_revenue is not None
    assert bundle.event_time_ttm_revenue.value == Decimal("10000000000")
    assert bundle.order_value_to_ttm_revenue.value == Decimal("0.25")
    assert len(bundle.event_time_ttm_revenue.lineage) == 4
    assert all(
        component.quarter.lineage[0].fact.source_record.raw_object_key
        for component in bundle.event_time_ttm_revenue.lineage
    )
    observation = bundle.order_value_to_ttm_revenue.evidence[0]
    assert isinstance(observation, ResolvedQuantitativeObservation)
    assert observation.value == Decimal("2500000000")
    assert observation.facts == quantitative.facts
    assert bundle.order_value_to_ttm_revenue.available_at == T1
