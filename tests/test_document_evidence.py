"""Phase 6B immutable bytes, deterministic text, and citation acceptance tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import func, select

from inflector_core.document_processing import (
    DocumentFetchError,
    FetchedDocument,
)
from inflector_data.announcement_pit import (
    PointInTimeAnnouncementReader,
    PointInTimeDocument,
)
from inflector_data.archive import (
    InvalidObjectKeyError,
    LocalRawObjectStore,
    ObjectNotFoundError,
    ObjectStoreIntegrityError,
)
from inflector_data.document_extractors import PlainTextExtractor, PyPdfTextExtractor
from inflector_data.document_fetchers import LocalFileDocumentFetcher, MockDocumentFetcher
from inflector_data.document_services import (
    PAGE_SEPARATOR,
    DocumentAcquisitionService,
    DocumentTextExtractionService,
)
from inflector_data.document_text import (
    DocumentExtractionIdentity,
    DocumentTextReader,
)
from inflector_data.pit import SourceRecordView
from inflector_database.models import (
    Announcement,
    AnnouncementDocument,
    Company,
    DataProvider,
    Document,
    DocumentAsset,
    DocumentTextExtraction,
    IngestionRun,
    ProviderDataset,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
T1 = datetime(2027, 9, 1, 10, tzinfo=UTC)
T2 = datetime(2027, 9, 1, 12, tzinfo=UTC)
T3 = datetime(2027, 9, 1, 14, tzinfo=UTC)


def _foundation(
    session, store: LocalRawObjectStore
) -> tuple[Company, ProviderDataset, IngestionRun]:
    company = Company(
        legal_name=f"Fictional Document Company {uuid4()}",
        display_name="Fictional Document Company",
        sector="Synthetic",
        industry="Testing",
    )
    provider = DataProvider(
        code=f"synthetic_documents_{uuid4().hex}",
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
        finished_at=T1,
    )
    session.add(run)
    session.flush()
    store.put(b"fictional provider announcement metadata")
    return company, dataset, run


def _source(
    session,
    *,
    store: LocalRawObjectStore,
    dataset: ProviderDataset,
    run: IngestionRun,
    external_id: str,
    available_at: datetime,
    content: bytes,
) -> SourceRecord:
    archived = store.put(content)
    source = SourceRecord(
        ingestion_run_id=run.id,
        provider_dataset_id=dataset.id,
        external_record_id=external_id,
        source_uri=f"synthetic://provider/{external_id}",
        raw_object_key=archived.object_key,
        raw_payload_reference=f"record:{external_id}",
        raw_content_sha256=archived.content_sha256,
        content_sha256=sha256(content).hexdigest(),
        retrieved_at=T3,
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


def _document(
    session,
    *,
    store: LocalRawObjectStore,
    media_type: str | None = "text/plain",
    provider_hash: str | None = None,
    uri: str = "synthetic://documents/evidence.txt",
    available_at: datetime = T1,
) -> tuple[Document, SourceRecord]:
    company, dataset, run = _foundation(session, store)
    source = _source(
        session,
        store=store,
        dataset=dataset,
        run=run,
        external_id=f"DOC-SOURCE-{uuid4().hex}",
        available_at=available_at,
        content=f"metadata:{uri}".encode(),
    )
    document = Document(
        company_id=company.id,
        security_id=None,
        provider_dataset_id=dataset.id,
        source_record_id=source.id,
        document_type="exchange_filing",
        title="Fictional source document",
        language="en",
        media_type=media_type,
        document_uri=uri,
        document_content_sha256=provider_hash,
        available_at=available_at,
        revision_at=None,
    )
    session.add(document)
    session.flush()
    return document, source


def _fetched(uri: str, content: bytes, *, retrieved_at: datetime = T2) -> FetchedDocument:
    return FetchedDocument(
        requested_uri=uri,
        resolved_uri=f"local://resolved/{sha256(content).hexdigest()}",
        content=content,
        media_type=None,
        retrieved_at=retrieved_at,
    )


def _pit_document(document: Document, source: SourceRecord) -> PointInTimeDocument:
    return PointInTimeDocument(
        id=document.id,
        company_id=document.company_id,
        security_id=document.security_id,
        provider_dataset_id=document.provider_dataset_id,
        document_type=document.document_type,
        title=document.title,
        language=document.language,
        media_type=document.media_type,
        document_uri=document.document_uri,
        document_content_sha256=document.document_content_sha256,
        role="primary",
        available_at=document.available_at.replace(tzinfo=UTC),
        revision_at=None,
        ingested_at=document.ingested_at.replace(tzinfo=UTC),
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


def _identity(extractor: PlainTextExtractor | PyPdfTextExtractor) -> DocumentExtractionIdentity:
    return DocumentExtractionIdentity(
        extractor.extractor_code,
        extractor.extractor_semantic_version,
        extractor.extractor_runtime_version,
    )


def _blank_pdf(*, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    if encrypted:
        writer.encrypt("synthetic-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _partially_empty_pdf() -> bytes:
    source = PdfReader(BytesIO((FIXTURES / "document_two_page_synthetic.pdf").read_bytes()))
    writer = PdfWriter()
    writer.append(source)
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_local_fetcher_root_size_and_object_store_key_safety(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "notice.txt").write_bytes(b"fictional text")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    fetcher = LocalFileDocumentFetcher(root, clock=lambda: T2)

    fetched = fetcher.fetch(uri="notice.txt", max_bytes=100)
    assert fetched.content == b"fictional text"
    assert fetched.retrieved_at == T2
    with pytest.raises(DocumentFetchError, match="escapes"):
        fetcher.fetch(uri="../outside.txt", max_bytes=100)
    with pytest.raises(DocumentFetchError, match="escapes"):
        fetcher.fetch(uri=str(outside.resolve()), max_bytes=100)
    with pytest.raises(DocumentFetchError, match="scheme"):
        fetcher.fetch(uri="https://example.invalid/notice.txt", max_bytes=100)
    with pytest.raises(DocumentFetchError, match="exceeds"):
        fetcher.fetch(uri="notice.txt", max_bytes=5)

    store = LocalRawObjectStore(tmp_path / "objects")
    archived = store.put(b"exact immutable bytes")
    assert store.get(archived.object_key) == b"exact immutable bytes"
    with pytest.raises(InvalidObjectKeyError):
        store.get("../outside.txt")
    with pytest.raises(InvalidObjectKeyError):
        store.get(f"sha256/ff/{archived.content_sha256}")
    with pytest.raises(ObjectNotFoundError):
        store.get(f"sha256/aa/{'a' * 64}")


def test_provider_hash_verification_mismatch_empty_and_exact_rerun(session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    matching = b"provider verified fictional text"
    document, _ = _document(
        session,
        store=store,
        provider_hash=sha256(matching).hexdigest(),
    )
    fetcher = MockDocumentFetcher(
        {
            document.document_uri: (
                _fetched(document.document_uri, matching, retrieved_at=T2),
                _fetched(document.document_uri, matching, retrieved_at=T3),
            )
        }
    )
    service = DocumentAcquisitionService(session, store)

    first = service.acquire(document_id=document.id, fetcher=fetcher, max_bytes=1024)
    second = service.acquire(document_id=document.id, fetcher=fetcher, max_bytes=1024)

    assert first.status == "verified"
    assert first.created and not second.created
    assert first.asset_id == second.asset_id
    assert first.retrieved_at == second.retrieved_at == T2
    assert session.scalar(select(func.count()).select_from(DocumentAsset)) == 1

    mismatch_document, _ = _document(
        session,
        store=store,
        provider_hash="f" * 64,
        uri="synthetic://documents/mismatch.txt",
    )
    mismatch = service.acquire(
        document_id=mismatch_document.id,
        fetcher=MockDocumentFetcher(
            {
                mismatch_document.document_uri: (
                    _fetched(mismatch_document.document_uri, b"different exact bytes"),
                )
            }
        ),
    )
    assert mismatch.status == "rejected_hash_mismatch"
    assert mismatch.warnings == ("document_hash_mismatch",)

    empty_document, _ = _document(
        session,
        store=store,
        provider_hash=None,
        uri="synthetic://documents/empty.txt",
    )
    empty = service.acquire(
        document_id=empty_document.id,
        fetcher=MockDocumentFetcher(
            {empty_document.document_uri: (_fetched(empty_document.document_uri, b""),)}
        ),
    )
    assert empty.status == "invalid_empty_content"
    assert "empty_document_content" in empty.warnings
    assert store.get(empty.object_key) == b""


def test_no_hash_mutable_uri_conflict_preserves_first_accepted_asset(
    session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    document, _ = _document(session, store=store, provider_hash=None)
    fetcher = MockDocumentFetcher(
        {
            document.document_uri: (
                _fetched(document.document_uri, b"first immutable content", retrieved_at=T2),
                _fetched(document.document_uri, b"changed mutable content", retrieved_at=T3),
            )
        }
    )
    service = DocumentAcquisitionService(session, store)

    accepted = service.acquire(document_id=document.id, fetcher=fetcher)
    conflict = service.acquire(document_id=document.id, fetcher=fetcher)

    assert accepted.status == "accepted_unverified"
    assert conflict.status == "rejected_content_conflict"
    assert conflict.warnings == ("document_content_changed_without_metadata_revision",)
    assert store.get(accepted.object_key) == b"first immutable content"
    assert store.get(conflict.object_key) == b"changed mutable content"
    assert session.scalar(select(func.count()).select_from(DocumentAsset)) == 2


def test_media_detection_uses_bytes_and_retains_declared_mismatch(session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    fake_pdf, _ = _document(
        session,
        store=store,
        media_type="application/pdf",
        uri="synthetic://documents/not-really.pdf",
    )
    fake = DocumentAcquisitionService(session, store).acquire(
        document_id=fake_pdf.id,
        fetcher=MockDocumentFetcher(
            {fake_pdf.document_uri: (_fetched(fake_pdf.document_uri, b"not a PDF"),)}
        ),
    )
    assert fake.declared_media_type == "application/pdf"
    assert fake.detected_media_type == "application/octet-stream"
    assert fake.warnings == ("declared_pdf_content_mismatch",)

    pdf_bytes = (FIXTURES / "document_two_page_synthetic.pdf").read_bytes()
    real_pdf, _ = _document(
        session,
        store=store,
        media_type=None,
        uri="synthetic://documents/no-extension",
    )
    real = DocumentAcquisitionService(session, store).acquire(
        document_id=real_pdf.id,
        fetcher=MockDocumentFetcher(
            {real_pdf.document_uri: (_fetched(real_pdf.document_uri, pdf_bytes),)}
        ),
    )
    assert real.detected_media_type == "application/pdf"


def test_plain_text_extraction_offsets_hashes_citations_and_time_separation(
    session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    document, source = _document(session, store=store, media_type="text/plain")
    raw_text = b"Fictional line one.\r\nLine two.\rLine three."
    asset = DocumentAcquisitionService(session, store).acquire(
        document_id=document.id,
        fetcher=MockDocumentFetcher(
            {document.document_uri: (_fetched(document.document_uri, raw_text),)}
        ),
    )
    extractor = PlainTextExtractor()
    extraction_service = DocumentTextExtractionService(session, store, clock=lambda: T3)

    first = extraction_service.extract(document_asset_id=asset.asset_id, extractor=extractor)
    second = extraction_service.extract(document_asset_id=asset.asset_id, extractor=extractor)

    expected = "Fictional line one.\nLine two.\nLine three."
    assert first.status == "success"
    assert asset.detected_media_type == "text/plain"
    assert first.created and not second.created
    assert first.extraction_id == second.extraction_id
    assert first.character_count == len(expected)
    assert first.text_sha256 == sha256(expected.encode()).hexdigest()
    assert first.page_map == (
        {
            "page_number": 1,
            "start_offset": 0,
            "end_offset": len(expected),
            "page_text_sha256": sha256(expected.encode()).hexdigest(),
        },
    )
    assert store.get(first.text_object_key or "") == expected.encode()

    pit_document = _pit_document(document, source)
    reader = DocumentTextReader(session, store)
    text = reader.text_for_document(
        document=pit_document,
        extraction_identity=_identity(extractor),
    )
    assert text is not None
    assert text.text == expected
    assert text.source_available_at == T1
    assert text.asset.retrieved_at == T2
    assert text.extraction.extracted_at == T3
    page = reader.page_evidence(
        document=pit_document,
        extraction_identity=_identity(extractor),
        page_number=1,
    )
    assert page.excerpt == expected
    assert page.page_numbers == (1,)
    excerpt = reader.excerpt_evidence(
        document=pit_document,
        extraction_identity=_identity(extractor),
        start_offset=10,
        end_offset=18,
    )
    assert excerpt.excerpt == expected[10:18]
    with pytest.raises(ValueError, match="offsets"):
        reader.excerpt_evidence(
            document=pit_document,
            extraction_identity=_identity(extractor),
            start_offset=0,
            end_offset=len(expected) + 1,
        )


def test_real_two_page_pdf_exact_composition_offsets_and_hashes(session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    pdf_bytes = (FIXTURES / "document_two_page_synthetic.pdf").read_bytes()
    document, source = _document(
        session,
        store=store,
        media_type="application/pdf",
        provider_hash=sha256(pdf_bytes).hexdigest(),
        uri="synthetic://documents/two-page.pdf",
    )
    asset = DocumentAcquisitionService(session, store).acquire(
        document_id=document.id,
        fetcher=MockDocumentFetcher(
            {document.document_uri: (_fetched(document.document_uri, pdf_bytes),)}
        ),
    )
    extractor = PyPdfTextExtractor()
    extraction = DocumentTextExtractionService(session, store, clock=lambda: T3).extract(
        document_asset_id=asset.asset_id,
        extractor=extractor,
    )
    page_one = "Fictional disclosure page one.\nDeterministic source evidence only.\n"
    page_two = "Fictional disclosure page two.\nNo catalyst meaning is implied.\n"
    expected = page_one + PAGE_SEPARATOR + page_two

    assert asset.status == "verified"
    assert extraction.status == "success"
    assert extraction.page_count == 2
    assert extraction.character_count == len(expected)
    assert extraction.text_sha256 == sha256(expected.encode()).hexdigest()
    assert extraction.page_map[0] == {
        "page_number": 1,
        "start_offset": 0,
        "end_offset": len(page_one),
        "page_text_sha256": sha256(page_one.encode()).hexdigest(),
    }
    assert extraction.page_map[1] == {
        "page_number": 2,
        "start_offset": len(page_one) + len(PAGE_SEPARATOR),
        "end_offset": len(expected),
        "page_text_sha256": sha256(page_two.encode()).hexdigest(),
    }
    assert store.get(extraction.text_object_key or "").decode() == expected

    evidence = DocumentTextReader(session, store).page_evidence(
        document=_pit_document(document, source),
        extraction_identity=_identity(extractor),
        page_number=2,
    )
    assert evidence.excerpt == page_two
    assert evidence.start_offset == len(page_one) + len(PAGE_SEPARATOR)
    assert evidence.asset.content_sha256 == sha256(pdf_bytes).hexdigest()
    assert evidence.document.source_record.raw_object_key != evidence.asset.object_key


def test_no_text_encrypted_and_unsupported_documents_are_explicit(session, tmp_path: Path) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    acquisition = DocumentAcquisitionService(session, store)
    extraction = DocumentTextExtractionService(session, store, clock=lambda: T3)
    extractor = PyPdfTextExtractor()

    blank = _blank_pdf()
    blank_document, _ = _document(
        session,
        store=store,
        media_type="application/pdf",
        uri="synthetic://documents/blank.pdf",
    )
    blank_asset = acquisition.acquire(
        document_id=blank_document.id,
        fetcher=MockDocumentFetcher(
            {blank_document.document_uri: (_fetched(blank_document.document_uri, blank),)}
        ),
    )
    blank_result = extraction.extract(document_asset_id=blank_asset.asset_id, extractor=extractor)
    assert blank_result.status == "no_extractable_text"
    assert blank_result.warnings == ("no_extractable_text",)
    assert blank_result.text_object_key is None

    partial = _partially_empty_pdf()
    partial_document, _ = _document(
        session,
        store=store,
        media_type="application/pdf",
        uri="synthetic://documents/partially-empty.pdf",
    )
    partial_asset = acquisition.acquire(
        document_id=partial_document.id,
        fetcher=MockDocumentFetcher(
            {partial_document.document_uri: (_fetched(partial_document.document_uri, partial),)}
        ),
    )
    partial_result = extraction.extract(
        document_asset_id=partial_asset.asset_id,
        extractor=extractor,
    )
    assert partial_result.status == "success"
    assert partial_result.page_count == 3
    assert partial_result.warnings == ("empty_pdf_page",)
    assert partial_result.page_map[2]["start_offset"] == partial_result.page_map[2]["end_offset"]
    assert partial_result.page_map[2]["page_text_sha256"] == sha256(b"").hexdigest()

    encrypted = _blank_pdf(encrypted=True)
    encrypted_document, _ = _document(
        session,
        store=store,
        media_type="application/pdf",
        uri="synthetic://documents/encrypted.pdf",
    )
    encrypted_asset = acquisition.acquire(
        document_id=encrypted_document.id,
        fetcher=MockDocumentFetcher(
            {
                encrypted_document.document_uri: (
                    _fetched(encrypted_document.document_uri, encrypted),
                )
            }
        ),
    )
    encrypted_result = extraction.extract(
        document_asset_id=encrypted_asset.asset_id,
        extractor=extractor,
    )
    assert encrypted_result.status == "extraction_failed"
    assert encrypted_result.warnings == ("encrypted_or_unreadable_pdf",)

    binary_document, _ = _document(
        session,
        store=store,
        media_type="application/octet-stream",
        uri="synthetic://documents/binary.bin",
    )
    binary_asset = acquisition.acquire(
        document_id=binary_document.id,
        fetcher=MockDocumentFetcher(
            {binary_document.document_uri: (_fetched(binary_document.document_uri, b"\x00\x01"),)}
        ),
    )
    binary_result = extraction.extract(document_asset_id=binary_asset.asset_id, extractor=extractor)
    assert binary_result.status == "unsupported_media_type"
    assert binary_result.text_object_key is None


def test_rejected_asset_cannot_extract_and_object_corruption_fails_closed(
    session, tmp_path: Path
) -> None:
    store_root = tmp_path / "objects"
    store = LocalRawObjectStore(store_root)
    rejected_document, _ = _document(
        session,
        store=store,
        provider_hash="f" * 64,
    )
    rejected = DocumentAcquisitionService(session, store).acquire(
        document_id=rejected_document.id,
        fetcher=MockDocumentFetcher(
            {rejected_document.document_uri: (_fetched(rejected_document.document_uri, b"text"),)}
        ),
    )
    with pytest.raises(ValueError, match="not extractable"):
        DocumentTextExtractionService(session, store).extract(
            document_asset_id=rejected.asset_id,
            extractor=PlainTextExtractor(),
        )

    document, _ = _document(
        session,
        store=store,
        uri="synthetic://documents/corrupt.txt",
    )
    accepted = DocumentAcquisitionService(session, store).acquire(
        document_id=document.id,
        fetcher=MockDocumentFetcher(
            {document.document_uri: (_fetched(document.document_uri, b"original"),)}
        ),
    )
    (store_root / accepted.object_key).write_bytes(b"corrupted")
    with pytest.raises(ObjectStoreIntegrityError, match="SHA-256"):
        DocumentTextExtractionService(session, store).extract(
            document_asset_id=accepted.asset_id,
            extractor=PlainTextExtractor(),
        )


def test_text_reader_detects_corrupted_text_object(session, tmp_path: Path) -> None:
    root = tmp_path / "objects"
    store = LocalRawObjectStore(root)
    document, source = _document(session, store=store)
    asset = DocumentAcquisitionService(session, store).acquire(
        document_id=document.id,
        fetcher=MockDocumentFetcher(
            {document.document_uri: (_fetched(document.document_uri, b"canonical text"),)}
        ),
    )
    extractor = PlainTextExtractor()
    result = DocumentTextExtractionService(session, store).extract(
        document_asset_id=asset.asset_id,
        extractor=extractor,
    )
    assert result.text_object_key is not None
    (root / result.text_object_key).write_bytes(b"corrupted text")
    with pytest.raises(ObjectStoreIntegrityError, match="SHA-256"):
        DocumentTextReader(session, store).text_for_document(
            document=_pit_document(document, source),
            extraction_identity=_identity(extractor),
        )


def test_historical_announcement_revision_selects_only_its_document_text(
    session, tmp_path: Path
) -> None:
    store = LocalRawObjectStore(tmp_path / "objects")
    company, dataset, run = _foundation(session, store)
    announcement_source_a = _source(
        session,
        store=store,
        dataset=dataset,
        run=run,
        external_id="CORRECTED-ANNOUNCEMENT",
        available_at=T1,
        content=b"announcement revision A",
    )
    announcement_source_b = _source(
        session,
        store=store,
        dataset=dataset,
        run=run,
        external_id="CORRECTED-ANNOUNCEMENT",
        available_at=T2,
        content=b"announcement revision B",
    )
    document_source_a = _source(
        session,
        store=store,
        dataset=dataset,
        run=run,
        external_id="CORRECTED-ANNOUNCEMENT#document:A",
        available_at=T1,
        content=b"document metadata A",
    )
    document_source_b = _source(
        session,
        store=store,
        dataset=dataset,
        run=run,
        external_id="CORRECTED-ANNOUNCEMENT#document:B",
        available_at=T2,
        content=b"document metadata B",
    )
    announcement_a = Announcement(
        company_id=company.id,
        security_id=None,
        provider_dataset_id=dataset.id,
        source_record_id=announcement_source_a.id,
        provider_category="raw_notice",
        headline="Original fictional notice",
        announcement_date=date(2027, 9, 1),
        exchange=None,
        available_at=T1,
        revision_at=None,
    )
    announcement_b = Announcement(
        company_id=company.id,
        security_id=None,
        provider_dataset_id=dataset.id,
        source_record_id=announcement_source_b.id,
        provider_category="raw_notice",
        headline="Corrected fictional notice",
        announcement_date=date(2027, 9, 1),
        exchange=None,
        available_at=T2,
        revision_at=None,
    )
    document_a = Document(
        company_id=company.id,
        security_id=None,
        provider_dataset_id=dataset.id,
        source_record_id=document_source_a.id,
        document_type="exchange_filing",
        title="Document A",
        language="en",
        media_type="text/plain",
        document_uri="synthetic://documents/A.txt",
        document_content_sha256=None,
        available_at=T1,
        revision_at=None,
    )
    document_b = Document(
        company_id=company.id,
        security_id=None,
        provider_dataset_id=dataset.id,
        source_record_id=document_source_b.id,
        document_type="exchange_filing",
        title="Document B",
        language="en",
        media_type="text/plain",
        document_uri="synthetic://documents/B.txt",
        document_content_sha256=None,
        available_at=T2,
        revision_at=None,
    )
    session.add_all((announcement_a, announcement_b, document_a, document_b))
    session.flush()
    session.add_all(
        (
            AnnouncementDocument(
                announcement_id=announcement_a.id,
                document_id=document_a.id,
                role="primary",
            ),
            AnnouncementDocument(
                announcement_id=announcement_b.id,
                document_id=document_b.id,
                role="primary",
            ),
        )
    )
    session.flush()

    acquisition = DocumentAcquisitionService(session, store)
    extractor = PlainTextExtractor()
    extraction = DocumentTextExtractionService(session, store, clock=lambda: T3)
    revisions = (
        (document_a, b"Only document A text."),
        (document_b, b"Only document B text."),
    )
    for document, content in revisions:
        asset = acquisition.acquire(
            document_id=document.id,
            fetcher=MockDocumentFetcher(
                {
                    document.document_uri: (
                        _fetched(document.document_uri, content, retrieved_at=T3),
                    )
                }
            ),
        )
        extraction.extract(document_asset_id=asset.asset_id, extractor=extractor)

    announcement_reader = PointInTimeAnnouncementReader(session)
    selected_a = announcement_reader.announcement_as_of(
        provider_dataset_id=dataset.id,
        external_record_id="CORRECTED-ANNOUNCEMENT",
        as_of=datetime(2027, 9, 1, 11, tzinfo=UTC),
    )
    selected_b = announcement_reader.announcement_as_of(
        provider_dataset_id=dataset.id,
        external_record_id="CORRECTED-ANNOUNCEMENT",
        as_of=T2,
    )
    assert selected_a is not None and selected_b is not None
    assert selected_a.documents[0].id == document_a.id
    assert selected_b.documents[0].id == document_b.id

    text_reader = DocumentTextReader(session, store)
    text_a = text_reader.text_for_document(
        document=selected_a.documents[0],
        extraction_identity=_identity(extractor),
    )
    text_b = text_reader.text_for_document(
        document=selected_b.documents[0],
        extraction_identity=_identity(extractor),
    )
    assert text_a is not None and text_b is not None
    assert text_a.text == "Only document A text."
    assert text_b.text == "Only document B text."
    assert text_a.source_available_at == T1
    assert text_b.source_available_at == T2
    assert text_a.asset.retrieved_at == text_b.asset.retrieved_at == T3
    assert text_a.extraction.extracted_at == text_b.extraction.extracted_at == T3
    assert text_a.document.source_record.raw_object_key
    assert text_a.asset.object_key != text_a.document.source_record.raw_object_key
    assert session.scalar(select(func.count()).select_from(DocumentTextExtraction)) == 2
