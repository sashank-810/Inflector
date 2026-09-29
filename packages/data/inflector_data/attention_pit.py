"""Point-in-time reads for provider-isolated external attention evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from re import fullmatch
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from inflector_data.attention import ATTENTION_COVERAGE_STATUSES, ATTENTION_METRIC_CODES
from inflector_data.pit import SourceRecordView
from inflector_database.models import AttentionObservation, Security, SourceRecord


class AttentionDataIntegrityError(ValueError):
    """Raised when immutable attention revisions cannot be selected safely."""


@dataclass(frozen=True, slots=True)
class PointInTimeAttentionObservation:
    id: UUID
    company_id: UUID
    security_id: UUID | None
    provider_dataset_id: UUID
    metric_code: str
    reported_count: int
    reported_unit: str
    scope_code: str
    methodology_version: str
    measurement_definition_sha256: str
    coverage_status: str
    observation_date: date | None
    window_start_at: datetime | None
    window_end_at: datetime | None
    available_at: datetime
    revision_at: datetime | None
    ingested_at: datetime
    source_record: SourceRecordView


_AttentionRow = tuple[AttentionObservation, SourceRecord]


class PointInTimeAttentionReader:
    """Resolve accepted revisions within one explicit provider measurement series."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def observations_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        metric_code: str,
        scope_code: str,
        methodology_version: str,
        measurement_definition_sha256: str,
        as_of: datetime,
        security_id: UUID | None,
        company_level_only: bool,
    ) -> tuple[PointInTimeAttentionObservation, ...]:
        """Return corrected observations for exactly one declared semantic series."""

        cutoff = self._cutoff(as_of)
        self._validate_series(
            metric_code,
            scope_code,
            methodology_version,
            measurement_definition_sha256,
            security_id,
            company_level_only,
        )
        statement = (
            select(AttentionObservation, SourceRecord)
            .join(SourceRecord, AttentionObservation.source_record_id == SourceRecord.id)
            .where(
                AttentionObservation.provider_dataset_id == provider_dataset_id,
                AttentionObservation.available_at <= cutoff,
                SourceRecord.validation_status == "accepted",
            )
        )
        rows = [(row[0], row[1]) for row in self._session.execute(statement)]
        by_external: dict[str, list[_AttentionRow]] = {}
        for row in rows:
            by_external.setdefault(row[1].external_record_id, []).append(row)
        source_selected = [
            self._latest(rows_for_external, identity="provider external record")
            for rows_for_external in by_external.values()
        ]
        source_selected = [
            row
            for row in source_selected
            if self._matches_series(
                row[0],
                company_id=company_id,
                security_id=security_id,
                company_level_only=company_level_only,
                metric_code=metric_code,
                scope_code=scope_code,
                methodology_version=methodology_version,
                measurement_definition_sha256=measurement_definition_sha256.lower(),
            )
        ]
        by_economic: dict[tuple[object, ...], list[_AttentionRow]] = {}
        for row in source_selected:
            by_economic.setdefault(self._economic_identity(row[0]), []).append(row)
        resolved = [
            self._latest(rows_for_identity, identity="attention economic identity")
            for rows_for_identity in by_economic.values()
        ]
        resolved.sort(key=self._result_order)
        return tuple(self._view(row) for row in resolved)

    def news_count_for_exact_window_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        scope_code: str,
        methodology_version: str,
        measurement_definition_sha256: str,
        window_start_at: datetime,
        window_end_at: datetime,
        as_of: datetime,
        security_id: UUID | None,
        company_level_only: bool,
    ) -> PointInTimeAttentionObservation | None:
        start = self._cutoff(window_start_at, name="window_start_at")
        end = self._cutoff(window_end_at, name="window_end_at")
        if start >= end:
            raise ValueError("window_start_at must be before window_end_at")
        matches = tuple(
            observation
            for observation in self.observations_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                metric_code="news_mentions_count",
                scope_code=scope_code,
                methodology_version=methodology_version,
                measurement_definition_sha256=measurement_definition_sha256,
                as_of=as_of,
                security_id=security_id,
                company_level_only=company_level_only,
            )
            if observation.window_start_at == start and observation.window_end_at == end
        )
        if len(matches) > 1:
            raise AttentionDataIntegrityError("ambiguous exact-window news observation")
        return matches[0] if matches else None

    def latest_analyst_coverage_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        scope_code: str,
        methodology_version: str,
        measurement_definition_sha256: str,
        as_of: datetime,
        security_id: UUID | None,
        company_level_only: bool,
        observation_on_or_before: date | None = None,
    ) -> PointInTimeAttentionObservation | None:
        observations = tuple(
            observation
            for observation in self.observations_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                metric_code="analyst_coverage_count",
                scope_code=scope_code,
                methodology_version=methodology_version,
                measurement_definition_sha256=measurement_definition_sha256,
                as_of=as_of,
                security_id=security_id,
                company_level_only=company_level_only,
            )
            if observation_on_or_before is None
            or (
                observation.observation_date is not None
                and observation.observation_date <= observation_on_or_before
            )
        )
        if not observations:
            return None
        latest_date = max(
            observation.observation_date
            for observation in observations
            if observation.observation_date is not None
        )
        latest = tuple(
            observation
            for observation in observations
            if observation.observation_date == latest_date
        )
        if len(latest) != 1:
            raise AttentionDataIntegrityError("ambiguous latest analyst coverage snapshot")
        return latest[0]

    @classmethod
    def _latest(cls, rows: list[_AttentionRow], *, identity: str) -> _AttentionRow:
        latest_order = max(cls._revision_order(row[0]) for row in rows)
        latest = [row for row in rows if cls._revision_order(row[0]) == latest_order]
        payloads = {cls._semantic_payload(row[0]) for row in latest}
        if len(payloads) > 1:
            raise AttentionDataIntegrityError(f"ambiguous changed revisions for {identity}")
        return min(latest, key=lambda row: str(row[0].id))

    @staticmethod
    def _semantic_payload(observation: AttentionObservation) -> tuple[object, ...]:
        return (
            observation.company_id,
            observation.security_id,
            observation.metric_code,
            observation.reported_count,
            observation.reported_unit,
            observation.scope_code,
            observation.methodology_version,
            observation.measurement_definition_sha256,
            observation.coverage_status,
            observation.observation_date,
            PointInTimeAttentionReader._as_utc_or_none(observation.window_start_at),
            PointInTimeAttentionReader._as_utc_or_none(observation.window_end_at),
        )

    @staticmethod
    def _matches_series(
        observation: AttentionObservation,
        *,
        company_id: UUID,
        security_id: UUID | None,
        company_level_only: bool,
        metric_code: str,
        scope_code: str,
        methodology_version: str,
        measurement_definition_sha256: str,
    ) -> bool:
        expected_security_id = None if company_level_only else security_id
        return (
            observation.company_id == company_id
            and observation.security_id == expected_security_id
            and observation.metric_code == metric_code
            and observation.scope_code == scope_code
            and observation.methodology_version == methodology_version
            and observation.measurement_definition_sha256 == measurement_definition_sha256
        )

    @classmethod
    def _revision_order(cls, observation: AttentionObservation) -> tuple[datetime, datetime]:
        available = cls._as_utc(observation.available_at)
        revision = cls._as_utc(observation.revision_at or observation.available_at)
        return available, revision

    @classmethod
    def _economic_identity(cls, observation: AttentionObservation) -> tuple[object, ...]:
        common: tuple[object, ...] = (
            observation.company_id,
            observation.security_id,
            observation.metric_code,
            observation.scope_code,
            observation.methodology_version,
            observation.measurement_definition_sha256,
        )
        if observation.metric_code == "news_mentions_count":
            return common + (
                cls._as_utc_or_none(observation.window_start_at),
                cls._as_utc_or_none(observation.window_end_at),
            )
        return common + (observation.observation_date,)

    @classmethod
    def _result_order(cls, row: _AttentionRow) -> tuple[object, ...]:
        observation = row[0]
        economic = cls._economic_identity(observation)
        return tuple("" if item is None else str(item) for item in economic) + (
            cls._revision_order(observation),
            str(observation.id),
        )

    def _view(self, row: _AttentionRow) -> PointInTimeAttentionObservation:
        observation, source = row
        if source.provider_dataset_id != observation.provider_dataset_id:
            raise AttentionDataIntegrityError("attention source provider mismatch")
        if source.available_at is None or self._as_utc(source.available_at) != self._as_utc(
            observation.available_at
        ):
            raise AttentionDataIntegrityError("attention source availability mismatch")
        if observation.security_id is not None:
            security = self._session.get(Security, observation.security_id)
            if security is None or security.company_id != observation.company_id:
                raise AttentionDataIntegrityError("attention company/security mismatch")
        self._validate_persisted_observation(observation)
        return PointInTimeAttentionObservation(
            id=observation.id,
            company_id=observation.company_id,
            security_id=observation.security_id,
            provider_dataset_id=observation.provider_dataset_id,
            metric_code=observation.metric_code,
            reported_count=observation.reported_count,
            reported_unit=observation.reported_unit,
            scope_code=observation.scope_code,
            methodology_version=observation.methodology_version,
            measurement_definition_sha256=observation.measurement_definition_sha256,
            coverage_status=observation.coverage_status,
            observation_date=observation.observation_date,
            window_start_at=self._as_utc_or_none(observation.window_start_at),
            window_end_at=self._as_utc_or_none(observation.window_end_at),
            available_at=self._as_utc(observation.available_at),
            revision_at=self._as_utc_or_none(observation.revision_at),
            ingested_at=self._as_utc(observation.ingested_at),
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

    @classmethod
    def _validate_persisted_observation(cls, observation: AttentionObservation) -> None:
        if observation.metric_code not in ATTENTION_METRIC_CODES:
            raise AttentionDataIntegrityError("unsupported persisted attention metric")
        if observation.reported_count < 0 or observation.reported_unit != "count":
            raise AttentionDataIntegrityError("invalid persisted attention count")
        if observation.coverage_status not in ATTENTION_COVERAGE_STATUSES:
            raise AttentionDataIntegrityError("invalid persisted attention coverage")
        if not observation.scope_code or observation.scope_code != observation.scope_code.strip():
            raise AttentionDataIntegrityError("invalid persisted attention scope")
        if (
            not observation.methodology_version
            or observation.methodology_version != observation.methodology_version.strip()
        ):
            raise AttentionDataIntegrityError("invalid persisted attention methodology")
        if fullmatch(r"[0-9a-f]{64}", observation.measurement_definition_sha256) is None:
            raise AttentionDataIntegrityError("invalid persisted measurement definition hash")
        available_at = cls._as_utc(observation.available_at)
        if observation.metric_code == "news_mentions_count":
            start = cls._as_utc_or_none(observation.window_start_at)
            end = cls._as_utc_or_none(observation.window_end_at)
            if (
                observation.observation_date is not None
                or start is None
                or end is None
                or start >= end
                or end > available_at
            ):
                raise AttentionDataIntegrityError("invalid persisted news window")
        elif (
            observation.observation_date is None
            or observation.observation_date > available_at.date()
            or observation.window_start_at is not None
            or observation.window_end_at is not None
        ):
            raise AttentionDataIntegrityError("invalid persisted analyst snapshot")

    @staticmethod
    def _validate_series(
        metric_code: str,
        scope_code: str,
        methodology_version: str,
        measurement_definition_sha256: str,
        security_id: UUID | None,
        company_level_only: bool,
    ) -> None:
        if metric_code not in ATTENTION_METRIC_CODES:
            raise ValueError("unsupported attention metric")
        if not scope_code or scope_code != scope_code.strip():
            raise ValueError("scope_code must be non-empty and trimmed")
        if not methodology_version or methodology_version != methodology_version.strip():
            raise ValueError("methodology_version must be non-empty and trimmed")
        if fullmatch(r"[0-9a-fA-F]{64}", measurement_definition_sha256) is None:
            raise ValueError("measurement_definition_sha256 must be a SHA-256 digest")
        if company_level_only == (security_id is not None):
            raise ValueError("select either company-level evidence or one explicit security")

    @staticmethod
    def _cutoff(value: datetime, *, name: str = "as_of") -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _as_utc_or_none(cls, value: datetime | None) -> datetime | None:
        return None if value is None else cls._as_utc(value)
