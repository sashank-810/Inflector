"""Authoritative historical-universe projection and deterministic J sharding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from inflector_data.historical_evaluation_policy import (
    HistoricalUniversePolicy,
    canonical_json_sha256,
)
from inflector_database.historical_evaluation_repository import (
    HistoricalEvaluationRepository,
    HistoricalUniverseMemberWrite,
)
from inflector_database.models import (
    Company,
    DataProvider,
    ExchangeListing,
    HistoricalUniverseMemberRecord,
    HistoricalUniverseRun,
    IngestionRun,
    ProviderDataset,
    Security,
    SourceRecord,
)


class HistoricalUniverseDataUnavailableError(RuntimeError):
    """Raised when the required authoritative historical snapshot is absent."""


@dataclass(frozen=True, slots=True)
class HistoricalUniverseEvidence:
    source_record_id: UUID
    semantic_row: dict[str, str]


@dataclass(frozen=True, slots=True)
class HistoricalUniverseBuildResult:
    run_id: UUID
    run_key_sha256: str
    created_run: bool
    total_members: int
    eligible: int
    unresolved_identity: int
    ambiguous_identity: int
    unsupported_security_type: int
    excluded: int


@dataclass(frozen=True, slots=True)
class HistoricalUniverseShard:
    ordinal: int
    shard_id: str
    checksum_sha256: str
    symbols: tuple[str, ...]
    security_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class _Resolution:
    status: str
    reason: str | None
    company_id: UUID | None = None
    security_id: UUID | None = None
    listing_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _DerivedHistoricalUniverseEvidence:
    trading_date: date
    exchange: str
    series: str
    symbol: str
    isin: str
    source_record_id: UUID
    semantic_row_sha256: str


@dataclass(frozen=True, slots=True)
class _AuthoritativeSourceSnapshot:
    ingestion_run_id: UUID
    source_date: date
    raw_content_sha256: str
    raw_object_key: str
    source_uri: str
    expected_sources: tuple[SourceRecord, ...]


def build_historical_universe(
    session: Session,
    *,
    policy: HistoricalUniversePolicy,
    cutoff: datetime,
    source_provider_dataset_id: UUID,
    source_ingestion_run_id: UUID,
    evidence: tuple[HistoricalUniverseEvidence, ...],
    completed_at: datetime,
) -> HistoricalUniverseBuildResult:
    """Persist one cohort from source-linked historical membership evidence."""

    as_of = _utc(cutoff, "cutoff")
    finished = _utc(completed_at, "completed_at")
    if (as_of.date() + timedelta(days=1)).month == as_of.date().month:
        raise ValueError("historical universe cutoff must be a calendar month end")
    dataset = session.get(ProviderDataset, source_provider_dataset_id)
    provider = session.get(DataProvider, dataset.provider_id) if dataset is not None else None
    if (
        dataset is None
        or provider is None
        or provider.code != policy.source_provider_code
        or dataset.code != policy.source_dataset_code
    ):
        raise ValueError("historical universe authoritative source binding mismatch")
    snapshot = _authoritative_source_snapshot(
        session,
        source_provider_dataset_id=source_provider_dataset_id,
        source_ingestion_run_id=source_ingestion_run_id,
        policy=policy,
    )
    supplied_ids = [item.source_record_id for item in evidence]
    if len(supplied_ids) != len(set(supplied_ids)):
        raise ValueError("duplicate historical universe source row")
    expected_ids = {source.id for source in snapshot.expected_sources}
    if set(supplied_ids) != expected_ids:
        missing = len(expected_ids - set(supplied_ids))
        extra = len(set(supplied_ids) - expected_ids)
        raise ValueError(
            "historical universe evidence set does not exactly match the authoritative "
            f"source snapshot (missing={missing}, extra={extra})"
        )
    evidence_by_id = {item.source_record_id: item for item in evidence}
    projected: list[tuple[_DerivedHistoricalUniverseEvidence, SourceRecord, str]] = []
    fingerprints: set[str] = set()
    for source in snapshot.expected_sources:
        item = evidence_by_id[source.id]
        derived = _derive_source_evidence(
            policy=policy,
            evidence=item,
            source=source,
            source_date=snapshot.source_date,
        )
        if derived.trading_date > as_of.date():
            raise ValueError("historical membership date must not exceed cohort cutoff")
        fingerprint = canonical_json_sha256(
            {
                "trading_date": derived.trading_date.isoformat(),
                "exchange": derived.exchange,
                "series": derived.series,
                "symbol": derived.symbol,
                "isin": derived.isin,
                "source_record_id": str(source.id),
                "source_content_sha256": source.content_sha256,
                "semantic_row_verification_version": policy.semantic_row_verification_version,
            }
        )
        if fingerprint in fingerprints:
            raise ValueError("duplicate historical universe evidence member")
        fingerprints.add(fingerprint)
        projected.append((derived, source, fingerprint))
    membership_date = snapshot.source_date
    latest_source_date = _latest_supported_source_date(
        session,
        source_provider_dataset_id=source_provider_dataset_id,
        cutoff=as_of.date(),
        policy=policy,
    )
    if latest_source_date.year != as_of.year or latest_source_date.month != as_of.month:
        raise ValueError("no supported authoritative source date exists in the cutoff month")
    if membership_date != latest_source_date:
        raise ValueError("historical universe source date is not the latest supported cohort date")
    projected.sort(key=lambda row: row[2])
    complete_member_set_checksum = canonical_json_sha256(
        [fingerprint for _, _, fingerprint in projected]
    )
    source_state_payload = {
        "source_provider_dataset_id": str(source_provider_dataset_id),
        "source_ingestion_run_id": str(snapshot.ingestion_run_id),
        "source_date": snapshot.source_date.isoformat(),
        "raw_content_sha256": snapshot.raw_content_sha256,
        "raw_object_key": snapshot.raw_object_key,
        "source_uri": snapshot.source_uri,
        "eligible_source_row_count": len(snapshot.expected_sources),
        "complete_member_set_checksum_sha256": complete_member_set_checksum,
        "members": [
            {
                "member_fingerprint": fingerprint,
                "source_record_id": str(source.id),
                "source_available_at": _iso(source.available_at),
                "source_retrieved_at": _iso(source.retrieved_at),
                "source_raw_object_key": source.raw_object_key,
                "source_raw_payload_reference": source.raw_payload_reference,
            }
            for _, source, fingerprint in projected
        ],
    }
    source_state = canonical_json_sha256(source_state_payload)
    identity = {
        "universe_policy_checksum": policy.checksum_sha256,
        "cutoff": as_of.isoformat(),
        "source_provider_dataset_id": str(source_provider_dataset_id),
        "source_ingestion_run_id": str(snapshot.ingestion_run_id),
        "source_date": snapshot.source_date.isoformat(),
        "raw_content_sha256": snapshot.raw_content_sha256,
        "eligible_source_row_count": len(snapshot.expected_sources),
        "complete_member_set_checksum_sha256": complete_member_set_checksum,
        "source_state_checksum": source_state,
        "ordered_member_fingerprints": [row[2] for row in projected],
    }
    run_key = canonical_json_sha256(identity)
    repository = HistoricalEvaluationRepository(session)
    run, created = repository.create_universe_run(
        run_key_sha256=run_key,
        universe_policy_code=policy.code,
        universe_policy_checksum_sha256=policy.checksum_sha256,
        cutoff=as_of,
        source_provider_dataset_id=source_provider_dataset_id,
        source_state_checksum_sha256=source_state,
        inputs_json=identity,
    )
    if run.status == "completed":
        return _build_result(run, created_run=False)
    counts: dict[str, int] = {}
    for item, source, fingerprint in projected:
        resolution = _resolve_identity(session, policy=policy, evidence=item)
        counts[resolution.status] = counts.get(resolution.status, 0) + 1
        repository.add_universe_member(
            run,
            HistoricalUniverseMemberWrite(
                historical_symbol=item.symbol,
                historical_isin=item.isin,
                exchange=item.exchange,
                series=item.series,
                membership_date=item.trading_date,
                company_id=resolution.company_id,
                security_id=resolution.security_id,
                exchange_listing_id=resolution.listing_id,
                membership_status=resolution.status,
                reason_code=resolution.reason,
                source_record_id=source.id,
                member_fingerprint_sha256=fingerprint,
                provenance_json={
                    "source_record_id": str(source.id),
                    "source_ingestion_run_id": str(snapshot.ingestion_run_id),
                    "source_content_sha256": source.content_sha256,
                    "verified_semantic_row_sha256": item.semantic_row_sha256,
                    "source_raw_content_sha256": source.raw_content_sha256,
                    "source_raw_object_key": source.raw_object_key,
                    "source_raw_payload_reference": source.raw_payload_reference,
                    "economic_membership_date": item.trading_date.isoformat(),
                    "derived_exchange": item.exchange,
                    "derived_series": item.series,
                    "derived_symbol": item.symbol,
                    "derived_isin": item.isin,
                    "source_retrieved_at": _iso(source.retrieved_at),
                    "source_available_at": _iso(source.available_at),
                    "research_visibility_claimed": False,
                    "identity_resolution_semantics": policy.identity_resolution_semantics,
                    "semantic_row_verification_version": (
                        policy.semantic_row_verification_version
                    ),
                    "cohort_source_date_semantics": policy.cohort_source_date_semantics,
                    "source_snapshot_semantics": policy.source_snapshot_semantics,
                    "complete_member_set_semantics": policy.complete_member_set_semantics,
                    "complete_member_set_checksum_sha256": complete_member_set_checksum,
                    "authoritative_eligible_source_row_count": len(
                        snapshot.expected_sources
                    ),
                },
            ),
        )
    summary = {
        "total_members": len(projected),
        "counts_by_status": dict(sorted(counts.items())),
        "eligible_members": counts.get("eligible", 0),
        "unresolved_or_ambiguous": counts.get("unresolved_identity", 0)
        + counts.get("ambiguous_identity", 0),
        "current_status_filtering": policy.current_status_filtering,
        "source_ingestion_run_id": str(snapshot.ingestion_run_id),
        "source_date": snapshot.source_date.isoformat(),
        "raw_content_sha256": snapshot.raw_content_sha256,
        "eligible_source_row_count": len(snapshot.expected_sources),
        "complete_member_set_checksum_sha256": complete_member_set_checksum,
    }
    repository.complete_universe_run(
        run,
        member_count=len(projected),
        summary_json=summary,
        completed_at=finished,
    )
    session.commit()
    return _build_result(run, created_run=created)


def shard_historical_universe(
    members: tuple[HistoricalUniverseMemberRecord, ...],
    *,
    policy: HistoricalUniversePolicy,
) -> tuple[HistoricalUniverseShard, ...]:
    """Map eligible resolved members into stable J-compatible shards."""

    eligible = [item for item in members if item.membership_status == "eligible"]
    if any(item.security_id is None for item in eligible):
        raise ValueError("eligible historical member must have a security identity")
    ordered = sorted(
        eligible,
        key=lambda item: (
            str(item.security_id),
            item.historical_isin or "",
            item.historical_symbol,
            item.member_fingerprint_sha256,
        ),
    )
    symbols = [item.historical_symbol for item in ordered]
    security_ids = [item.security_id for item in ordered]
    if len(symbols) != len(set(symbols)):
        raise ValueError("historical shard symbols must be unique at the cohort cutoff")
    if len(security_ids) != len(set(security_ids)):
        raise ValueError("historical shard security identities must be unique")
    maximum = policy.maximum_symbols_per_backtest_shard
    shards: list[HistoricalUniverseShard] = []
    for offset in range(0, len(ordered), maximum):
        group = ordered[offset : offset + maximum]
        ordinal = len(shards) + 1
        projection = [
            {
                "security_id": str(item.security_id),
                "symbol": item.historical_symbol,
                "member_fingerprint": item.member_fingerprint_sha256,
            }
            for item in group
        ]
        checksum = canonical_json_sha256(projection)
        shards.append(
            HistoricalUniverseShard(
                ordinal=ordinal,
                shard_id=f"shard-{ordinal:04d}-{checksum[:12]}",
                checksum_sha256=checksum,
                symbols=tuple(item.historical_symbol for item in group),
                security_ids=tuple(item.security_id for item in group if item.security_id),
            )
        )
    return tuple(shards)


def historical_universe_summary(
    run: HistoricalUniverseRun, *, policy: HistoricalUniversePolicy
) -> dict[str, object]:
    members = sorted(
        run.members,
        key=lambda item: (
            item.membership_status,
            item.historical_symbol,
            item.historical_isin or "",
            item.member_fingerprint_sha256,
        ),
    )
    shards = shard_historical_universe(tuple(members), policy=policy)
    return {
        "run_id": run.id,
        "run_key_sha256": run.run_key_sha256,
        "cutoff": run.cutoff,
        "status": run.status,
        "summary": run.summary_json,
        "shards": [
            {
                "ordinal": item.ordinal,
                "shard_id": item.shard_id,
                "checksum_sha256": item.checksum_sha256,
                "symbols": list(item.symbols),
                "security_ids": list(item.security_ids),
            }
            for item in shards
        ],
        "members": [
            {
                "symbol": item.historical_symbol,
                "isin": item.historical_isin,
                "series": item.series,
                "membership_date": item.membership_date,
                "membership_status": item.membership_status,
                "reason_code": item.reason_code,
                "company_id": item.company_id,
                "security_id": item.security_id,
                "listing_id": item.exchange_listing_id,
                "source_record_id": item.source_record_id,
                "member_fingerprint_sha256": item.member_fingerprint_sha256,
            }
            for item in members
        ],
    }


def _resolve_identity(
    session: Session,
    *,
    policy: HistoricalUniversePolicy,
    evidence: _DerivedHistoricalUniverseEvidence,
) -> _Resolution:
    if evidence.exchange != policy.exchange:
        return _Resolution("excluded_exchange", "exchange_not_eligible")
    if evidence.series not in policy.eligible_series:
        return _Resolution("excluded_series", "series_not_eligible")
    rows: list[tuple[Company, Security, ExchangeListing]]
    interval = (
        ExchangeListing.exchange == evidence.exchange,
        ExchangeListing.valid_from <= evidence.trading_date,
        or_(
            ExchangeListing.valid_to.is_(None),
            ExchangeListing.valid_to >= evidence.trading_date,
        ),
    )
    statement = (
        select(Company, Security, ExchangeListing)
        .join(Security, Security.company_id == Company.id)
        .join(ExchangeListing, ExchangeListing.security_id == Security.id)
        .where(*interval)
    )
    if evidence.isin is not None:
        statement = statement.where(Security.isin == evidence.isin)
    else:
        statement = statement.where(ExchangeListing.symbol == evidence.symbol)
    rows = list(session.execute(statement).tuples())
    if not rows:
        return _Resolution("unresolved_identity", "no_exact_historical_identity")
    identities = {(security.id, listing.id) for _, security, listing in rows}
    if len(identities) != 1:
        return _Resolution("ambiguous_identity", "multiple_exact_historical_identities")
    company, security, listing = rows[0]
    if security.security_type not in policy.security_types:
        return _Resolution(
            "unsupported_security_type",
            "security_type_not_supported",
            company.id,
            security.id,
            listing.id,
        )
    return _Resolution("eligible", None, company.id, security.id, listing.id)


def _derive_source_evidence(
    *,
    policy: HistoricalUniversePolicy,
    evidence: HistoricalUniverseEvidence,
    source: SourceRecord,
    source_date: date,
) -> _DerivedHistoricalUniverseEvidence:
    row = evidence.semantic_row
    if (
        not isinstance(row, dict)
        or not row
        or any(
            not isinstance(key, str)
            or not key
            or key.startswith("__")
            or not isinstance(value, str)
            for key, value in row.items()
        )
    ):
        raise ValueError("historical universe semantic source row is malformed")
    semantic_hash = canonical_json_sha256(row)
    if semantic_hash != source.content_sha256:
        raise ValueError("historical universe semantic source row content hash mismatch")
    required = {"Sgmt", "FinInstrmTp", "ISIN", "TckrSymb", "SctySrs"}
    if not required.issubset(row):
        raise ValueError("historical universe semantic source row is malformed")
    segment = row["Sgmt"].strip()
    instrument_type = row["FinInstrmTp"].strip()
    series = row["SctySrs"].strip()
    symbol = row["TckrSymb"].strip()
    isin = row["ISIN"].strip()
    if segment != "CM" or instrument_type != "STK" or series != "EQ" or not isin:
        raise ValueError("historical universe row is not a supported NSE cash-market stock row")
    if series not in policy.eligible_series:
        raise ValueError("historical universe semantic source row series is unsupported")
    if not symbol or symbol != symbol.upper():
        raise ValueError("historical universe semantic source row symbol is malformed")
    expected_external_id = f"nse-cm-mii-security:{source_date.isoformat()}:{isin}:EQ"
    if source.external_record_id != expected_external_id:
        raise ValueError("historical universe source record identity disagrees with semantic row")
    return _DerivedHistoricalUniverseEvidence(
        trading_date=source_date,
        exchange=policy.exchange,
        series=series,
        symbol=symbol,
        isin=isin,
        source_record_id=source.id,
        semantic_row_sha256=semantic_hash,
    )


def _authoritative_source_snapshot(
    session: Session,
    *,
    source_provider_dataset_id: UUID,
    source_ingestion_run_id: UUID,
    policy: HistoricalUniversePolicy,
) -> _AuthoritativeSourceSnapshot:
    ingestion = session.get(IngestionRun, source_ingestion_run_id)
    if (
        ingestion is None
        or ingestion.provider_dataset_id != source_provider_dataset_id
        or ingestion.status != "completed"
    ):
        raise ValueError("historical universe source ingestion snapshot is not completed")
    all_sources = tuple(
        session.scalars(
            select(SourceRecord)
            .where(
                SourceRecord.ingestion_run_id == source_ingestion_run_id,
                SourceRecord.provider_dataset_id == source_provider_dataset_id,
            )
            .order_by(SourceRecord.id.asc())
        )
    )
    if not all_sources:
        raise HistoricalUniverseDataUnavailableError(
            "historical NSE CM MII security-master snapshot is unavailable"
        )
    if any(
        source.parse_status != "parsed" or source.validation_status != "accepted"
        for source in all_sources
    ):
        raise ValueError(
            "historical universe source ingestion contains an unaccepted source row"
        )
    sources = all_sources
    if (
        ingestion.records_quarantined != 0
        or ingestion.records_duplicated != 0
        or ingestion.records_received != len(sources)
        or ingestion.records_accepted != len(sources)
    ):
        raise ValueError(
            "historical universe source ingestion does not prove a complete eligible snapshot"
        )
    external_ids = {source.external_record_id for source in sources}
    if len(external_ids) != len(sources):
        raise ValueError("historical universe source ingestion contains duplicate source rows")
    raw_hashes = {source.raw_content_sha256 for source in sources}
    raw_objects = {source.raw_object_key for source in sources}
    source_uris = {source.source_uri for source in sources}
    if len(raw_hashes) != 1 or len(raw_objects) != 1 or len(source_uris) != 1:
        raise ValueError(
            "historical universe source ingestion mixes multiple archived source artifacts"
        )
    raw_hash = next(iter(raw_hashes))
    if len(raw_hash) != 64 or any(character not in "0123456789abcdef" for character in raw_hash):
        raise ValueError("historical universe archived source content hash is malformed")
    source_dates = {_mii_source_date(source) for source in sources}
    if len(source_dates) != 1:
        raise ValueError("historical universe source ingestion mixes snapshot dates")
    source_date = next(iter(source_dates))
    expected_filename = f"NSE_CM_security_{source_date.strftime('%d%m%Y')}.csv.gz"
    for source in sources:
        reference = source.raw_payload_reference or ""
        if expected_filename not in reference and expected_filename not in source.raw_object_key:
            raise ValueError("historical universe source artifact filename is not authoritative")
    if policy.source_filename_pattern != "NSE_CM_security_ddmmyyyy.csv.gz":
        raise ValueError("historical universe source filename policy is unsupported")
    return _AuthoritativeSourceSnapshot(
        ingestion_run_id=ingestion.id,
        source_date=source_date,
        raw_content_sha256=raw_hash,
        raw_object_key=next(iter(raw_objects)),
        source_uri=next(iter(source_uris)),
        expected_sources=sources,
    )


def _mii_source_date(source: SourceRecord) -> date:
    parts = source.external_record_id.split(":")
    if len(parts) != 4 or parts[0] != "nse-cm-mii-security" or parts[-1] != "EQ":
        raise ValueError("historical universe authoritative source identity is malformed")
    try:
        return date.fromisoformat(parts[1])
    except ValueError as error:
        raise ValueError("historical universe authoritative source date is malformed") from error


def _latest_supported_source_date(
    session: Session,
    *,
    source_provider_dataset_id: UUID,
    cutoff: date,
    policy: HistoricalUniversePolicy,
) -> date:
    ingestions = session.scalars(
        select(IngestionRun).where(
            IngestionRun.provider_dataset_id == source_provider_dataset_id,
            IngestionRun.status == "completed",
        )
    )
    dates: list[date] = []
    for ingestion in ingestions:
        try:
            snapshot = _authoritative_source_snapshot(
                session,
                source_provider_dataset_id=source_provider_dataset_id,
                source_ingestion_run_id=ingestion.id,
                policy=policy,
            )
        except HistoricalUniverseDataUnavailableError:
            continue
        if snapshot.source_date <= cutoff:
            dates.append(snapshot.source_date)
    if not dates:
        raise ValueError("no supported authoritative source date exists on or before cutoff")
    return max(dates)


def _build_result(
    run: HistoricalUniverseRun, *, created_run: bool
) -> HistoricalUniverseBuildResult:
    raw_counts = run.summary_json.get("counts_by_status", {})
    if not isinstance(raw_counts, dict):
        raise ValueError("historical universe summary counts are malformed")
    counts = {
        str(key): int(value)
        for key, value in raw_counts.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    excluded = counts.get("excluded_exchange", 0) + counts.get("excluded_series", 0)
    return HistoricalUniverseBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        created_run=created_run,
        total_members=run.member_count,
        eligible=counts.get("eligible", 0),
        unresolved_identity=counts.get("unresolved_identity", 0),
        ambiguous_identity=counts.get("ambiguous_identity", 0),
        unsupported_security_type=counts.get("unsupported_security_type", 0),
        excluded=excluded,
    )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
