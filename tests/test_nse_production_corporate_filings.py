"""Official NSE action, announcement, document, and catalyst activation tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import func, select

from inflector_core.business_event_rules import (
    BUSINESS_EVENT_RULESET_CODE,
    BUSINESS_EVENT_RULESET_VERSION,
)
from inflector_core.document_processing import DocumentFetchError
from inflector_core.providers import IngestionEnvelope, ProviderMetadata, UniverseRecord
from inflector_data.announcement_pit import PointInTimeAnnouncementReader
from inflector_data.archive import LocalRawObjectStore
from inflector_data.business_event_detection import BusinessEventDetectionService
from inflector_data.business_event_pit import PointInTimeBusinessEventReader
from inflector_data.business_event_quantitative import (
    BusinessEventQuantitativeDerivationService,
)
from inflector_data.document_extractors import PyPdfTextExtractor
from inflector_data.document_fetchers import NSEOfficialDocumentFetcher
from inflector_data.document_services import (
    DocumentAcquisitionService,
    DocumentTextExtractionService,
)
from inflector_data.document_text import DocumentExtractionIdentity, DocumentTextReader
from inflector_data.nse_corporate_filings import (
    NSE_ANNOUNCEMENT_DATASET_CODE,
    NSE_CORPORATE_ACTION_DATASET_CODE,
    NSE_CORPORATE_ACTION_PURPOSE_RULES_VERSION,
    NSEAnnouncementProvider,
    NSECorporateActionProvider,
    NSECorporateFilingFormatError,
    NSEEquityIdentity,
    _parse_action_purpose,
    nse_announcements_url,
    nse_corporate_actions_url,
    nse_equity_identities,
)
from inflector_data.nse_http import AcquiredNSEArtifact, NSEArtifactTooLargeError
from inflector_data.service import IngestionService
from inflector_database.business_event_quantitative_repository import (
    BusinessEventQuantitativeRepository,
)
from inflector_database.business_event_repository import BusinessEventRepository
from inflector_database.models import (
    Announcement,
    Company,
    CorporateAction,
    DataProvider,
    Document,
    ExchangeListing,
    ProviderDataset,
    Security,
    SourceRecord,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
LATER = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
ISIN = "INE000SYN001"
SYMBOL = "FICALPHA"
ACTION_URI = nse_corporate_actions_url(date(2026, 10, 1), date(2026, 10, 31))
ANNOUNCEMENT_URI = nse_announcements_url(date(2026, 10, 1), date(2026, 10, 1))
PDF_URI = "https://nsearchives.nseindia.com/corporate/FICALPHA_ORDER.pdf"


class _StaticSource:
    def __init__(self, uri: str, payload: bytes, retrieved_at: datetime = RETRIEVED) -> None:
        self.source_uri = uri
        self._artifact = AcquiredNSEArtifact(uri, payload, retrieved_at)

    def acquire(self) -> AcquiredNSEArtifact:
        return self._artifact


class _DocumentClient:
    def __init__(self, artifact: AcquiredNSEArtifact | Exception) -> None:
        self.artifact = artifact
        self.calls: list[tuple[str, int | None]] = []

    def acquire(
        self,
        uri: str,
        *,
        maximum_response_bytes: int | None = None,
        warm_up: bool = True,
    ) -> AcquiredNSEArtifact:
        del warm_up
        self.calls.append((uri, maximum_response_bytes))
        if isinstance(self.artifact, Exception):
            raise self.artifact
        return self.artifact


def _metadata(code: str) -> ProviderMetadata:
    return ProviderMetadata(
        "nse_official",
        "https",
        code,
        "official-source-terms-reviewed-locally",
        redistributable=False,
    )


def _identities() -> dict[str, NSEEquityIdentity]:
    return {SYMBOL: NSEEquityIdentity(SYMBOL, ISIN, "Fictional Alpha Limited")}


def _seed_identity(session) -> tuple[Company, Security]:
    company = Company(
        legal_name="Fictional Alpha Limited",
        display_name="Fictional Alpha",
        sector="",
        industry="",
    )
    security = Security(
        company=company,
        isin=ISIN,
        security_type="equity",
        status="active",
    )
    ExchangeListing(
        security=security,
        exchange="NSE",
        symbol=SYMBOL,
        valid_from=date(2020, 1, 1),
        valid_to=None,
        status="active",
    )
    session.add(company)
    session.commit()
    return company, security


def _dataset_id(session, code: str):
    value = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider, ProviderDataset.provider_id == DataProvider.id)
        .where(DataProvider.code == "nse_official", ProviderDataset.code == code)
    )
    assert value is not None
    return value


def _text_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream.set_data(f"BT /F1 11 Tf 40 740 Td ({escaped}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_action_rules_are_anchored_exact_and_decimal_safe() -> None:
    assert NSE_CORPORATE_ACTION_PURPOSE_RULES_VERSION.endswith("_v1")
    assert _parse_action_purpose("Dividend - Rs 2.50 Per Share", face_value="10") == (
        "cash_dividend",
        None,
        None,
        Decimal("2.50"),
        None,
    )
    bonus = _parse_action_purpose("Bonus 2:1", face_value="10")
    assert bonus is not None and bonus[:3] == ("bonus", 2, 1)
    split = _parse_action_purpose(
        "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
        face_value="2",
    )
    assert split is not None and split[:3] == ("split", 5, 1)
    assert _parse_action_purpose("Rights 1:4 @ Premium Rs 8/-", face_value="10") == (
        "rights",
        1,
        4,
        None,
        Decimal("18"),
    )
    assert _parse_action_purpose("Rights 1:4 @ Premium Rs 8/-", face_value="") is None
    assert (
        _parse_action_purpose(
            "Dividend - Rs 3 Per Share/Special Dividend - Re 0.50 Per Share",
            face_value="10",
        )
        is None
    )
    assert _parse_action_purpose("Demerger", face_value="10") is None


def test_action_provider_mapping_skips_unsupported_and_uses_observed_time() -> None:
    payload = (FIXTURES / "nse_corporate_actions_fictional.json").read_bytes()
    provider = NSECorporateActionProvider(
        cast(object, _StaticSource(ACTION_URI, payload)),  # type: ignore[arg-type]
        _metadata(NSE_CORPORATE_ACTION_DATASET_CODE),
        _identities(),
    )
    batch = provider.fetch_corporate_actions()
    assert batch.raw_payload == payload
    assert provider.skipped_rows == 4
    assert provider.supported_action_counts == {
        "cash_dividend": 2,
        "bonus": 1,
        "split": 1,
        "rights": 1,
    }
    records = [value.record for value in batch.records]
    assert records[0].cash_amount == Decimal("2.50")
    assert records[1].ratio_numerator == 2
    assert records[2].ratio_numerator == 5
    assert records[3].subscription_price == Decimal("18")
    assert all(value.available_at == RETRIEVED for value in batch.records)
    assert all(value.revision_at is None for value in batch.records)
    assert records[2].effective_date == records[2].ex_date
    assert records[4].parse_errors == ("invalid_ex_date",)


def test_universe_identity_map_is_exact_and_rejects_conflicts() -> None:
    record = UniverseRecord(
        "Fictional Alpha Limited",
        "Fictional Alpha Limited",
        "",
        "",
        ISIN,
        "equity",
        "active",
        "NSE",
        SYMBOL,
        "active",
        date(2020, 1, 1),
        None,
    )
    envelope = IngestionEnvelope(
        _metadata("nse_equity_universe"),
        "universe-1",
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
        "row-2",
        "0" * 64,
        RETRIEVED,
        record,
        available_at=RETRIEVED,
    )
    assert nse_equity_identities((envelope,))[SYMBOL].isin == ISIN
    conflicting = IngestionEnvelope(
        envelope.provider,
        "universe-2",
        envelope.source_uri,
        "row-3",
        "1" * 64,
        RETRIEVED,
        UniverseRecord(
            "Other Fictional Limited",
            "Other Fictional Limited",
            "",
            "",
            "INE000SYN002",
            "equity",
            "active",
            "NSE",
            SYMBOL,
            "active",
            date(2021, 1, 1),
            None,
        ),
        available_at=RETRIEVED,
    )
    with pytest.raises(NSECorporateFilingFormatError, match="conflicting"):
        nse_equity_identities((envelope, conflicting))


def test_action_ingestion_is_idempotent_and_archives_exact_bytes(session, tmp_path: Path) -> None:
    _seed_identity(session)
    payload = (FIXTURES / "nse_corporate_actions_fictional.json").read_bytes()
    provider = NSECorporateActionProvider(
        cast(object, _StaticSource(ACTION_URI, payload)),  # type: ignore[arg-type]
        _metadata(NSE_CORPORATE_ACTION_DATASET_CODE),
        _identities(),
    )
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    first = service.ingest_corporate_actions(provider)
    repeat = service.ingest_corporate_actions(provider)
    assert first.records_accepted == 4
    assert first.records_quarantined == 1
    assert repeat.records_duplicated == 5
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 4
    source = session.scalar(select(SourceRecord))
    assert source is not None
    assert (tmp_path / "raw" / source.raw_object_key).read_bytes() == payload


def test_announcement_provider_preserves_raw_metadata_and_zero_fake_hashes() -> None:
    payload = (FIXTURES / "nse_announcements_fictional.json").read_bytes()
    provider = NSEAnnouncementProvider(
        cast(object, _StaticSource(ANNOUNCEMENT_URI, payload)),  # type: ignore[arg-type]
        _metadata(NSE_ANNOUNCEMENT_DATASET_CODE),
        _identities(),
        max_announcements=10,
    )
    batch = provider.fetch_announcements()
    assert batch.raw_payload == payload
    assert len(batch.records) == 2
    first = batch.records[0]
    assert first.external_record_id == "nse-announcement:900000001"
    assert first.record.security_isin == ISIN
    assert first.record.provider_category == "Award of order"
    assert first.record.announcement_date == date(2026, 10, 1)
    assert first.available_at == RETRIEVED
    assert first.record.documents[0].document_uri == PDF_URI
    assert first.record.documents[0].document_content_sha256 is None
    assert first.raw_payload_reference == "json:[0]:seq_id:900000001"


def test_production_corrections_append_and_identity_mismatch_quarantines(
    session, tmp_path: Path
) -> None:
    _seed_identity(session)
    store = LocalRawObjectStore(tmp_path / "raw")
    service = IngestionService(session, store)
    action_payload = (FIXTURES / "nse_corporate_actions_fictional.json").read_bytes()
    first_actions = NSECorporateActionProvider(
        cast(object, _StaticSource(ACTION_URI, action_payload)),  # type: ignore[arg-type]
        _metadata(NSE_CORPORATE_ACTION_DATASET_CODE),
        _identities(),
    )
    service.ingest_corporate_actions(first_actions)
    corrected_actions = NSECorporateActionProvider(
        cast(
            object,
            _StaticSource(
                ACTION_URI,
                action_payload.replace(b"Rs 2.50", b"Rs 3.00"),
                LATER,
            ),
        ),  # type: ignore[arg-type]
        _metadata(NSE_CORPORATE_ACTION_DATASET_CODE),
        _identities(),
    )
    correction = service.ingest_corporate_actions(corrected_actions)
    assert correction.records_accepted == 1
    assert correction.records_duplicated == 4
    assert session.scalar(select(func.count()).select_from(CorporateAction)) == 5

    rows = json.loads((FIXTURES / "nse_announcements_fictional.json").read_bytes())
    first_announcements = NSEAnnouncementProvider(
        cast(object, _StaticSource(ANNOUNCEMENT_URI, json.dumps(rows).encode())),  # type: ignore[arg-type]
        _metadata(NSE_ANNOUNCEMENT_DATASET_CODE),
        _identities(),
        max_announcements=10,
    )
    service.ingest_announcements(first_announcements)
    rows[0]["attchmntText"] = "Fictional Alpha Limited has received a revised order."
    corrected_announcements = NSEAnnouncementProvider(
        cast(
            object,
            _StaticSource(ANNOUNCEMENT_URI, json.dumps(rows).encode(), LATER),
        ),  # type: ignore[arg-type]
        _metadata(NSE_ANNOUNCEMENT_DATASET_CODE),
        _identities(),
        max_announcements=10,
    )
    corrected = service.ingest_announcements(corrected_announcements)
    assert corrected.records_accepted == 1
    assert corrected.records_duplicated == 1
    assert session.scalar(select(func.count()).select_from(Announcement)) == 3

    rows[0]["seq_id"] = "900000003"
    rows[0]["sm_name"] = "Contradictory Fictional Name Limited"
    mismatch = NSEAnnouncementProvider(
        cast(
            object,
            _StaticSource(ANNOUNCEMENT_URI, json.dumps([rows[0]]).encode(), LATER),
        ),  # type: ignore[arg-type]
        _metadata(NSE_ANNOUNCEMENT_DATASET_CODE),
        _identities(),
        max_announcements=10,
    )
    result = service.ingest_announcements(mismatch)
    assert result.records_quarantined == 1


def test_malformed_structured_response_fails_closed() -> None:
    provider = NSEAnnouncementProvider(
        cast(object, _StaticSource(ANNOUNCEMENT_URI, b'{"data": []}')),  # type: ignore[arg-type]
        _metadata(NSE_ANNOUNCEMENT_DATASET_CODE),
        _identities(),
        max_announcements=10,
    )
    with pytest.raises(NSECorporateFilingFormatError, match="JSON list"):
        provider.fetch_announcements()


def test_official_document_fetcher_is_bounded_allowlisted_and_exact() -> None:
    payload = b"%PDF-1.4 exact fictional bytes"
    client = _DocumentClient(AcquiredNSEArtifact(PDF_URI, payload, RETRIEVED, "application/pdf"))
    fetcher = NSEOfficialDocumentFetcher(cast(object, client))  # type: ignore[arg-type]
    fetched = fetcher.fetch(uri=PDF_URI, max_bytes=100)
    assert fetched.content == payload
    assert fetched.retrieved_at == RETRIEVED
    assert fetched.media_type == "application/pdf"
    assert client.calls == [(PDF_URI, 100)]
    with pytest.raises(DocumentFetchError):
        fetcher.fetch(uri="http://nsearchives.nseindia.com/file.pdf", max_bytes=100)
    with pytest.raises(DocumentFetchError):
        fetcher.fetch(uri="https://example.com/file.pdf", max_bytes=100)
    too_large = NSEOfficialDocumentFetcher(
        cast(object, _DocumentClient(NSEArtifactTooLargeError("too large")))  # type: ignore[arg-type]
    )
    with pytest.raises(DocumentFetchError, match="exceeds"):
        too_large.fetch(uri=PDF_URI, max_bytes=10)


def test_full_official_shape_document_event_quantitative_vertical_is_idempotent(
    session, tmp_path: Path
) -> None:
    company, _ = _seed_identity(session)
    payload = (FIXTURES / "nse_announcements_fictional.json").read_bytes()
    provider = NSEAnnouncementProvider(
        cast(object, _StaticSource(ANNOUNCEMENT_URI, payload)),  # type: ignore[arg-type]
        _metadata(NSE_ANNOUNCEMENT_DATASET_CODE),
        _identities(),
        max_announcements=10,
    )
    store = LocalRawObjectStore(tmp_path / "raw")
    service = IngestionService(session, store)
    assert service.ingest_announcements(provider).records_accepted == 2
    assert service.ingest_announcements(provider).records_duplicated == 2
    dataset_id = _dataset_id(session, NSE_ANNOUNCEMENT_DATASET_CODE)
    announcements = PointInTimeAnnouncementReader(session).company_announcements_as_of(
        provider_dataset_id=dataset_id,
        company_id=company.id,
        as_of=RETRIEVED,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
    )
    announcement = next(value for value in announcements if value.documents)
    document = announcement.documents[0]
    pdf = _text_pdf("The company has received an order worth Rs. 75 lakh from a customer.")
    fetcher = NSEOfficialDocumentFetcher(
        cast(
            object,
            _DocumentClient(AcquiredNSEArtifact(PDF_URI, pdf, LATER, "application/pdf")),
        )  # type: ignore[arg-type]
    )
    acquisition = DocumentAcquisitionService(session, store)
    asset = acquisition.acquire(document_id=document.id, fetcher=fetcher)
    repeat_asset = acquisition.acquire(document_id=document.id, fetcher=fetcher)
    assert repeat_asset.asset_id == asset.asset_id
    extractor = PyPdfTextExtractor()
    extraction_service = DocumentTextExtractionService(session, store, clock=lambda: LATER)
    extraction = extraction_service.extract(document_asset_id=asset.asset_id, extractor=extractor)
    repeat_extraction = extraction_service.extract(
        document_asset_id=asset.asset_id, extractor=extractor
    )
    assert extraction.status == "success"
    assert repeat_extraction.extraction_id == extraction.extraction_id
    text = DocumentTextReader(session, store).text_for_document(
        document=document,
        extraction_identity=DocumentExtractionIdentity(
            extractor.extractor_code,
            extractor.extractor_semantic_version,
            extractor.extractor_runtime_version,
        ),
    )
    assert text is not None
    detector = BusinessEventDetectionService(BusinessEventRepository(session))
    detected = detector.detect(
        announcement=announcement,
        document_texts=(text,),
        derived_at=LATER,
    )
    repeated = detector.detect(
        announcement=announcement,
        document_texts=(text,),
        derived_at=LATER,
    )
    assert [value.event_type for value in detected] == ["order_award"]
    assert repeated[0].id == detected[0].id
    assert any(value.evidence_kind == "document_text" for value in detected[0].evidence)
    event = PointInTimeBusinessEventReader(session).events_for_announcement(
        announcement=announcement,
        ruleset_code=BUSINESS_EVENT_RULESET_CODE,
        ruleset_semantic_version=BUSINESS_EVENT_RULESET_VERSION,
    )[0]
    assert event.source_available_at == RETRIEVED
    quantitative = BusinessEventQuantitativeDerivationService(
        BusinessEventQuantitativeRepository(session), store
    )
    derived = quantitative.derive(event=event, derived_at=LATER)
    repeated_derived = quantitative.derive(event=event, derived_at=LATER)
    assert derived.facts
    assert {value.fact_code for value in derived.facts} == {"order_value"}
    assert repeated_derived.id == derived.id
    assert session.scalar(select(func.count()).select_from(Announcement)) == 2
    assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_static_boundaries_have_no_scoring_or_provider_scraper_imports() -> None:
    source = (
        Path(__file__).parents[1] / "packages/data/inflector_data/nse_corporate_filings.py"
    ).read_text(encoding="utf-8")
    assert "ScoreSnapshotOrchestrator" not in source
    assert "ComponentScorer" not in source
    assert "sqlalchemy" not in source.lower()
