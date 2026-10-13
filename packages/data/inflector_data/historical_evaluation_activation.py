"""Production S1 real-data readiness, temporal holdout, and factual error analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import PurePosixPath
from statistics import median
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.archive import RawObjectStore
from inflector_data.historical_activation_policy import (
    HistoricalEvaluationActivationPolicy,
    MultibaggerErrorAnalysisPolicy,
    MultibaggerTemporalSplitPolicy,
)
from inflector_data.historical_evaluation_policy import canonical_json_sha256
from inflector_data.nse_http import validate_nse_url
from inflector_database.models import (
    BenchmarkSeries,
    DataProvider,
    HistoricalMIIArtifactAcquisition,
    HistoricalSessionArtifactAcquisition,
    HistoricalSessionCalendarDate,
    HistoricalSessionCalendarRun,
    HistoricalUniverseRun,
    IngestionRun,
    MultibaggerConfusionRow,
    MultibaggerErrorAnalysisRow,
    MultibaggerErrorAnalysisRun,
    MultibaggerEvaluationCohort,
    MultibaggerEvaluationRun,
    MultibaggerOutcomeLabel,
    MultibaggerPredictionRow,
    MultibaggerTemporalSplitCutoff,
    MultibaggerTemporalSplitRun,
    ProviderDataset,
    ScoreComponent,
    SourceRecord,
)


class HistoricalEvaluationActivationError(RuntimeError):
    """An immutable S1 input conflicts with the accepted activation contract."""


class HistoricalEvaluationDataBlockedError(HistoricalEvaluationActivationError):
    """Authoritative evidence is absent or insufficient, not synthetically repairable."""


@dataclass(frozen=True, slots=True)
class SessionCalendarVerificationResult:
    run_id: UUID
    run_key_sha256: str
    status: str
    expected_count: int
    observed_count: int
    missing_dates: tuple[date, ...]
    unexpected_dates: tuple[date, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class TemporalSplitBuildResult:
    run_id: UUID
    run_key_sha256: str
    split_checksum_sha256: str
    eligible_count: int
    development_count: int
    validation_count: int
    holdout_count: int
    created: bool


@dataclass(frozen=True, slots=True)
class ErrorAnalysisBuildResult:
    run_id: UUID
    run_key_sha256: str
    row_count: int
    development_rows: int
    validation_rows: int
    created: bool
    summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class TemporalCutoffAssignment:
    historical_cutoff: datetime
    split_code: str


def _validate_persisted_verified_session_calendar(
    session: Session,
    *,
    calendar_run_id: UUID,
    activation_policy_code: str,
    activation_policy_checksum_sha256: str,
    source_provider_code: str,
    source_dataset_code: str,
    benchmark_dataset_code: str,
    benchmark_code: str,
    verification_algorithm: str,
) -> HistoricalSessionCalendarRun:
    """Fail closed unless one persisted verified calendar has a coherent source graph."""

    calendar = session.get(HistoricalSessionCalendarRun, calendar_run_id)
    if calendar is None or calendar.status != "verified":
        raise HistoricalEvaluationDataBlockedError(
            "independently verified historical NSE session calendar is unavailable"
        )
    if (
        calendar.activation_policy_code != activation_policy_code
        or calendar.activation_policy_checksum_sha256
        != activation_policy_checksum_sha256
        or calendar.acquisition_provenance != "verified_official_https"
    ):
        raise HistoricalEvaluationDataBlockedError(
            "session calendar activation/provenance binding mismatch"
        )
    acquisition = session.get(
        HistoricalSessionArtifactAcquisition,
        calendar.source_artifact_acquisition_id,
    )
    if (
        acquisition is None
        or acquisition.acquisition_provenance != "verified_official_https"
        or acquisition.provenance_status != "production_authoritative"
        or acquisition.status != "parsed"
    ):
        raise HistoricalEvaluationActivationError(
            "verified session calendar acquisition provenance is invalid"
        )
    duplicated = (
        acquisition.source_provider_dataset_id == calendar.source_provider_dataset_id
        and acquisition.source_ingestion_run_id == calendar.source_ingestion_run_id
        and acquisition.source_record_id == calendar.source_record_id
        and acquisition.source_uri == calendar.source_uri
        and acquisition.raw_object_key == calendar.raw_object_key
        and acquisition.raw_content_sha256 == calendar.raw_content_sha256
        and acquisition.parser_version == calendar.parser_version
        and _stored_utc(acquisition.retrieved_at)
        == _stored_utc(calendar.source_retrieved_at)
        and _optional_utc(acquisition.published_at)
        == _optional_utc(calendar.source_published_at)
    )
    if not duplicated:
        raise HistoricalEvaluationActivationError(
            "verified session calendar and acquisition identity mismatch"
        )
    source_dataset = session.get(ProviderDataset, calendar.source_provider_dataset_id)
    source_provider = (
        session.get(DataProvider, source_dataset.provider_id)
        if source_dataset is not None
        else None
    )
    if (
        source_dataset is None
        or source_provider is None
        or source_provider.code != source_provider_code
        or source_dataset.code != source_dataset_code
    ):
        raise HistoricalEvaluationActivationError(
            "verified session calendar official source dataset binding mismatch"
        )
    ingestion = session.get(IngestionRun, calendar.source_ingestion_run_id)
    source = session.get(SourceRecord, calendar.source_record_id)
    if (
        ingestion is None
        or ingestion.provider_dataset_id != source_dataset.id
        or ingestion.status != "completed"
        or source is None
        or source.ingestion_run_id != ingestion.id
        or source.provider_dataset_id != source_dataset.id
        or source.parse_status != "parsed"
        or source.validation_status != "accepted"
        or source.source_uri != calendar.source_uri
        or source.raw_object_key != calendar.raw_object_key
        or source.raw_content_sha256 != calendar.raw_content_sha256
        or _stored_utc(source.retrieved_at) != _stored_utc(calendar.source_retrieved_at)
        or _optional_utc(source.published_at) != _optional_utc(calendar.source_published_at)
        or source.available_at is None
        or _stored_utc(source.available_at) > _stored_utc(calendar.source_cutoff)
    ):
        raise HistoricalEvaluationActivationError(
            "verified session calendar source record graph is invalid"
        )
    benchmark_dataset = session.get(ProviderDataset, calendar.benchmark_provider_dataset_id)
    benchmark_provider = (
        session.get(DataProvider, benchmark_dataset.provider_id)
        if benchmark_dataset is not None
        else None
    )
    series = session.get(BenchmarkSeries, calendar.benchmark_series_id)
    if (
        benchmark_dataset is None
        or benchmark_provider is None
        or benchmark_provider.code != source_provider_code
        or benchmark_dataset.code != benchmark_dataset_code
        or series is None
        or series.provider_dataset_id != benchmark_dataset.id
        or series.code != benchmark_code
    ):
        raise HistoricalEvaluationActivationError(
            "verified session calendar benchmark binding mismatch"
        )
    rows = tuple(
        session.scalars(
            select(HistoricalSessionCalendarDate)
            .where(
                HistoricalSessionCalendarDate.historical_session_calendar_run_id
                == calendar.id
            )
            .order_by(HistoricalSessionCalendarDate.session_date)
        )
    )
    dates = tuple(item.session_date for item in rows)
    if (
        calendar.missing_dates_json
        or calendar.unexpected_dates_json
        or calendar.expected_count <= 0
        or calendar.expected_count != calendar.observed_count
        or len(rows) != calendar.expected_count
        or len(dates) != len(set(dates))
        or any(not item.benchmark_observed for item in rows)
        or any(item.source_record_id != source.id for item in rows)
        or any(
            item.session_date < calendar.requested_from_date
            or item.session_date > calendar.requested_to_date
            for item in rows
        )
        or calendar.first_expected_session != dates[0]
        or calendar.last_expected_session != dates[-1]
        or calendar.first_observed_session != dates[0]
        or calendar.last_observed_session != dates[-1]
    ):
        raise HistoricalEvaluationActivationError(
            "verified session calendar persisted date structure is invalid"
        )
    for row in rows:
        expected_fingerprint = canonical_json_sha256(
            {
                "run_key_sha256": calendar.run_key_sha256,
                "session_date": row.session_date.isoformat(),
                "benchmark_observed": True,
            }
        )
        if row.date_fingerprint_sha256 != expected_fingerprint:
            raise HistoricalEvaluationActivationError(
                "verified session calendar date fingerprint mismatch"
            )
    verification_payload = {
        "policy_checksum_sha256": activation_policy_checksum_sha256,
        "source_record_id": str(source.id),
        "source_content_sha256": source.content_sha256,
        "raw_content_sha256": source.raw_content_sha256,
        "benchmark_dataset_id": str(benchmark_dataset.id),
        "benchmark_series_id": str(series.id),
        "requested_from_date": calendar.requested_from_date.isoformat(),
        "requested_to_date": calendar.requested_to_date.isoformat(),
        "expected_sessions": [item.isoformat() for item in dates],
        "observed_sessions": [item.isoformat() for item in dates],
        "source_cutoff": _stored_utc(calendar.source_cutoff).isoformat(),
        "algorithm": verification_algorithm,
    }
    verification_checksum = canonical_json_sha256(verification_payload)
    run_key = canonical_json_sha256(
        {"verification_checksum_sha256": verification_checksum, "status": "verified"}
    )
    if (
        calendar.verification_checksum_sha256 != verification_checksum
        or calendar.run_key_sha256 != run_key
    ):
        raise HistoricalEvaluationActivationError(
            "verified session calendar checksum/run-key mismatch"
        )
    return calendar


def _validate_authoritative_mii_ingestion_graph(
    session: Session,
    *,
    universe: HistoricalUniverseRun | None,
    cohort: MultibaggerEvaluationCohort,
    policy: HistoricalEvaluationActivationPolicy,
) -> None:
    """Prove that a Q cohort's persisted MII source graph is authoritative."""

    if (
        universe is None
        or universe.status != "completed"
        or universe.universe_policy_code != "production_historical_universe_v1"
        or universe.universe_policy_checksum_sha256 != policy.inherited_checksums["universe"]
        or _stored_utc(universe.cutoff) != _stored_utc(cohort.historical_cutoff)
    ):
        raise HistoricalEvaluationActivationError("historical universe binding is invalid")
    source_run_text = universe.inputs_json.get("source_ingestion_run_id")
    source_date_text = universe.inputs_json.get("source_date")
    try:
        source_run_id = UUID(str(source_run_text))
        source_date = date.fromisoformat(str(source_date_text))
    except (TypeError, ValueError) as error:
        raise HistoricalEvaluationActivationError(
            "historical universe source snapshot identity is invalid"
        ) from error
    source_dataset = session.get(ProviderDataset, universe.source_provider_dataset_id)
    source_provider = (
        session.get(DataProvider, source_dataset.provider_id)
        if source_dataset is not None
        else None
    )
    source_run = session.get(IngestionRun, source_run_id)
    if (
        source_dataset is None
        or source_provider is None
        or source_provider.code != policy.source_provider_code
        or source_dataset.code != policy.mii_dataset_code
        or source_run is None
        or source_run.provider_dataset_id != source_dataset.id
        or source_run.status != "completed"
        or source_run.records_received != source_run.records_accepted
        or source_run.records_quarantined != 0
    ):
        raise HistoricalEvaluationActivationError("historical MII ingestion binding is invalid")
    sources = tuple(
        session.scalars(
            select(SourceRecord)
            .where(
                SourceRecord.ingestion_run_id == source_run.id,
                SourceRecord.provider_dataset_id == source_dataset.id,
            )
            .order_by(SourceRecord.id)
        )
    )
    if not sources or source_run.records_accepted != len(sources):
        raise HistoricalEvaluationActivationError("historical MII accepted source set is invalid")
    raw_keys = {item.raw_object_key for item in sources}
    raw_hashes = {item.raw_content_sha256 for item in sources}
    source_uris = {item.source_uri for item in sources}
    if len(raw_keys) != 1 or len(raw_hashes) != 1 or len(source_uris) != 1:
        raise HistoricalEvaluationActivationError("historical MII source rows mix artifacts")
    for source in sources:
        if (
            source.parse_status != "parsed"
            or source.validation_status != "accepted"
            or source.provider_dataset_id != source_dataset.id
            or source.ingestion_run_id != source_run.id
            or source.available_at is None
        ):
            raise HistoricalEvaluationActivationError("historical MII source row is invalid")
        parts = source.external_record_id.split(":")
        if (
            len(parts) != 4
            or parts[0] != "nse-cm-mii-security"
            or parts[1] != source_date.isoformat()
            or not parts[2]
            or parts[3] != "EQ"
        ):
            raise HistoricalEvaluationActivationError(
                "historical MII source row identity is invalid"
            )
    expected_filename = f"NSE_CM_security_{source_date:%d%m%Y}.csv.gz"
    acquisitions = tuple(
        session.scalars(
            select(HistoricalMIIArtifactAcquisition)
            .where(
                HistoricalMIIArtifactAcquisition.linked_ingestion_run_id == source_run.id,
                HistoricalMIIArtifactAcquisition.acquisition_provenance
                == "verified_official_https",
                HistoricalMIIArtifactAcquisition.provenance_status
                == "production_authoritative",
                HistoricalMIIArtifactAcquisition.status.in_(
                    ("accepted", "reused_semantic_snapshot")
                ),
            )
            .order_by(HistoricalMIIArtifactAcquisition.created_at)
        )
    )
    for acquisition in acquisitions:
        try:
            validate_nse_url(acquisition.source_uri)
        except RuntimeError:
            continue
        filename = PurePosixPath(unquote(urlparse(acquisition.source_uri).path)).name
        if (
            acquisition.requested_source_date != source_date
            or acquisition.parser_version != "nse_cm_mii_security_csv_gzip_v1"
            or acquisition.expected_filename != expected_filename
            or filename != expected_filename
            or acquisition.raw_object_key != next(iter(raw_keys))
            or acquisition.raw_content_sha256 != next(iter(raw_hashes))
            or acquisition.source_uri != next(iter(source_uris))
        ):
            continue
        if all(
            source.available_at is not None
            and _stored_utc(source.available_at) >= _stored_utc(acquisition.retrieved_at)
            for source in sources
        ):
            return
    raise HistoricalEvaluationActivationError(
        "historical MII acquisition does not bind the accepted source set"
    )


def official_session_semantic_projection(
    *, from_date: date, to_date: date, session_dates: tuple[date, ...]
) -> dict[str, object]:
    """Canonical projection an independently ingested official source must bind."""

    ordered = _ordered_unique_dates(session_dates)
    if from_date > to_date or not ordered:
        raise ValueError("official session range and dates must be non-empty and ordered")
    if ordered[0] < from_date or ordered[-1] > to_date:
        raise ValueError("official session date falls outside the requested range")
    return {
        "calendar_code": "NSE_CM",
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "session_dates": [item.isoformat() for item in ordered],
        "semantic_verification_version": "official_nse_session_dates_v1",
    }


def verify_historical_session_calendar(
    session: Session,
    *,
    policy: HistoricalEvaluationActivationPolicy,
    source_artifact_acquisition_id: UUID,
    archive: RawObjectStore,
    benchmark_provider_dataset_id: UUID,
    requested_from_date: date,
    requested_to_date: date,
    source_cutoff: datetime,
) -> SessionCalendarVerificationResult:
    """Verify only archive-bound evidence from a reviewed production parser.

    S1 deliberately has no reviewed official NSE session artifact format yet;
    therefore archived candidates are auditable but cannot self-certify.
    """

    _utc(source_cutoff, "source_cutoff")
    if requested_from_date > requested_to_date:
        raise ValueError("requested session range is invalid")
    acquisition = session.get(HistoricalSessionArtifactAcquisition, source_artifact_acquisition_id)
    source_dataset = (
        session.get(ProviderDataset, acquisition.source_provider_dataset_id)
        if acquisition
        else None
    )
    source_provider = (
        session.get(DataProvider, source_dataset.provider_id)
        if source_dataset is not None
        else None
    )
    if (
        acquisition is None
        or source_dataset is None
        or source_provider is None
        or source_provider.code != policy.source_provider_code
        or source_dataset.code != policy.official_session_source_dataset_code
        or acquisition.acquisition_provenance != "verified_official_https"
    ):
        raise HistoricalEvaluationActivationError(
            "session verification requires archive-bound verified official HTTPS evidence"
        )
    validate_nse_url(acquisition.source_uri)
    try:
        raw = archive.get(acquisition.raw_object_key)
    except Exception as error:
        raise HistoricalEvaluationActivationError(
            "archived session source bytes are unavailable"
        ) from error
    if sha256(raw).hexdigest() != acquisition.raw_content_sha256:
        raise HistoricalEvaluationActivationError("archived session source SHA-256 mismatch")
    raise HistoricalEvaluationDataBlockedError(
        "no reviewed official NSE session-calendar artifact parser is available"
    )


def build_multibagger_temporal_split(
    session: Session,
    *,
    policy: MultibaggerTemporalSplitPolicy,
    source_evaluation_run_id: UUID,
    session_calendar_run_id: UUID,
    completed_at: datetime,
) -> TemporalSplitBuildResult:
    """Freeze cutoff identities chronologically without loading any outcome rows."""

    completed = _utc(completed_at, "completed_at")
    run_meta = session.execute(
        select(
            MultibaggerEvaluationRun.id,
            MultibaggerEvaluationRun.run_key_sha256,
            MultibaggerEvaluationRun.evaluation_policy_checksum_sha256,
            MultibaggerEvaluationRun.status,
        ).where(MultibaggerEvaluationRun.id == source_evaluation_run_id)
    ).one_or_none()
    if (
        run_meta is None
        or run_meta.status != "completed"
        or run_meta.evaluation_policy_checksum_sha256
        != policy.source_evaluation_policy_checksum_sha256
    ):
        raise HistoricalEvaluationActivationError("source R evaluation run binding mismatch")
    calendar = _validate_persisted_verified_session_calendar(
        session,
        calendar_run_id=session_calendar_run_id,
        activation_policy_code=policy.activation_policy_code,
        activation_policy_checksum_sha256=policy.activation_policy_checksum_sha256,
        source_provider_code="nse_official",
        source_dataset_code="nse_cm_official_session_calendar",
        benchmark_dataset_code="nse_indices_daily",
        benchmark_code="NIFTY 50",
        verification_algorithm="official_expected_sessions_exact_nifty_observed_set_v1",
    )
    cohorts = tuple(
        session.execute(
            select(
                MultibaggerEvaluationCohort.id,
                MultibaggerEvaluationCohort.historical_cutoff,
                MultibaggerEvaluationCohort.outcome_data_cutoff,
                MultibaggerEvaluationCohort.bundle_checksum_sha256,
            )
            .where(
                MultibaggerEvaluationCohort.multibagger_evaluation_run_id
                == source_evaluation_run_id,
                MultibaggerEvaluationCohort.status == "completed",
            )
            .order_by(MultibaggerEvaluationCohort.historical_cutoff)
        )
    )
    if not cohorts:
        raise HistoricalEvaluationDataBlockedError("R evaluation has no completed cohorts")
    outcome_cutoffs = {_stored_utc(item.outcome_data_cutoff) for item in cohorts}
    if len(outcome_cutoffs) != 1:
        raise HistoricalEvaluationActivationError("R cohorts mix outcome data cutoffs")
    outcome_cutoff = next(iter(outcome_cutoffs))
    partition = partition_temporal_cutoffs(
        policy=policy,
        historical_cutoffs=tuple(_stored_utc(item.historical_cutoff) for item in cohorts),
        outcome_data_cutoff=outcome_cutoff,
    )
    eligible_dates = {item.historical_cutoff for item in partition}
    eligible = tuple(
        item for item in cohorts if _stored_utc(item.historical_cutoff) in eligible_dates
    )
    if eligible and (
        calendar.requested_from_date > _stored_utc(eligible[0].historical_cutoff).date()
        or calendar.requested_to_date < outcome_cutoff.date()
    ):
        raise HistoricalEvaluationDataBlockedError(
            "verified session calendar does not cover the complete evaluation window"
        )
    split_by_date = {item.historical_cutoff: item.split_code for item in partition}
    assignments = [(item, split_by_date[_stored_utc(item.historical_cutoff)]) for item in eligible]
    development_count = sum(item.split_code == "development" for item in partition)
    validation_count = sum(item.split_code == "validation" for item in partition)
    holdout_count = sum(item.split_code == "holdout" for item in partition)
    assignment_projection = [
        {
            "cohort_id": str(item.id),
            "cutoff": _stored_utc(item.historical_cutoff).isoformat(),
            "bundle_checksum_sha256": item.bundle_checksum_sha256,
            "split_code": split,
        }
        for item, split in assignments
    ]
    split_checksum = canonical_json_sha256(assignment_projection)
    run_key = canonical_json_sha256(
        {
            "split_policy_checksum_sha256": policy.checksum_sha256,
            "source_evaluation_run_id": str(run_meta.id),
            "source_evaluation_run_key_sha256": run_meta.run_key_sha256,
            "session_calendar_run_id": str(calendar.id),
            "session_calendar_run_key_sha256": calendar.run_key_sha256,
            "outcome_data_cutoff": outcome_cutoff.isoformat(),
            "split_checksum_sha256": split_checksum,
        }
    )
    existing = session.scalar(
        select(MultibaggerTemporalSplitRun).where(
            MultibaggerTemporalSplitRun.run_key_sha256 == run_key
        )
    )
    if existing is not None:
        return _split_result(existing, created=False)
    serialized = {
        split: [
            _stored_utc(item.historical_cutoff).isoformat()
            for item, item_split in assignments
            if item_split == split
        ]
        for split in ("development", "validation", "holdout")
    }
    split_run = MultibaggerTemporalSplitRun(
        run_key_sha256=run_key,
        split_policy_code=policy.code,
        split_policy_checksum_sha256=policy.checksum_sha256,
        source_evaluation_run_id=run_meta.id,
        source_evaluation_run_key_sha256=run_meta.run_key_sha256,
        session_calendar_run_id=calendar.id,
        ordered_eligible_cutoffs_json=[
            _stored_utc(item.historical_cutoff).isoformat() for item in eligible
        ],
        development_cutoffs_json=serialized["development"],
        validation_cutoffs_json=serialized["validation"],
        holdout_cutoffs_json=serialized["holdout"],
        split_checksum_sha256=split_checksum,
        outcome_data_cutoff=outcome_cutoff,
        status="completed",
        summary_json={
            "eligible_count": len(eligible),
            "development_count": development_count,
            "validation_count": validation_count,
            "holdout_count": holdout_count,
            "holdout_outcomes_read": False,
        },
        completed_at=completed,
    )
    session.add(split_run)
    session.flush()
    for item, split in assignments:
        session.add(
            MultibaggerTemporalSplitCutoff(
                multibagger_temporal_split_run_id=split_run.id,
                multibagger_evaluation_cohort_id=item.id,
                historical_cutoff=item.historical_cutoff,
                split_code=split,
                assignment_fingerprint_sha256=canonical_json_sha256(
                    {
                        "split_checksum_sha256": split_checksum,
                        "cohort_id": str(item.id),
                        "split_code": split,
                    }
                ),
            )
        )
    session.commit()
    return _split_result(split_run, created=True)


def partition_temporal_cutoffs(
    *,
    policy: MultibaggerTemporalSplitPolicy,
    historical_cutoffs: tuple[datetime, ...],
    outcome_data_cutoff: datetime,
) -> tuple[TemporalCutoffAssignment, ...]:
    """Pure chronological partition; it has no label, return, score, or random input."""

    outcome_cutoff = _utc(outcome_data_cutoff, "outcome_data_cutoff")
    ordered = tuple(sorted(_utc(item, "historical_cutoff") for item in historical_cutoffs))
    if len(ordered) != len(set(ordered)):
        raise HistoricalEvaluationActivationError("temporal split has duplicate cohort cutoff")
    eligible = tuple(
        item
        for item in ordered
        if _add_calendar_years(item.date(), policy.longest_horizon_calendar_years)
        <= outcome_cutoff.date()
    )
    development_count = len(eligible) * policy.development_percent // 100
    validation_count = len(eligible) * policy.validation_percent // 100
    holdout_count = len(eligible) - development_count - validation_count
    if (
        validation_count < policy.minimum_validation_cutoffs
        or holdout_count < policy.minimum_holdout_cutoffs
    ):
        raise HistoricalEvaluationDataBlockedError(
            "insufficient fully matured monthly cohorts for validation and holdout"
        )
    return tuple(
        TemporalCutoffAssignment(
            item,
            "development"
            if index < development_count
            else "validation"
            if index < development_count + validation_count
            else "holdout",
        )
        for index, item in enumerate(eligible)
    )


def build_multibagger_error_analysis(
    session: Session,
    *,
    policy: MultibaggerErrorAnalysisPolicy,
    temporal_split_run_id: UUID,
    completed_at: datetime,
) -> ErrorAnalysisBuildResult:
    """Persist factual FP/FN evidence for development and validation, never holdout."""

    completed = _utc(completed_at, "completed_at")
    split_meta = session.execute(
        select(
            MultibaggerTemporalSplitRun.id,
            MultibaggerTemporalSplitRun.split_checksum_sha256,
            MultibaggerTemporalSplitRun.source_evaluation_run_id,
            MultibaggerTemporalSplitRun.status,
        ).where(MultibaggerTemporalSplitRun.id == temporal_split_run_id)
    ).one_or_none()
    if split_meta is None or split_meta.status != "completed":
        raise HistoricalEvaluationActivationError("completed temporal split is required")
    evaluation = session.execute(
        select(
            MultibaggerEvaluationRun.id,
            MultibaggerEvaluationRun.run_key_sha256,
            MultibaggerEvaluationRun.evaluation_policy_checksum_sha256,
            MultibaggerEvaluationRun.universe_policy_checksum_sha256,
            MultibaggerEvaluationRun.outcome_policy_checksum_sha256,
            MultibaggerEvaluationRun.model_family,
            MultibaggerEvaluationRun.scoring_configuration_checksum_sha256,
        ).where(MultibaggerEvaluationRun.id == split_meta.source_evaluation_run_id)
    ).one()
    if (
        evaluation.evaluation_policy_checksum_sha256
        != policy.source_evaluation_policy_checksum_sha256
    ):
        raise HistoricalEvaluationActivationError("error analysis R policy binding mismatch")
    cutoff_rows = tuple(
        session.execute(
            select(
                MultibaggerTemporalSplitCutoff.id,
                MultibaggerTemporalSplitCutoff.multibagger_evaluation_cohort_id,
                MultibaggerTemporalSplitCutoff.historical_cutoff,
                MultibaggerTemporalSplitCutoff.split_code,
            )
            .where(
                MultibaggerTemporalSplitCutoff.multibagger_temporal_split_run_id == split_meta.id
            )
            .order_by(MultibaggerTemporalSplitCutoff.historical_cutoff)
        )
    )
    analysis_cutoffs = tuple(
        item for item in cutoff_rows if item.split_code in policy.allowed_split_codes
    )
    holdout_cutoffs = tuple(item for item in cutoff_rows if item.split_code == "holdout")
    if not analysis_cutoffs or not holdout_cutoffs:
        raise HistoricalEvaluationActivationError(
            "temporal split must contain analysis and frozen holdout cutoffs"
        )
    if {item.multibagger_evaluation_cohort_id for item in analysis_cutoffs} & {
        item.multibagger_evaluation_cohort_id for item in holdout_cutoffs
    }:
        raise HistoricalEvaluationActivationError("holdout cohort overlaps analysis cohorts")
    run_key = canonical_json_sha256(
        {
            "error_policy_checksum_sha256": policy.checksum_sha256,
            "temporal_split_run_id": str(split_meta.id),
            "temporal_split_checksum_sha256": split_meta.split_checksum_sha256,
            "source_evaluation_run_id": str(evaluation.id),
            "source_evaluation_run_key_sha256": evaluation.run_key_sha256,
        }
    )
    existing = session.scalar(
        select(MultibaggerErrorAnalysisRun).where(
            MultibaggerErrorAnalysisRun.run_key_sha256 == run_key
        )
    )
    if existing is not None:
        return _analysis_result(existing, created=False)
    split_by_cohort = {item.multibagger_evaluation_cohort_id: item for item in analysis_cutoffs}
    analysis_ids = tuple(split_by_cohort)
    # This is the first outcome-bearing query in S1 analysis. Its cohort filter
    # is constructed exclusively from persisted development/validation rows.
    facts = tuple(
        session.execute(
            select(
                MultibaggerConfusionRow,
                MultibaggerPredictionRow,
                MultibaggerOutcomeLabel,
            )
            .join(
                MultibaggerPredictionRow,
                MultibaggerConfusionRow.multibagger_prediction_row_id
                == MultibaggerPredictionRow.id,
            )
            .join(
                MultibaggerOutcomeLabel,
                MultibaggerConfusionRow.multibagger_outcome_label_id == MultibaggerOutcomeLabel.id,
            )
            .where(MultibaggerPredictionRow.multibagger_evaluation_cohort_id.in_(analysis_ids))
        )
    )
    snapshot_ids = tuple(
        {
            prediction.score_snapshot_id
            for _, prediction, _ in facts
            if prediction.score_snapshot_id is not None
        }
    )
    components: dict[UUID, dict[str, Decimal]] = defaultdict(dict)
    if snapshot_ids:
        for snapshot_id, code, score in session.execute(
            select(
                ScoreComponent.score_snapshot_id,
                ScoreComponent.component_code,
                ScoreComponent.score,
            ).where(ScoreComponent.score_snapshot_id.in_(snapshot_ids))
        ):
            if score is not None:
                components[snapshot_id][code] = score
    analysis_run = MultibaggerErrorAnalysisRun(
        run_key_sha256=run_key,
        error_policy_code=policy.code,
        error_policy_checksum_sha256=policy.checksum_sha256,
        temporal_split_run_id=split_meta.id,
        temporal_split_checksum_sha256=split_meta.split_checksum_sha256,
        source_evaluation_run_id=evaluation.id,
        evaluation_policy_checksum_sha256=evaluation.evaluation_policy_checksum_sha256,
        universe_policy_checksum_sha256=evaluation.universe_policy_checksum_sha256,
        outcome_policy_checksum_sha256=evaluation.outcome_policy_checksum_sha256,
        model_family=evaluation.model_family,
        scoring_configuration_checksum_sha256=(evaluation.scoring_configuration_checksum_sha256),
        holdout_outcomes_accessed=False,
        status="completed",
        summary_json={},
        completed_at=completed,
    )
    session.add(analysis_run)
    session.flush()
    persisted_rows: list[MultibaggerErrorAnalysisRow] = []
    for confusion, prediction, label in facts:
        if confusion.confusion_class not in policy.confusion_classes:
            continue
        cutoff = split_by_cohort[prediction.multibagger_evaluation_cohort_id]
        boundary_count, boundary_score = _selection_boundary(
            session,
            cohort_id=prediction.multibagger_evaluation_cohort_id,
            selection_code=confusion.selection_contract_code,
        )
        below = (
            prediction.display_order - boundary_count
            if prediction.rankable
            and prediction.display_order is not None
            and prediction.display_order > boundary_count
            else None
        )
        gap = (
            boundary_score - prediction.final_score
            if boundary_score is not None
            and prediction.final_score is not None
            and below is not None
            else None
        )
        component_values = (
            components.get(prediction.score_snapshot_id, {})
            if prediction.score_snapshot_id is not None
            else {}
        )
        component_json = {key: str(value) for key, value in sorted(component_values.items())}
        bucket = _score_bucket(prediction.final_score)
        signature = _error_signature(
            confusion_class=confusion.confusion_class,
            rankable=prediction.rankable,
            rank_positions_below_boundary=below,
            score_bucket=bucket,
        )
        projection: dict[str, object] = {
            "analysis_run_key_sha256": run_key,
            "source_confusion_row_id": str(confusion.id),
            "split_code": cutoff.split_code,
            "confusion_class": confusion.confusion_class,
            "component_values": component_json,
            "error_signature": signature,
        }
        row = MultibaggerErrorAnalysisRow(
            multibagger_error_analysis_run_id=analysis_run.id,
            temporal_split_cutoff_id=cutoff.id,
            source_confusion_row_id=confusion.id,
            prediction_row_id=prediction.id,
            split_code=cutoff.split_code,
            historical_cutoff=cutoff.historical_cutoff,
            company_id=prediction.company_id,
            security_id=prediction.security_id,
            historical_symbol=prediction.historical_symbol,
            outcome_contract_code=confusion.outcome_contract_code,
            selection_contract_code=confusion.selection_contract_code,
            evaluation_view=confusion.evaluation_view,
            confusion_class=confusion.confusion_class,
            rankable=prediction.rankable,
            unranked_reason=prediction.unranked_reason,
            snapshot_status=prediction.snapshot_status,
            final_score=prediction.final_score,
            dense_score_rank=prediction.dense_score_rank,
            display_order=prediction.display_order,
            selection_boundary_count=boundary_count,
            selection_boundary_score=boundary_score,
            rank_positions_below_boundary=below,
            score_gap_to_boundary=gap,
            threshold_hit_trading_date=confusion.threshold_hit_trading_date,
            calendar_days_to_threshold=label.calendar_days_to_threshold,
            trading_observations_to_threshold=label.trading_observations_to_threshold,
            endpoint_return=confusion.endpoint_return,
            peak_price_multiple=confusion.peak_price_multiple,
            maximum_drawdown=confusion.maximum_drawdown,
            available_component_codes_json=prediction.available_component_codes_json,
            component_values_json=component_json,
            score_bucket=bucket,
            error_signature=signature,
            factual_provenance_json={
                "source_confusion_fingerprint_sha256": confusion.confusion_fingerprint_sha256,
                "source_label_fingerprint_sha256": label.label_fingerprint_sha256,
                "prediction_fingerprint_sha256": prediction.prediction_fingerprint_sha256,
            },
            row_fingerprint_sha256=canonical_json_sha256(projection),
        )
        session.add(row)
        persisted_rows.append(row)
    summary = _error_summary(
        facts=facts,
        persisted_rows=persisted_rows,
        components=components,
        split_by_cohort=split_by_cohort,
    )
    analysis_run.summary_json = summary
    session.commit()
    return _analysis_result(analysis_run, created=True)


def historical_evaluation_readiness(
    session: Session,
    *,
    policy: HistoricalEvaluationActivationPolicy,
    split_policy: MultibaggerTemporalSplitPolicy,
    source_evaluation_run_id: UUID | None = None,
    session_calendar_run_id: UUID | None = None,
) -> dict[str, object]:
    """Read-only factual readiness; it never creates, claims, or evaluates anything."""
    blockers: list[str] = []
    evaluation = (
        session.get(MultibaggerEvaluationRun, source_evaluation_run_id)
        if source_evaluation_run_id
        else None
    )
    calendar: HistoricalSessionCalendarRun | None = None
    if (
        evaluation is None
        or evaluation.status != "completed"
        or (
            evaluation.evaluation_policy_checksum_sha256 != policy.inherited_checksums["evaluation"]
        )
        or evaluation.backtest_policy_checksum_sha256
        != policy.inherited_checksums["backtest"]
        or evaluation.universe_policy_checksum_sha256
        != policy.inherited_checksums["universe"]
        or evaluation.outcome_policy_checksum_sha256
        != policy.inherited_checksums["outcomes"]
        or evaluation.research_profile_checksum_sha256
        != policy.inherited_checksums["research"]
    ):
        blockers.append("exact_completed_production_r_evaluation_unavailable")
    if (
        split_policy.activation_policy_code != policy.code
        or split_policy.activation_policy_checksum_sha256 != policy.checksum_sha256
    ):
        blockers.append("temporal_split_policy_activation_binding_mismatch")
    if session_calendar_run_id is not None:
        try:
            calendar = _validate_persisted_verified_session_calendar(
                session,
                calendar_run_id=session_calendar_run_id,
                activation_policy_code=policy.code,
                activation_policy_checksum_sha256=policy.checksum_sha256,
                source_provider_code=policy.source_provider_code,
                source_dataset_code=policy.official_session_source_dataset_code,
                benchmark_dataset_code=policy.benchmark_dataset_code,
                benchmark_code=policy.benchmark_code,
                verification_algorithm=policy.calendar_verification_algorithm,
            )
        except HistoricalEvaluationActivationError:
            calendar = None
    if calendar is None:
        blockers.append("exact_production_verified_session_calendar_unavailable")
    cohorts = (
        tuple(
            session.scalars(
                select(MultibaggerEvaluationCohort).where(
                    MultibaggerEvaluationCohort.multibagger_evaluation_run_id
                    == source_evaluation_run_id,
                    MultibaggerEvaluationCohort.status == "completed",
                )
            )
        )
        if evaluation is not None
        else ()
    )
    if evaluation is not None and not cohorts:
        blockers.append("evaluation_cohorts_unavailable")
    for cohort in cohorts:
        universe = session.get(HistoricalUniverseRun, cohort.historical_universe_run_id)
        try:
            _validate_authoritative_mii_ingestion_graph(
                session,
                universe=universe,
                cohort=cohort,
                policy=policy,
            )
        except HistoricalEvaluationActivationError:
            blockers.append(f"cohort_{cohort.id}_authoritative_mii_provenance_unavailable")
    if cohorts:
        earliest = min(_stored_utc(item.historical_cutoff).date() for item in cohorts)
        outcome_cutoffs = {_stored_utc(item.outcome_data_cutoff) for item in cohorts}
        if len(outcome_cutoffs) != 1:
            blockers.append("evaluation_cohorts_mix_outcome_cutoffs")
        else:
            outcome_cutoff = next(iter(outcome_cutoffs))
            if calendar is not None and (
                calendar.requested_from_date > earliest
                or calendar.requested_to_date < outcome_cutoff.date()
                or _stored_utc(calendar.source_cutoff) < outcome_cutoff
            ):
                blockers.append("session_calendar_does_not_cover_evaluation_period")
            try:
                partition_temporal_cutoffs(
                    policy=split_policy,
                    historical_cutoffs=tuple(
                        _stored_utc(item.historical_cutoff) for item in cohorts
                    ),
                    outcome_data_cutoff=outcome_cutoff,
                )
            except HistoricalEvaluationDataBlockedError:
                blockers.append(
                    "insufficient_fully_matured_cutoffs_for_temporal_split"
                )
    return {
        "status": "READY" if not blockers else "DATA-BLOCKED",
        "source_evaluation_run_id": None if evaluation is None else str(evaluation.id),
        "session_calendar_run_id": None if calendar is None else str(calendar.id),
        "temporal_split_policy_checksum_sha256": split_policy.checksum_sha256,
        "blockers": sorted(set(blockers)),
    }


def _calendar_result(
    run: HistoricalSessionCalendarRun, *, created: bool
) -> SessionCalendarVerificationResult:
    return SessionCalendarVerificationResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        status=run.status,
        expected_count=run.expected_count,
        observed_count=run.observed_count,
        missing_dates=tuple(date.fromisoformat(str(item)) for item in run.missing_dates_json),
        unexpected_dates=tuple(date.fromisoformat(str(item)) for item in run.unexpected_dates_json),
        created=created,
    )


def _split_result(run: MultibaggerTemporalSplitRun, *, created: bool) -> TemporalSplitBuildResult:
    return TemporalSplitBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        split_checksum_sha256=run.split_checksum_sha256,
        eligible_count=len(run.ordered_eligible_cutoffs_json),
        development_count=len(run.development_cutoffs_json),
        validation_count=len(run.validation_cutoffs_json),
        holdout_count=len(run.holdout_cutoffs_json),
        created=created,
    )


def _analysis_result(
    run: MultibaggerErrorAnalysisRun, *, created: bool
) -> ErrorAnalysisBuildResult:
    rows = run.rows
    return ErrorAnalysisBuildResult(
        run_id=run.id,
        run_key_sha256=run.run_key_sha256,
        row_count=len(rows),
        development_rows=sum(item.split_code == "development" for item in rows),
        validation_rows=sum(item.split_code == "validation" for item in rows),
        created=created,
        summary=dict(run.summary_json),
    )


def _selection_boundary(
    session: Session, *, cohort_id: UUID, selection_code: str
) -> tuple[int, Decimal | None]:
    rankable = tuple(
        session.scalars(
            select(MultibaggerPredictionRow)
            .where(
                MultibaggerPredictionRow.multibagger_evaluation_cohort_id == cohort_id,
                MultibaggerPredictionRow.rankable.is_(True),
            )
            .order_by(MultibaggerPredictionRow.display_order)
        )
    )
    if selection_code.startswith("TOP_") and selection_code.endswith("_PERCENT"):
        percentage = int(selection_code.removeprefix("TOP_").removesuffix("_PERCENT"))
        count = 0 if not rankable else max(1, (len(rankable) * percentage + 99) // 100)
    else:
        count = min(int(selection_code.removeprefix("TOP_")), len(rankable))
    return count, None if count == 0 else rankable[count - 1].final_score


def _error_summary(
    *,
    facts: tuple[Any, ...],
    persisted_rows: list[MultibaggerErrorAnalysisRow],
    components: dict[UUID, dict[str, Decimal]],
    split_by_cohort: dict[UUID, Any],
) -> dict[str, object]:
    split_counts: dict[str, dict[str, int]] = {}
    for split in ("development", "validation"):
        rows = [item for item in persisted_rows if item.split_code == split]
        split_counts[split] = {
            "fp": sum(item.confusion_class == "fp" for item in rows),
            "fn": sum(item.confusion_class == "fn" for item in rows),
            "unrankable_positive_fn": sum(
                item.confusion_class == "fn" and not item.rankable for item in rows
            ),
            "rankable_positive_fn_below_boundary": sum(
                item.confusion_class == "fn" and item.rankable for item in rows
            ),
        }
    diagnostics: dict[str, Any] = {}
    for split in ("development", "validation"):
        split_facts = [
            (confusion, prediction, label)
            for confusion, prediction, label in facts
            if split_by_cohort[prediction.multibagger_evaluation_cohort_id].split_code == split
            and confusion.confusion_class in {"tp", "fp", "fn", "tn"}
        ]
        diagnostics[split] = {}
        for outcome in sorted({item[0].outcome_contract_code for item in split_facts}):
            diagnostics[split][outcome] = {}
            for selection in sorted({item[0].selection_contract_code for item in split_facts}):
                diagnostics[split][outcome][selection] = {}
                for view in sorted({item[0].evaluation_view for item in split_facts}):
                    cell = [
                        item
                        for item in split_facts
                        if item[0].outcome_contract_code == outcome
                        and item[0].selection_contract_code == selection
                        and item[0].evaluation_view == view
                    ]
                    codes = sorted(
                        {
                            str(code)
                            for _, prediction, _ in cell
                            for code in prediction.available_component_codes_json
                        }
                    )
                    diagnostics[split][outcome][selection][view] = {
                        code: _component_diagnostic(code, cell, components) for code in codes
                    }
    return {
        "holdout_outcomes_read": False,
        "development_validation_separate": True,
        "error_counts": split_counts,
        "component_diagnostics": diagnostics,
        "factual_rows": len(persisted_rows),
        "mature_source_confusion_rows": sum(
            confusion.confusion_class in {"tp", "fp", "fn", "tn"} for confusion, _, _ in facts
        ),
        "causal_claims": "none",
    }


def _component_diagnostic(
    code: str,
    facts: list[tuple[Any, Any, Any]],
    components: dict[UUID, dict[str, Decimal]],
) -> dict[str, object]:
    available = [
        (confusion, prediction)
        for confusion, prediction, _ in facts
        if code in prediction.available_component_codes_json
    ]
    positives = [
        (confusion, prediction)
        for confusion, prediction in available
        if confusion.confusion_class in {"tp", "fn"}
    ]
    negatives = [
        (confusion, prediction)
        for confusion, prediction in available
        if confusion.confusion_class in {"fp", "tn"}
    ]

    def values(items: list[tuple[Any, Any]]) -> list[Decimal]:
        result: list[Decimal] = []
        for _, prediction in items:
            if prediction.score_snapshot_id is not None:
                value = components.get(prediction.score_snapshot_id, {}).get(code)
                if value is not None:
                    result.append(value)
        return result

    positive_values = values(positives)
    negative_values = values(negatives)
    return {
        "availability_count": len(available),
        "missing_count": len(facts) - len(available),
        "mature_positive_count": len(positives),
        "mature_negative_count": len(negatives),
        "fp_count": sum(confusion.confusion_class == "fp" for confusion, _ in available),
        "fn_count": sum(confusion.confusion_class == "fn" for confusion, _ in available),
        "median_component_value_mature_positive": (
            None if not positive_values else str(median(positive_values))
        ),
        "median_component_value_mature_negative": (
            None if not negative_values else str(median(negative_values))
        ),
    }


def _error_signature(
    *,
    confusion_class: str,
    rankable: bool,
    rank_positions_below_boundary: int | None,
    score_bucket: str,
) -> str:
    if confusion_class == "fn" and not rankable:
        return "UNRANKABLE_POSITIVE_FN"
    if confusion_class == "fn" and rank_positions_below_boundary == 1:
        return "RANKABLE_JUST_BELOW_BOUNDARY"
    if confusion_class == "fp" and score_bucket == "[80,100]":
        return "HIGH_SCORE_NEGATIVE"
    if confusion_class == "fn" and score_bucket in {"[0,20)", "[20,40)"}:
        return "LOW_SCORE_EVENTUAL_POSITIVE"
    return "OTHER_FACTUAL_FP" if confusion_class == "fp" else "OTHER_FACTUAL_FN"


def _score_bucket(score: Decimal | None) -> str:
    if score is None:
        return "unrankable"
    for code, lower, upper, inclusive in (
        ("[0,20)", Decimal("0"), Decimal("20"), False),
        ("[20,40)", Decimal("20"), Decimal("40"), False),
        ("[40,60)", Decimal("40"), Decimal("60"), False),
        ("[60,80)", Decimal("60"), Decimal("80"), False),
        ("[80,100]", Decimal("80"), Decimal("100"), True),
    ):
        if score >= lower and (score <= upper if inclusive else score < upper):
            return code
    return "outside_j_v1_buckets"


def _ordered_unique_dates(values: tuple[date, ...]) -> tuple[date, ...]:
    ordered = tuple(sorted(values))
    if len(ordered) != len(set(ordered)):
        raise ValueError("official session dates contain duplicates")
    return ordered


def _add_calendar_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _stored_utc(value)
