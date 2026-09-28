"""Phase 6A announcement/document source-evidence acceptance tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from inflector_core.providers import (
    AnnouncementDocumentRecord,
    AnnouncementProvider,
    AnnouncementRecord,
    IngestionEnvelope,
    ProviderBatch,
    ProviderMetadata,
)
from inflector_data.announcement_pit import PointInTimeAnnouncementReader
from inflector_data.archive import LocalRawObjectStore
from inflector_data.providers import CSVAnnouncementProvider, MockAnnouncementProvider
from inflector_data.service import IngestionService
from inflector_database.models import (
    Announcement,
    AnnouncementDocument,
    Company,
    DataProvider,
    DataQualityIssue,
    Document,
    IngestionRun,
    ProviderDataset,
    Security,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2027, 8, 1, 13, tzinfo=UTC)
T1 = datetime(2027, 8, 1, 10, tzinfo=UTC)
T2 = datetime(2027, 8, 1, 12, tzinfo=UTC)
ALPHA_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ALPHA_SECURITY_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
BETA_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
BETA_SECURITY_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
METADATA = ProviderMetadata(
    "synthetic_announcements",
    "synthetic_csv",
    "announcements",
    "synthetic-development-only",
)


def _identities(session) -> None:
    alpha = Company(
        id=ALPHA_ID,
        legal_name="Fictional Alpha Limited",
        display_name="Fictional Alpha",
        sector="Synthetic",
        industry="Testing",
    )
    beta = Company(
        id=BETA_ID,
        legal_name="Fictional Beta Limited",
        display_name="Fictional Beta",
        sector="Synthetic",
        industry="Testing",
    )
    session.add_all((alpha, beta))
    session.flush()
    session.add_all(
        (
            Security(
                id=ALPHA_SECURITY_ID,
                company_id=alpha.id,
                isin="INE000SYN001",
                security_type="equity",
                status="inactive",
            ),
            Security(
                id=BETA_SECURITY_ID,
                company_id=beta.id,
                isin="INE000SYN002",
                security_type="equity",
                status="inactive",
            ),
        )
    )
    session.commit()


def _dataset_id(session, provider_code: str = "synthetic_announcements") -> UUID:
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(
            DataProvider.code == provider_code,
            ProviderDataset.code == "announcements",
        )
    )
    assert value is not None
    return value


def _document(
    uri: str,
    *,
    role: str = "primary",
    content_hash: str | None = None,
) -> AnnouncementDocumentRecord:
    return AnnouncementDocumentRecord(
        document_type="exchange_filing",
        title=f"Synthetic document {uri.rsplit('/', 1)[-1]}",
        language="en",
        media_type="application/pdf",
        document_uri=uri,
        document_content_sha256=content_hash,
        role=role,
    )


def _record(
    headline: str,
    *documents: AnnouncementDocumentRecord,
    company: str | None = "Fictional Alpha Limited",
    isin: str | None = "INE000SYN001",
    announcement_date: date | None = date(2027, 8, 1),
    parse_errors: tuple[str, ...] = (),
) -> AnnouncementRecord:
    return AnnouncementRecord(
        company_legal_name=company,
        security_isin=isin,
        provider_category="raw_provider_notice",
        headline=headline,
        announcement_date=announcement_date,
        exchange="NSE",
        documents=tuple(documents),
        parse_errors=parse_errors,
    )


def _envelope(
    external_id: str,
    record: AnnouncementRecord,
    *,
    available_at: datetime | None,
    revision_at: datetime | None = None,
    metadata: ProviderMetadata = METADATA,
    salt: str = "",
) -> IngestionEnvelope[AnnouncementRecord]:
    content_hash = sha256(
        f"{external_id}|{record.headline}|{salt}|{record.documents}".encode()
    ).hexdigest()
    return IngestionEnvelope(
        provider=metadata,
        external_record_id=external_id,
        source_uri=f"synthetic://announcements/{external_id}",
        raw_payload_reference=f"record:{external_id}:{salt}",
        content_sha256=content_hash,
        retrieved_at=RETRIEVED_AT,
        record=record,
        available_at=available_at,
        revision_at=revision_at,
    )


def _batch(
    *envelopes: IngestionEnvelope[AnnouncementRecord],
    metadata: ProviderMetadata = METADATA,
    payload: bytes = b"explicit fictional announcement batch",
) -> ProviderBatch[AnnouncementRecord]:
    return ProviderBatch(
        provider=metadata,
        source_uri="synthetic://announcements/batch",
        raw_payload=payload,
        retrieved_at=RETRIEVED_AT,
        records=tuple(envelopes),
    )


def test_csv_announcement_ingestion_groups_documents_archives_and_is_idempotent(
    session, tmp_path: Path
) -> None:
    _identities(session)
    archive_root = tmp_path / "raw"
    service = IngestionService(session, LocalRawObjectStore(archive_root))
    provider = CSVAnnouncementProvider(
        FIXTURES / "announcements_synthetic.csv", METADATA, RETRIEVED_AT
    )
    port: AnnouncementProvider = provider

    first = service.ingest_announcements(port)
    duplicate = service.ingest_announcements(provider)

    assert (first.records_received, first.records_accepted) == (3, 3)
    assert duplicate.records_duplicated == 3
    assert session.scalar(select(func.count()).select_from(Announcement)) == 3
    assert session.scalar(select(func.count()).select_from(Document)) == 3
    assert session.scalar(select(func.count()).select_from(AnnouncementDocument)) == 3
    assert session.scalar(select(func.count()).select_from(SourceRecord)) == 6
    board = session.scalar(
        select(Announcement)
        .join(SourceRecord, Announcement.source_record_id == SourceRecord.id)
        .where(SourceRecord.external_record_id == "SYN-ANN-BOARD-001")
    )
    assert board is not None
    assert board.security_id is None
    assert board.headline == "Board meeting outcome"
    sources = list(session.scalars(select(SourceRecord)))
    assert all(source.raw_object_key for source in sources)
    assert all((archive_root / source.raw_object_key).exists() for source in sources)
    documents = list(session.scalars(select(Document).order_by(Document.title)))
    assert any(document.document_content_sha256 is None for document in documents)
    assert all(document.available_at == board.available_at for document in documents[:2])


def test_announcement_persistence_failure_rolls_back_and_marks_run_failed(
    session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))

    def fail_document(**_kwargs: object) -> None:
        raise RuntimeError("synthetic document persistence failure")

    monkeypatch.setattr(service._repository, "add_document", fail_document)
    with pytest.raises(RuntimeError, match="synthetic document persistence failure"):
        service.ingest_announcements(
            CSVAnnouncementProvider(
                FIXTURES / "announcements_synthetic.csv", METADATA, RETRIEVED_AT
            )
        )

    run = session.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()))
    assert run is not None
    assert run.status == "failed"
    assert (run.records_received, run.records_accepted, run.records_quarantined) == (3, 0, 0)
    assert session.scalar(select(func.count()).select_from(Announcement)) == 0
    assert session.scalar(select(func.count()).select_from(Document)) == 0
    assert session.scalar(select(func.count()).select_from(AnnouncementDocument)) == 0


def test_pit_correction_replaces_document_set_and_preserves_historical_cutoff(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_announcements(
        CSVAnnouncementProvider(FIXTURES / "announcements_synthetic.csv", METADATA, RETRIEVED_AT)
    )
    service.ingest_announcements(
        CSVAnnouncementProvider(
            FIXTURES / "announcements_correction_synthetic.csv",
            METADATA,
            RETRIEVED_AT,
        )
    )
    reader = PointInTimeAnnouncementReader(session)
    dataset_id = _dataset_id(session)

    original = reader.announcement_as_of(
        provider_dataset_id=dataset_id,
        external_record_id="SYN-ANN-BOARD-001",
        as_of=datetime(2027, 8, 1, 11, tzinfo=UTC),
    )
    corrected = reader.announcement_as_of(
        provider_dataset_id=dataset_id,
        external_record_id="SYN-ANN-BOARD-001",
        as_of=T2,
    )
    historical = reader.announcement_as_of(
        provider_dataset_id=dataset_id,
        external_record_id="SYN-ANN-BOARD-001",
        as_of=datetime(2027, 8, 1, 16, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    )

    assert original is not None and corrected is not None and historical is not None
    assert original.headline == historical.headline == "Board meeting outcome"
    assert original.id == historical.id
    assert [document.document_uri for document in original.documents] == [
        "synthetic://documents/board-outcome.pdf",
        "synthetic://documents/board-annexure.pdf",
    ]
    assert corrected.headline == "Corrected board meeting outcome"
    assert [document.document_uri for document in corrected.documents] == [
        "synthetic://documents/board-outcome-corrected.pdf"
    ]
    assert corrected.documents[0].available_at == T2
    assert corrected.source_record.raw_object_key
    assert corrected.documents[0].source_record.raw_payload_reference is not None
    assert corrected.documents[0].source_record.external_record_id.startswith(
        "SYN-ANN-BOARD-001#document:1:"
    )


def test_ambiguous_revision_is_quarantined_without_overwriting_prior_revision(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_announcements(
        CSVAnnouncementProvider(FIXTURES / "announcements_synthetic.csv", METADATA, RETRIEVED_AT)
    )
    result = service.ingest_announcements(
        CSVAnnouncementProvider(
            FIXTURES / "announcements_ambiguous_synthetic.csv",
            METADATA,
            RETRIEVED_AT,
        )
    )

    assert result.records_quarantined == 1
    issue = session.scalar(
        select(DataQualityIssue).where(
            DataQualityIssue.rule_code == "ambiguous_announcement_revision"
        )
    )
    assert issue is not None
    assert session.scalar(select(func.count()).select_from(Announcement)) == 3
    dataset_id = _dataset_id(session)
    selected = PointInTimeAnnouncementReader(session).announcement_as_of(
        provider_dataset_id=dataset_id,
        external_record_id="SYN-ANN-BOARD-001",
        as_of=T2,
    )
    assert selected is not None and selected.headline == "Board meeting outcome"


def test_precise_validation_rules_quarantine_without_partial_normalization(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    result = service.ingest_announcements(
        CSVAnnouncementProvider(
            FIXTURES / "announcements_invalid_synthetic.csv",
            METADATA,
            RETRIEVED_AT,
        )
    )

    assert (result.records_received, result.records_quarantined) == (10, 10)
    assert session.scalar(select(func.count()).select_from(Announcement)) == 0
    assert session.scalar(select(func.count()).select_from(Document)) == 0
    codes = set(session.scalars(select(DataQualityIssue.rule_code)))
    assert {
        "missing_company_identity",
        "unknown_company",
        "unknown_security",
        "security_company_mismatch",
        "missing_headline",
        "missing_available_at",
        "invalid_announcement_date",
        "invalid_document_role",
        "invalid_document_sha256",
        "missing_document_uri",
    }.issubset(codes)


def test_provider_isolation_same_headline_distinct_ids_and_no_status_gating(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    p1 = METADATA
    p2 = ProviderMetadata(
        "synthetic_announcements_two",
        "synthetic_mock",
        "announcements",
        "synthetic-development-only",
    )
    shared_headline = "Identical fictional disclosure headline"
    p1_batch = _batch(
        _envelope("123", _record(shared_headline), available_at=T1, metadata=p1),
        _envelope("124", _record(shared_headline), available_at=T1, metadata=p1),
        metadata=p1,
        payload=b"fictional provider one",
    )
    p2_batch = _batch(
        _envelope("123", _record(shared_headline), available_at=T1, metadata=p2),
        metadata=p2,
        payload=b"fictional provider two",
    )
    service.ingest_announcements(MockAnnouncementProvider(p1_batch))
    service.ingest_announcements(MockAnnouncementProvider(p2_batch))

    p1_id = _dataset_id(session)
    p2_id = _dataset_id(session, "synthetic_announcements_two")
    reader = PointInTimeAnnouncementReader(session)
    p1_values = reader.company_announcements_as_of(
        provider_dataset_id=p1_id,
        company_id=ALPHA_ID,
        as_of=T2,
    )
    p2_values = reader.company_announcements_as_of(
        provider_dataset_id=p2_id,
        company_id=ALPHA_ID,
        as_of=T2,
    )
    security_values = reader.security_announcements_as_of(
        provider_dataset_id=p1_id,
        security_id=ALPHA_SECURITY_ID,
        as_of=T2,
    )

    assert {value.external_record_id for value in p1_values} == {"123", "124"}
    assert {value.external_record_id for value in p2_values} == {"123"}
    assert {value.external_record_id for value in security_values} == {"123", "124"}
    assert all(value.headline == shared_headline for value in (*p1_values, *p2_values))


def test_company_security_date_filters_undated_behavior_and_time_validation(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_announcements(
        CSVAnnouncementProvider(FIXTURES / "announcements_synthetic.csv", METADATA, RETRIEVED_AT)
    )
    reader = PointInTimeAnnouncementReader(session)
    dataset_id = _dataset_id(session)

    all_company = reader.company_announcements_as_of(
        provider_dataset_id=dataset_id,
        company_id=ALPHA_ID,
        as_of=datetime(2027, 8, 4, tzinfo=UTC),
    )
    filtered = reader.company_announcements_as_of(
        provider_dataset_id=dataset_id,
        company_id=ALPHA_ID,
        as_of=datetime(2027, 8, 4, tzinfo=UTC),
        start_date=date(2027, 8, 2),
        end_date=date(2027, 8, 2),
    )
    security = reader.security_announcements_as_of(
        provider_dataset_id=dataset_id,
        security_id=ALPHA_SECURITY_ID,
        as_of=datetime(2027, 8, 4, tzinfo=UTC),
    )

    assert {value.external_record_id for value in all_company} == {
        "SYN-ANN-BOARD-001",
        "SYN-ANN-SEC-001",
        "SYN-ANN-UNDATED-001",
    }
    assert [value.external_record_id for value in filtered] == ["SYN-ANN-SEC-001"]
    assert [value.external_record_id for value in security] == ["SYN-ANN-SEC-001"]
    with pytest.raises(ValueError, match="timezone-aware"):
        reader.company_announcements_as_of(
            provider_dataset_id=dataset_id,
            company_id=ALPHA_ID,
            as_of=datetime(2027, 8, 4),
        )
    with pytest.raises(ValueError, match="start_date"):
        reader.company_announcements_as_of(
            provider_dataset_id=dataset_id,
            company_id=ALPHA_ID,
            as_of=T2,
            start_date=date(2027, 8, 3),
            end_date=date(2027, 8, 2),
        )


def test_economic_date_filter_applies_after_revision_selection(session, tmp_path: Path) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    first = _envelope(
        "DATE-CORRECTION",
        _record("Original dated disclosure", announcement_date=date(2027, 8, 1)),
        available_at=T1,
        salt="original",
    )
    corrected = _envelope(
        "DATE-CORRECTION",
        _record("Corrected dated disclosure", announcement_date=date(2027, 8, 3)),
        available_at=T2,
        salt="corrected",
    )
    service.ingest_announcements(MockAnnouncementProvider(_batch(first, payload=b"original")))
    service.ingest_announcements(MockAnnouncementProvider(_batch(corrected, payload=b"corrected")))

    selected = PointInTimeAnnouncementReader(session).company_announcements_as_of(
        provider_dataset_id=_dataset_id(session),
        company_id=ALPHA_ID,
        as_of=T2,
        start_date=date(2027, 8, 1),
        end_date=date(2027, 8, 1),
    )

    assert selected == []


def test_document_uri_is_not_identity_and_document_hash_is_never_invented(
    session, tmp_path: Path
) -> None:
    _identities(session)
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    common_uri = "synthetic://documents/reused-location.pdf"
    batch = _batch(
        _envelope(
            "URI-ONE",
            _record("First disclosure", _document(common_uri)),
            available_at=T1,
            salt="one",
        ),
        _envelope(
            "URI-TWO",
            _record("Second disclosure", _document(common_uri)),
            available_at=T1,
            salt="two",
        ),
    )
    service.ingest_announcements(MockAnnouncementProvider(batch))

    documents = list(session.scalars(select(Document)))
    assert len(documents) == 2
    assert {document.document_uri for document in documents} == {common_uri}
    assert all(document.document_content_sha256 is None for document in documents)
    assert len({document.source_record_id for document in documents}) == 2
