"""Deterministic, non-persisted primitives over external attention evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from re import fullmatch
from uuid import UUID

from inflector_data.attention import ATTENTION_COVERAGE_STATUSES
from inflector_data.attention_pit import (
    PointInTimeAttentionObservation,
    PointInTimeAttentionReader,
)

ATTENTION_FEATURE_BUNDLE_VERSION = "attention_feature_bundle_v1"
NEWS_MENTIONS_COUNT_FEATURE_VERSION = "news_mentions_count_feature_v1"
NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION = "news_window_duration_days_v1"
NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION = "news_window_age_days_v1"
ANALYST_COVERAGE_COUNT_FEATURE_VERSION = "analyst_coverage_count_feature_v1"
ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION = "analyst_snapshot_age_days_v1"


@dataclass(frozen=True, slots=True)
class AttentionSeriesIdentity:
    """One exact caller-selected provider measurement series."""

    provider_dataset_id: UUID
    scope_code: str
    methodology_version: str
    measurement_definition_sha256: str

    def __post_init__(self) -> None:
        if not self.scope_code or self.scope_code != self.scope_code.strip():
            raise ValueError("scope_code must be non-empty and trimmed")
        if (
            not self.methodology_version
            or self.methodology_version != self.methodology_version.strip()
        ):
            raise ValueError("methodology_version must be non-empty and trimmed")
        if fullmatch(r"[0-9a-fA-F]{64}", self.measurement_definition_sha256) is None:
            raise ValueError("measurement_definition_sha256 must be a SHA-256 digest")
        object.__setattr__(
            self,
            "measurement_definition_sha256",
            self.measurement_definition_sha256.lower(),
        )


@dataclass(frozen=True, slots=True)
class AttentionFeatureValue:
    """One neutral numeric attention primitive with full source evidence."""

    code: str
    value: Decimal | None
    unit: str
    available_at: datetime | None
    warnings: tuple[str, ...]
    evidence: tuple[PointInTimeAttentionObservation, ...]
    algorithm_version: str


@dataclass(frozen=True, slots=True)
class AttentionFeatureBundle:
    """Caller-scoped attention facts; never a score or persisted snapshot."""

    company_id: UUID
    security_id: UUID | None
    company_level_only: bool
    as_of: datetime
    news_series: AttentionSeriesIdentity
    news_window_start_at: datetime
    news_window_end_at: datetime
    analyst_series: AttentionSeriesIdentity
    analyst_observation_on_or_before: date | None
    news_mentions_count: AttentionFeatureValue
    news_window_duration_days: AttentionFeatureValue
    news_window_age_days: AttentionFeatureValue
    analyst_coverage_count: AttentionFeatureValue
    analyst_snapshot_age_days: AttentionFeatureValue
    news_observation: PointInTimeAttentionObservation | None
    analyst_observation: PointInTimeAttentionObservation | None
    algorithm_version: str


class AttentionFeaturePrimitives:
    """Build exact raw features from explicitly selected PIT attention series."""

    def __init__(self, reader: PointInTimeAttentionReader) -> None:
        self._reader = reader

    def features_as_of(
        self,
        *,
        company_id: UUID,
        security_id: UUID | None,
        company_level_only: bool,
        as_of: datetime,
        news_series: AttentionSeriesIdentity,
        news_window_start_at: datetime,
        news_window_end_at: datetime,
        analyst_series: AttentionSeriesIdentity,
        analyst_observation_on_or_before: date | None,
    ) -> AttentionFeatureBundle:
        cutoff = _aware_utc(as_of, field="as_of")
        self._validate_security_mode(security_id, company_level_only)
        window_start = _aware_utc(news_window_start_at, field="news_window_start_at")
        window_end = _aware_utc(news_window_end_at, field="news_window_end_at")
        if window_start >= window_end:
            raise ValueError("news_window_start_at must be before news_window_end_at")

        news = self._reader.news_count_for_exact_window_as_of(
            provider_dataset_id=news_series.provider_dataset_id,
            company_id=company_id,
            scope_code=news_series.scope_code,
            methodology_version=news_series.methodology_version,
            measurement_definition_sha256=news_series.measurement_definition_sha256,
            window_start_at=window_start,
            window_end_at=window_end,
            as_of=cutoff,
            security_id=security_id,
            company_level_only=company_level_only,
        )
        analyst = self._reader.latest_analyst_coverage_as_of(
            provider_dataset_id=analyst_series.provider_dataset_id,
            company_id=company_id,
            scope_code=analyst_series.scope_code,
            methodology_version=analyst_series.methodology_version,
            measurement_definition_sha256=analyst_series.measurement_definition_sha256,
            as_of=cutoff,
            security_id=security_id,
            company_level_only=company_level_only,
            observation_on_or_before=analyst_observation_on_or_before,
        )
        if news is not None:
            self._validate_news_observation(
                news,
                company_id=company_id,
                security_id=security_id,
                company_level_only=company_level_only,
                series=news_series,
                window_start=window_start,
                window_end=window_end,
                cutoff=cutoff,
            )
        if analyst is not None:
            self._validate_analyst_observation(
                analyst,
                company_id=company_id,
                security_id=security_id,
                company_level_only=company_level_only,
                series=analyst_series,
                observation_on_or_before=analyst_observation_on_or_before,
                cutoff=cutoff,
            )

        return AttentionFeatureBundle(
            company_id=company_id,
            security_id=security_id,
            company_level_only=company_level_only,
            as_of=cutoff,
            news_series=news_series,
            news_window_start_at=window_start,
            news_window_end_at=window_end,
            analyst_series=analyst_series,
            analyst_observation_on_or_before=analyst_observation_on_or_before,
            news_mentions_count=self._count_feature(
                observation=news,
                code="news_mentions_count",
                unit="count",
                algorithm_version=NEWS_MENTIONS_COUNT_FEATURE_VERSION,
                missing_warning="missing_news_mentions_observation",
                partial_warning="partial_news_attention_coverage",
                unknown_warning="unknown_news_attention_coverage",
            ),
            news_window_duration_days=self._news_duration(news),
            news_window_age_days=self._news_age(news, cutoff),
            analyst_coverage_count=self._count_feature(
                observation=analyst,
                code="analyst_coverage_count",
                unit="count",
                algorithm_version=ANALYST_COVERAGE_COUNT_FEATURE_VERSION,
                missing_warning="missing_analyst_coverage_observation",
                partial_warning="partial_analyst_attention_coverage",
                unknown_warning="unknown_analyst_attention_coverage",
            ),
            analyst_snapshot_age_days=self._analyst_age(analyst, cutoff),
            news_observation=news,
            analyst_observation=analyst,
            algorithm_version=ATTENTION_FEATURE_BUNDLE_VERSION,
        )

    @staticmethod
    def _count_feature(
        *,
        observation: PointInTimeAttentionObservation | None,
        code: str,
        unit: str,
        algorithm_version: str,
        missing_warning: str,
        partial_warning: str,
        unknown_warning: str,
    ) -> AttentionFeatureValue:
        if observation is None:
            return AttentionFeatureValue(
                code=code,
                value=None,
                unit=unit,
                available_at=None,
                warnings=(missing_warning,),
                evidence=(),
                algorithm_version=algorithm_version,
            )
        warning_by_coverage = {
            "partial": partial_warning,
            "unknown": unknown_warning,
        }
        warning = warning_by_coverage.get(observation.coverage_status)
        return AttentionFeatureValue(
            code=code,
            value=(
                Decimal(observation.reported_count)
                if observation.coverage_status == "complete"
                else None
            ),
            unit=unit,
            available_at=observation.available_at,
            warnings=() if warning is None else (warning,),
            evidence=(observation,),
            algorithm_version=algorithm_version,
        )

    @staticmethod
    def _news_duration(
        observation: PointInTimeAttentionObservation | None,
    ) -> AttentionFeatureValue:
        if observation is None:
            return _missing_time_feature(
                code="news_window_duration_days",
                algorithm_version=NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION,
                warning="missing_news_mentions_observation",
            )
        assert observation.window_start_at is not None
        assert observation.window_end_at is not None
        return AttentionFeatureValue(
            code="news_window_duration_days",
            value=_decimal_days(observation.window_end_at - observation.window_start_at),
            unit="days",
            available_at=observation.available_at,
            warnings=(),
            evidence=(observation,),
            algorithm_version=NEWS_WINDOW_DURATION_DAYS_FEATURE_VERSION,
        )

    @staticmethod
    def _news_age(
        observation: PointInTimeAttentionObservation | None,
        cutoff: datetime,
    ) -> AttentionFeatureValue:
        if observation is None:
            return _missing_time_feature(
                code="news_window_age_days",
                algorithm_version=NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION,
                warning="missing_news_mentions_observation",
            )
        assert observation.window_end_at is not None
        age = _decimal_days(cutoff - observation.window_end_at)
        if age < 0:
            raise ValueError("news window cannot end after as_of")
        return AttentionFeatureValue(
            code="news_window_age_days",
            value=age,
            unit="days",
            available_at=observation.available_at,
            warnings=(),
            evidence=(observation,),
            algorithm_version=NEWS_WINDOW_AGE_DAYS_FEATURE_VERSION,
        )

    @staticmethod
    def _analyst_age(
        observation: PointInTimeAttentionObservation | None,
        cutoff: datetime,
    ) -> AttentionFeatureValue:
        if observation is None:
            return _missing_time_feature(
                code="analyst_snapshot_age_days",
                algorithm_version=ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION,
                warning="missing_analyst_coverage_observation",
            )
        assert observation.observation_date is not None
        age_days = (cutoff.date() - observation.observation_date).days
        if age_days < 0:
            raise ValueError("analyst observation date cannot be after as_of")
        return AttentionFeatureValue(
            code="analyst_snapshot_age_days",
            value=Decimal(age_days),
            unit="days",
            available_at=observation.available_at,
            warnings=(),
            evidence=(observation,),
            algorithm_version=ANALYST_SNAPSHOT_AGE_DAYS_FEATURE_VERSION,
        )

    @staticmethod
    def _validate_security_mode(
        security_id: UUID | None, company_level_only: bool
    ) -> None:
        if company_level_only == (security_id is not None):
            raise ValueError("select either company-level evidence or one explicit security")

    @classmethod
    def _validate_common_observation(
        cls,
        observation: PointInTimeAttentionObservation,
        *,
        company_id: UUID,
        security_id: UUID | None,
        company_level_only: bool,
        series: AttentionSeriesIdentity,
        cutoff: datetime,
    ) -> None:
        expected_security = None if company_level_only else security_id
        if observation.company_id != company_id:
            raise ValueError("attention observation company mismatch")
        if observation.security_id != expected_security:
            raise ValueError("attention observation security mode mismatch")
        if observation.provider_dataset_id != series.provider_dataset_id:
            raise ValueError("attention observation provider mismatch")
        if observation.scope_code != series.scope_code:
            raise ValueError("attention observation scope mismatch")
        if observation.methodology_version != series.methodology_version:
            raise ValueError("attention observation methodology mismatch")
        if observation.measurement_definition_sha256.lower() != (
            series.measurement_definition_sha256
        ):
            raise ValueError("attention observation definition mismatch")
        available_at = _aware_utc(observation.available_at, field="observation.available_at")
        if available_at > cutoff:
            raise ValueError("attention observation was not available at as_of")
        if observation.reported_unit != "count":
            raise ValueError("attention observation unit must be count")
        if observation.coverage_status not in ATTENTION_COVERAGE_STATUSES:
            raise ValueError("attention observation coverage status is unsupported")
        if type(observation.reported_count) is not int:
            raise ValueError("attention observation count must be an integer")
        if observation.reported_count < 0:
            raise ValueError("attention observation count cannot be negative")
        if observation.source_record.validation_status != "accepted":
            raise ValueError("attention observation source must be accepted")

    @classmethod
    def _validate_news_observation(
        cls,
        observation: PointInTimeAttentionObservation,
        *,
        company_id: UUID,
        security_id: UUID | None,
        company_level_only: bool,
        series: AttentionSeriesIdentity,
        window_start: datetime,
        window_end: datetime,
        cutoff: datetime,
    ) -> None:
        cls._validate_common_observation(
            observation,
            company_id=company_id,
            security_id=security_id,
            company_level_only=company_level_only,
            series=series,
            cutoff=cutoff,
        )
        if observation.metric_code != "news_mentions_count":
            raise ValueError("news observation metric mismatch")
        if observation.observation_date is not None:
            raise ValueError("news observation cannot have an observation date")
        if observation.window_start_at is None or observation.window_end_at is None:
            raise ValueError("news observation window is incomplete")
        start = _aware_utc(observation.window_start_at, field="news.window_start_at")
        end = _aware_utc(observation.window_end_at, field="news.window_end_at")
        if start != window_start or end != window_end:
            raise ValueError("news observation exact window mismatch")
        available_at = _aware_utc(
            observation.available_at, field="news.available_at"
        )
        if start >= end or end > available_at or end > cutoff:
            raise ValueError("news observation window is invalid at as_of")

    @classmethod
    def _validate_analyst_observation(
        cls,
        observation: PointInTimeAttentionObservation,
        *,
        company_id: UUID,
        security_id: UUID | None,
        company_level_only: bool,
        series: AttentionSeriesIdentity,
        observation_on_or_before: date | None,
        cutoff: datetime,
    ) -> None:
        cls._validate_common_observation(
            observation,
            company_id=company_id,
            security_id=security_id,
            company_level_only=company_level_only,
            series=series,
            cutoff=cutoff,
        )
        if observation.metric_code != "analyst_coverage_count":
            raise ValueError("analyst observation metric mismatch")
        if observation.observation_date is None:
            raise ValueError("analyst observation date is required")
        if observation.window_start_at is not None or observation.window_end_at is not None:
            raise ValueError("analyst observation cannot have a window")
        if observation.observation_date > cutoff.date():
            raise ValueError("analyst observation date cannot be after as_of")
        available_at = _aware_utc(
            observation.available_at, field="analyst.available_at"
        )
        if observation.observation_date > available_at.date():
            raise ValueError("analyst observation date cannot be after availability")
        if (
            observation_on_or_before is not None
            and observation.observation_date > observation_on_or_before
        ):
            raise ValueError("analyst observation exceeds the requested date bound")


def _missing_time_feature(
    *, code: str, algorithm_version: str, warning: str
) -> AttentionFeatureValue:
    return AttentionFeatureValue(
        code=code,
        value=None,
        unit="days",
        available_at=None,
        warnings=(warning,),
        evidence=(),
        algorithm_version=algorithm_version,
    )


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_days(value: timedelta) -> Decimal:
    total_microseconds = (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )
    return Decimal(total_microseconds) / Decimal(86_400_000_000)
