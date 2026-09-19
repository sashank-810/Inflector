"""Point-in-time, same-period snapshots for instant financial metrics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from inflector_data.pit import (
    FinancialPeriodView,
    PointInTimeFinancialFact,
    PointInTimeFinancialReader,
)

INSTANT_SNAPSHOT_VERSION = "instant_snapshot_v1"


@dataclass(frozen=True, slots=True)
class InstantSnapshotComponent:
    """One eligible PIT fact in an instant financial snapshot."""

    metric_code: str
    value: Decimal
    unit: str
    fact: PointInTimeFinancialFact


@dataclass(frozen=True, slots=True)
class InstantFinancialSnapshot:
    """A complete set of instant metrics from one stored fiscal period."""

    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    fiscal_period: FinancialPeriodView
    components: tuple[InstantSnapshotComponent, ...]
    as_of: datetime
    available_at: datetime
    algorithm_version: str


class InstantFinancialSnapshotReader:
    """Build PIT-clean snapshots without mixing fiscal periods or evidence contexts."""

    def __init__(self, financial_reader: PointInTimeFinancialReader) -> None:
        self._financial_reader = financial_reader

    def snapshot_for_period_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_period_id: UUID,
        metric_codes: Sequence[str],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        """Return all requested eligible metrics for one exact stored period."""

        requested_metrics = self._validated_metric_codes(metric_codes)
        cutoff = self._knowledge_cutoff(as_of)
        facts: list[PointInTimeFinancialFact] = []
        for metric_code in requested_metrics:
            fact = self._financial_reader.financial_fact_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                fiscal_period_id=fiscal_period_id,
                filing_scope=filing_scope,
                metric_code=metric_code,
                as_of=cutoff,
            )
            if fact is None or not self._eligible(
                fact,
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                fiscal_period_id=fiscal_period_id,
            ):
                return None
            facts.append(fact)
        return self._snapshot(
            facts,
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            as_of=cutoff,
        )

    def latest_common_snapshot_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_codes: Sequence[str],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        """Return the latest economic period common to all requested metrics.

        Economic recency is determined only by ``period_end``. If multiple
        distinct common period IDs share the maximum end date, selection fails
        closed because this layer has no quarter/annual preference policy.
        """

        requested_metrics = self._validated_metric_codes(metric_codes)
        cutoff = self._knowledge_cutoff(as_of)
        facts_by_metric = self._eligible_facts_by_metric(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=requested_metrics,
            as_of=cutoff,
        )
        if facts_by_metric is None:
            return None
        common_period_ids = self._common_period_ids(facts_by_metric, requested_metrics)
        if not common_period_ids:
            return None

        first_metric = requested_metrics[0]
        latest_period_end = max(
            facts_by_metric[first_metric][period_id].fiscal_period.period_end
            for period_id in common_period_ids
        )
        latest_period_ids = [
            period_id
            for period_id in common_period_ids
            if facts_by_metric[first_metric][period_id].fiscal_period.period_end
            == latest_period_end
        ]
        if len(latest_period_ids) != 1:
            return None

        selected_period_id = latest_period_ids[0]
        return self._snapshot(
            [facts_by_metric[metric_code][selected_period_id] for metric_code in requested_metrics],
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            as_of=cutoff,
        )

    def snapshot_for_fiscal_endpoint_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_year: int,
        fiscal_quarter: int,
        metric_codes: Sequence[str],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        """Return one unambiguous complete snapshot for an exact fiscal FY/Q.

        FY/Q labels can legitimately identify more than one stored period kind.
        This layer has no preference policy for that ambiguity, so it fails closed.
        """

        if fiscal_quarter not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter must be between 1 and 4")
        requested_metrics = self._validated_metric_codes(metric_codes)
        cutoff = self._knowledge_cutoff(as_of)
        facts_by_metric = self._eligible_facts_by_metric(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=requested_metrics,
            as_of=cutoff,
        )
        if facts_by_metric is None:
            return None
        first_metric = requested_metrics[0]
        matching_period_ids = [
            period_id
            for period_id in self._common_period_ids(facts_by_metric, requested_metrics)
            if (
                facts_by_metric[first_metric][period_id].fiscal_period.fiscal_year == fiscal_year
                and facts_by_metric[first_metric][period_id].fiscal_period.fiscal_quarter
                == fiscal_quarter
            )
        ]
        if len(matching_period_ids) != 1:
            return None
        selected_period_id = matching_period_ids[0]
        return self._snapshot(
            [facts_by_metric[metric_code][selected_period_id] for metric_code in requested_metrics],
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            as_of=cutoff,
        )

    def common_snapshot_for_period_end_as_of(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        period_end: date,
        metric_codes: Sequence[str],
        as_of: datetime,
    ) -> InstantFinancialSnapshot | None:
        """Return one unambiguous complete snapshot ending on an exact date."""

        requested_metrics = self._validated_metric_codes(metric_codes)
        cutoff = self._knowledge_cutoff(as_of)
        facts_by_metric = self._eligible_facts_by_metric(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            metric_codes=requested_metrics,
            as_of=cutoff,
        )
        if facts_by_metric is None:
            return None
        first_metric = requested_metrics[0]
        matching_period_ids = [
            period_id
            for period_id in self._common_period_ids(facts_by_metric, requested_metrics)
            if facts_by_metric[first_metric][period_id].fiscal_period.period_end == period_end
        ]
        if len(matching_period_ids) != 1:
            return None
        selected_period_id = matching_period_ids[0]
        return self._snapshot(
            [facts_by_metric[metric_code][selected_period_id] for metric_code in requested_metrics],
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            as_of=cutoff,
        )

    def _eligible_facts_by_metric(
        self,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        metric_codes: tuple[str, ...],
        as_of: datetime,
    ) -> dict[str, dict[UUID, PointInTimeFinancialFact]] | None:
        facts_by_metric: dict[str, dict[UUID, PointInTimeFinancialFact]] = {}
        for metric_code in metric_codes:
            series = self._financial_reader.financial_series_as_of(
                provider_dataset_id=provider_dataset_id,
                company_id=company_id,
                filing_scope=filing_scope,
                metric_code=metric_code,
                as_of=as_of,
            )
            eligible = {
                fact.fiscal_period.id: fact
                for fact in series
                if self._eligible(
                    fact,
                    provider_dataset_id=provider_dataset_id,
                    company_id=company_id,
                    filing_scope=filing_scope,
                    fiscal_period_id=fact.fiscal_period.id,
                )
            }
            if not eligible:
                return None
            facts_by_metric[metric_code] = eligible
        return facts_by_metric

    @staticmethod
    def _common_period_ids(
        facts_by_metric: dict[str, dict[UUID, PointInTimeFinancialFact]],
        metric_codes: tuple[str, ...],
    ) -> set[UUID]:
        return set.intersection(
            *(set(facts_by_metric[metric_code]) for metric_code in metric_codes)
        )

    @staticmethod
    def _validated_metric_codes(metric_codes: Sequence[str]) -> tuple[str, ...]:
        requested = tuple(metric_codes)
        if not requested:
            raise ValueError("metric_codes must contain at least one metric")
        if len(set(requested)) != len(requested):
            raise ValueError("metric_codes must not contain duplicates")
        return tuple(sorted(requested))

    @staticmethod
    def _knowledge_cutoff(as_of: datetime) -> datetime:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)

    @staticmethod
    def _eligible(
        fact: PointInTimeFinancialFact,
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        fiscal_period_id: UUID,
    ) -> bool:
        return (
            fact.provider_dataset_id == provider_dataset_id
            and fact.company_id == company_id
            and fact.filing.filing_scope == filing_scope
            and fact.fiscal_period.id == fiscal_period_id
            and fact.metric.semantic_type == "instant"
            and fact.metric.unit_category == "monetary"
            and isinstance(fact.normalized_value, Decimal)
            and fact.normalized_unit == "INR"
        )

    @staticmethod
    def _snapshot(
        facts: list[PointInTimeFinancialFact],
        *,
        provider_dataset_id: UUID,
        company_id: UUID,
        filing_scope: str,
        as_of: datetime,
    ) -> InstantFinancialSnapshot:
        period_ids = {fact.fiscal_period.id for fact in facts}
        if len(period_ids) != 1:
            raise ValueError("snapshot facts must share one fiscal period")
        ordered_facts = sorted(facts, key=lambda fact: fact.metric_code)
        components = tuple(
            InstantSnapshotComponent(
                metric_code=fact.metric_code,
                value=fact.normalized_value,
                unit=fact.normalized_unit,
                fact=fact,
            )
            for fact in ordered_facts
            if fact.normalized_value is not None and fact.normalized_unit is not None
        )
        return InstantFinancialSnapshot(
            provider_dataset_id=provider_dataset_id,
            company_id=company_id,
            filing_scope=filing_scope,
            fiscal_period=ordered_facts[0].fiscal_period,
            components=components,
            as_of=as_of,
            available_at=max(component.fact.available_at for component in components),
            algorithm_version=INSTANT_SNAPSHOT_VERSION,
        )
