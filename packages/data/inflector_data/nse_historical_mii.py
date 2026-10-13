"""Archive-first adapter for official historical NSE CM MII security masters."""

from __future__ import annotations

import csv
import gzip
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import RawObjectStore
from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.historical_universe import HistoricalUniverseEvidence
from inflector_data.nse_http import AcquiredNSEArtifact, validate_nse_url
from inflector_data.nse_providers import (
    HttpNSEArtifactSource,
    LocalNSEArtifactSource,
    NSEArtifactSource,
    NSEFormatError,
)
from inflector_data.service import IngestionResult
from inflector_database.ingestion_repository import IngestionRepository
from inflector_database.models import (
    HistoricalMIIArtifactAcquisition,
    IngestionRun,
    ProviderDataset,
    SourceRecord,
)

NSE_HISTORICAL_MII_DATASET_CODE = "nse_cm_mii_security_daily"
MII_PARSER_VERSION = "nse_cm_mii_security_csv_gzip_v1"
MII_REQUIRED_HEADERS = frozenset({"Sgmt", "FinInstrmTp", "ISIN", "TckrSymb", "SctySrs"})


@dataclass(frozen=True, slots=True)
class HistoricalMIISecurityRow:
    row_number: int
    semantic_row: dict[str, str]

    @property
    def isin(self) -> str:
        return self.semantic_row["ISIN"].strip()


@dataclass(frozen=True, slots=True)
class HistoricalMIIBatch:
    provider: ProviderMetadata
    artifact: AcquiredNSEArtifact
    source_date: date
    filename: str
    eligible_rows: tuple[HistoricalMIISecurityRow, ...]
    unsupported_row_count: int


@dataclass(frozen=True, slots=True)
class HistoricalMIIIngestionResult:
    acquisition_id: UUID
    acquisition_key_sha256: str
    acquisition_provenance: str
    acquisition_status: str
    ingestion: IngestionResult | None
    provider_dataset_id: UUID | None
    source_date: date
    raw_content_sha256: str
    evidence: tuple[HistoricalUniverseEvidence, ...]
    reused: bool
    unsupported_row_count: int


class NSEHistoricalMIISecurityProvider:
    """Parse one explicit official `NSE_CM_security_DDMMYYYY.csv.gz` artifact."""

    def __init__(
        self,
        source: NSEArtifactSource,
        metadata: ProviderMetadata,
        source_date: date,
        *,
        maximum_decompressed_bytes: int = 75_000_000,
    ) -> None:
        if metadata.dataset_code != NSE_HISTORICAL_MII_DATASET_CODE:
            raise ValueError("historical MII provider dataset binding mismatch")
        if maximum_decompressed_bytes <= 0:
            raise ValueError("maximum_decompressed_bytes must be positive")
        self._source = source
        self._metadata = metadata
        self._source_date = source_date
        self._maximum_decompressed_bytes = maximum_decompressed_bytes

    @property
    def maximum_decompressed_bytes(self) -> int:
        return self._maximum_decompressed_bytes

    @property
    def acquisition_provenance(self) -> str:
        if isinstance(self._source, HttpNSEArtifactSource):
            return "verified_official_https"
        if isinstance(self._source, LocalNSEArtifactSource):
            return "operator_supplied_unverified"
        return "fixture_only"

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    @property
    def source_date(self) -> date:
        return self._source_date

    def acquire_artifact(self) -> AcquiredNSEArtifact:
        artifact = self._source.acquire()
        validate_nse_url(artifact.source_uri)
        expected = mii_security_filename(self._source_date)
        source_name = PurePosixPath(unquote(urlparse(artifact.source_uri).path)).name
        if source_name != expected:
            raise NSEFormatError("historical MII source URI filename does not match requested date")
        return artifact

    def parse_artifact(self, artifact: AcquiredNSEArtifact) -> HistoricalMIIBatch:
        expected = mii_security_filename(self._source_date)
        payload = _bounded_gzip(artifact.payload, self._maximum_decompressed_bytes)
        rows = _semantic_rows(payload)
        eligible: list[HistoricalMIISecurityRow] = []
        unsupported = 0
        identities: set[tuple[str, str]] = set()
        for row_number, row in rows:
            segment = row["Sgmt"].strip()
            instrument = row["FinInstrmTp"].strip()
            series = row["SctySrs"].strip()
            isin = row["ISIN"].strip()
            symbol = row["TckrSymb"].strip()
            if segment == "CM" and instrument == "STK" and series == "EQ":
                if not isin or not symbol or symbol != symbol.upper():
                    raise NSEFormatError("historical MII eligible row has malformed identity")
                identity = (isin, series)
                if identity in identities:
                    raise NSEFormatError("historical MII artifact has duplicate semantic identity")
                identities.add(identity)
                eligible.append(HistoricalMIISecurityRow(row_number, row))
            else:
                unsupported += 1
        if not eligible:
            raise NSEFormatError("historical MII artifact has no eligible NSE CM EQ rows")
        return HistoricalMIIBatch(
            provider=self._metadata,
            artifact=artifact,
            source_date=self._source_date,
            filename=expected,
            eligible_rows=tuple(eligible),
            unsupported_row_count=unsupported,
        )

    def fetch(self) -> HistoricalMIIBatch:
        return self.parse_artifact(self.acquire_artifact())


def ingest_historical_mii(
    session: Session,
    *,
    archive: RawObjectStore,
    provider: NSEHistoricalMIISecurityProvider,
    started_at: datetime | None = None,
) -> HistoricalMIIIngestionResult:
    """Archive one artifact; only the reviewed HTTPS path may create Q evidence."""

    artifact = provider.acquire_artifact()
    retrieved_at = _utc(artifact.retrieved_at, "retrieved_at")
    raw = archive.put(artifact.payload)
    provenance = provider.acquisition_provenance
    acquisition_key = canonical_json_sha256(
        {
            "requested_source_date": provider.source_date.isoformat(),
            "acquisition_provenance": provenance,
            "source_uri": artifact.source_uri,
            "raw_content_sha256": raw.content_sha256,
            "parser_version": MII_PARSER_VERSION,
        }
    )
    acquisition = session.scalar(
        select(HistoricalMIIArtifactAcquisition).where(
            HistoricalMIIArtifactAcquisition.acquisition_key_sha256 == acquisition_key
        )
    )
    created_acquisition = acquisition is None
    if acquisition is None:
        acquisition = HistoricalMIIArtifactAcquisition(
            acquisition_key_sha256=acquisition_key,
            requested_source_date=provider.source_date,
            acquisition_provenance=provenance,
            source_uri=artifact.source_uri,
            raw_object_key=raw.object_key,
            raw_content_sha256=raw.content_sha256,
            retrieved_at=retrieved_at,
            expected_filename=mii_security_filename(provider.source_date),
            parser_version=MII_PARSER_VERSION,
            provenance_status=(
                "production_authoritative"
                if provenance == "verified_official_https"
                else "not_production_authoritative"
            ),
            status="archived",
            linked_ingestion_run_id=None,
            detail_json={},
        )
        session.add(acquisition)
        session.flush()
    batch = provider.parse_artifact(artifact)
    if provenance != "verified_official_https":
        acquisition.status = "unverified"
        acquisition.detail_json = {
            "eligible_row_count": len(batch.eligible_rows),
            "unsupported_row_count": batch.unsupported_row_count,
            "reason": (
                "only bytes fetched by the reviewed official HTTPS source may "
                "create Q evidence"
            ),
        }
        session.commit()
        return HistoricalMIIIngestionResult(
            acquisition.id,
            acquisition_key,
            provenance,
            acquisition.status,
            None,
            None,
            batch.source_date,
            raw.content_sha256,
            (),
            not created_acquisition,
            batch.unsupported_row_count,
        )
    repository = IngestionRepository(session)
    dataset = repository.ensure_dataset(batch.provider)
    expected = {
        f"nse-cm-mii-security:{batch.source_date.isoformat()}:{item.isin}:EQ": (
            canonical_json_sha256(item.semantic_row)
        )
        for item in batch.eligible_rows
    }
    candidates = tuple(
        session.scalars(
            select(IngestionRun).where(
                IngestionRun.provider_dataset_id == dataset.id, IngestionRun.status == "completed"
            )
        )
    )
    existing = next(
        (
            run
            for run in candidates
            if {
                item.external_record_id: item.content_sha256
                for item in _accepted_sources(session, run.id)
            }
            == expected
        ),
        None,
    )
    if existing is not None:
        acquisition.linked_ingestion_run_id = existing.id
        acquisition.status = "reused_semantic_snapshot"
        acquisition.detail_json = {"eligible_row_count": len(expected), "semantic_reuse": True}
        session.commit()
        evidence = evidence_for_ingestion(
            session,
            archive=archive,
            ingestion_run_id=existing.id,
            maximum_decompressed_bytes=provider.maximum_decompressed_bytes,
        )
        return _mii_result(
            acquisition=acquisition,
            dataset=dataset,
            run=existing,
            source_date=batch.source_date,
            raw_sha=raw.content_sha256,
            evidence=evidence,
            reused=True,
            unsupported=batch.unsupported_row_count,
        )
    run = repository.create_run(
        dataset.id,
        _utc(started_at or datetime.now(UTC), "started_at"),
        json.dumps(
            {
                "source_date": batch.source_date.isoformat(),
                "filename": batch.filename,
                "raw_content_sha256": raw.content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    session.commit()
    try:
        for item in batch.eligible_rows:
            row_hash = canonical_json_sha256(item.semantic_row)
            external_id = f"nse-cm-mii-security:{batch.source_date.isoformat()}:{item.isin}:EQ"
            if repository.source_exists(dataset.id, external_id, row_hash):
                raise NSEFormatError(
                    "historical MII row already belongs to a different source snapshot"
                )
            source = repository.create_source(
                run_id=run.id,
                dataset_id=dataset.id,
                external_id=external_id,
                source_uri=batch.artifact.source_uri,
                raw_object_key=raw.object_key,
                raw_payload_reference=f"{batch.filename}#row-{item.row_number}",
                raw_sha256=raw.content_sha256,
                content_sha256=row_hash,
                retrieved_at=retrieved_at,
                reported_at=None,
                published_at=None,
                available_at=retrieved_at,
                revision_at=None,
                parse_status="parsed",
            )
            source.validation_status = "accepted"
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.records_received = len(batch.eligible_rows)
        run.records_accepted = len(batch.eligible_rows)
        run.records_quarantined = 0
        run.records_duplicated = 0
        acquisition.linked_ingestion_run_id = run.id
        acquisition.status = "accepted"
        acquisition.detail_json = {"eligible_row_count": len(batch.eligible_rows)}
        session.commit()
    except Exception as error:
        session.rollback()
        failed = session.get(IngestionRun, run.id)
        assert failed is not None
        failed.status = "failed"
        failed.finished_at = datetime.now(UTC)
        failed.error_message = f"{type(error).__name__}: {error}"
        failed.records_received = len(batch.eligible_rows)
        failed.records_accepted = 0
        failed.records_quarantined = 0
        failed.records_duplicated = 0
        session.commit()
        raise
    evidence = evidence_for_ingestion(
        session,
        archive=archive,
        ingestion_run_id=run.id,
        maximum_decompressed_bytes=provider.maximum_decompressed_bytes,
    )
    return _mii_result(
        acquisition=acquisition,
        dataset=dataset,
        run=run,
        source_date=batch.source_date,
        raw_sha=raw.content_sha256,
        evidence=evidence,
        reused=False,
        unsupported=batch.unsupported_row_count,
    )


def mii_security_filename(source_date: date) -> str:
    return f"NSE_CM_security_{source_date:%d%m%Y}.csv.gz"


def _bounded_gzip(payload: bytes, maximum_bytes: int) -> bytes:
    try:
        with gzip.GzipFile(fileobj=BytesIO(payload), mode="rb") as stream:
            result = stream.read(maximum_bytes + 1)
    except (OSError, EOFError) as error:
        raise NSEFormatError("historical MII artifact is not a valid gzip stream") from error
    if len(result) > maximum_bytes:
        raise NSEFormatError("historical MII artifact exceeds decompressed byte limit")
    return result


def _semantic_rows(payload: bytes) -> list[tuple[int, dict[str, str]]]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise NSEFormatError("historical MII CSV is not UTF-8 compatible") from error
    reader = csv.reader(StringIO(text, newline=""))
    try:
        raw_headers = next(reader)
    except StopIteration as error:
        raise NSEFormatError("historical MII CSV is empty") from error
    headers = [item.strip() for item in raw_headers]
    if len(headers) != len(set(headers)):
        raise NSEFormatError("historical MII CSV has duplicate normalized headers")
    missing = MII_REQUIRED_HEADERS.difference(headers)
    if missing:
        raise NSEFormatError(f"historical MII CSV is missing required headers: {sorted(missing)}")
    output: list[tuple[int, dict[str, str]]] = []
    for row_number, values in enumerate(reader, start=2):
        if not values or not any(value.strip() for value in values):
            continue
        if len(values) != len(headers):
            raise NSEFormatError("historical MII CSV row shape is inconsistent")
        output.append((row_number, dict(zip(headers, values, strict=True))))
    return output


def _accepted_sources(session: Session, run_id: UUID) -> tuple[SourceRecord, ...]:
    return tuple(
        session.scalars(
            select(SourceRecord)
            .where(
                SourceRecord.ingestion_run_id == run_id,
                SourceRecord.parse_status == "parsed",
                SourceRecord.validation_status == "accepted",
            )
            .order_by(SourceRecord.external_record_id)
        )
    )


def _mii_result(
    *,
    acquisition: HistoricalMIIArtifactAcquisition,
    dataset: ProviderDataset,
    run: IngestionRun,
    source_date: date,
    raw_sha: str,
    evidence: tuple[HistoricalUniverseEvidence, ...],
    reused: bool,
    unsupported: int,
) -> HistoricalMIIIngestionResult:
    ingestion = IngestionResult(
        run.id,
        run.status,
        run.records_received,
        run.records_accepted,
        run.records_quarantined,
        run.records_duplicated,
    )
    return HistoricalMIIIngestionResult(
        acquisition_id=acquisition.id,
        acquisition_key_sha256=acquisition.acquisition_key_sha256,
        acquisition_provenance=acquisition.acquisition_provenance,
        acquisition_status=acquisition.status,
        ingestion=ingestion,
        provider_dataset_id=dataset.id,
        source_date=source_date,
        raw_content_sha256=raw_sha,
        evidence=evidence,
        reused=reused,
        unsupported_row_count=unsupported,
    )


def evidence_for_ingestion(
    session: Session,
    *,
    archive: RawObjectStore,
    ingestion_run_id: UUID,
    maximum_decompressed_bytes: int = 75_000_000,
) -> tuple[HistoricalUniverseEvidence, ...]:
    """Reconstruct exact semantic rows from the immutable archived artifact."""

    acquisition = session.scalar(
        select(HistoricalMIIArtifactAcquisition).where(
            HistoricalMIIArtifactAcquisition.linked_ingestion_run_id == ingestion_run_id,
            HistoricalMIIArtifactAcquisition.acquisition_provenance == "verified_official_https",
            HistoricalMIIArtifactAcquisition.status.in_(("accepted", "reused_semantic_snapshot")),
        )
    )
    if acquisition is None:
        raise NSEFormatError(
            "historical MII ingestion lacks verified official HTTPS acquisition provenance"
        )

    sources = _accepted_sources(session, ingestion_run_id)
    if not sources:
        raise NSEFormatError("historical MII ingestion has no accepted source rows")
    raw_keys = {item.raw_object_key for item in sources}
    raw_hashes = {item.raw_content_sha256 for item in sources}
    if len(raw_keys) != 1 or len(raw_hashes) != 1:
        raise NSEFormatError("historical MII ingestion mixes raw artifacts")
    raw = archive.get(next(iter(raw_keys)))
    if sha256(raw).hexdigest() != next(iter(raw_hashes)):
        raise NSEFormatError("historical MII archived raw SHA-256 mismatch")
    rows = _semantic_rows(_bounded_gzip(raw, maximum_decompressed_bytes))
    by_reference = {f"row-{number}": row for number, row in rows}
    output: list[HistoricalUniverseEvidence] = []
    for source in sources:
        reference = (source.raw_payload_reference or "").rsplit("#", maxsplit=1)[-1]
        row = by_reference.get(reference)
        if row is None or canonical_json_sha256(row) != source.content_sha256:
            raise NSEFormatError("historical MII semantic source row cannot be reproduced")
        output.append(HistoricalUniverseEvidence(source.id, row))
    return tuple(output)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
