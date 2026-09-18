"""Phase 3F PIT growth-window, consistency, and persistence tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from inflector_core.providers import ProviderMetadata
from inflector_data.archive import LocalRawObjectStore
from inflector_data.financial_features import FinancialInflectionFeatures, QuarterGrowthValue
from inflector_data.growth_history_features import GrowthHistoryFeatures
from inflector_data.period_normalization import FiscalQuarterNormalizer, QuarterizedFinancialValue
from inflector_data.pit import PointInTimeFinancialReader
from inflector_data.providers import CSVFinancialsProvider, CSVUniverseProvider
from inflector_data.service import IngestionService
from inflector_database.models import Company, DataProvider, ProviderDataset

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED_AT = datetime(2030, 1, 1, tzinfo=UTC)
UNIVERSE = ProviderMetadata("synthetic_csv", "csv", "universe", "synthetic-development-only")
HISTORY = ProviderMetadata(
    "growth_history", "csv", "financials", "synthetic-development-only"
)
PROVIDER_A = ProviderMetadata(
    "growth_history_a", "csv", "financials", "synthetic-development-only"
)
PROVIDER_B = ProviderMetadata(
    "growth_history_b", "csv", "financials", "synthetic-development-only"
)
SCOPE = ProviderMetadata(
    "growth_history_scope", "csv", "financials", "synthetic-development-only"
)


class _WindowRequest(TypedDict):
    provider_dataset_id: UUID
    company_id: UUID
    filing_scope: str
    metric_code: str
    fiscal_year: int
    fiscal_quarter: int
    window_size: int
    as_of: datetime


def _at(
    year: int,
    month: int,
    day: int,
    hour: int = 12,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _period(year: int, quarter: int) -> tuple[date, date]:
    periods = {
        (2025, 1): (date(2025, 4, 1), date(2025, 6, 30)),
        (2025, 2): (date(2025, 7, 1), date(2025, 9, 30)),
        (2025, 3): (date(2025, 10, 1), date(2025, 12, 31)),
        (2025, 4): (date(2026, 1, 1), date(2026, 3, 31)),
        (2026, 1): (date(2026, 4, 1), date(2026, 6, 30)),
        (2026, 2): (date(2026, 7, 1), date(2026, 9, 30)),
        (2026, 3): (date(2026, 10, 1), date(2026, 12, 31)),
        (2026, 4): (date(2027, 1, 1), date(2027, 3, 31)),
        (2027, 1): (date(2027, 4, 1), date(2027, 6, 30)),
        (2027, 2): (date(2027, 7, 1), date(2027, 9, 30)),
    }
    return periods[(year, quarter)]


def _quarter(
    year: int,
    quarter: int,
    *,
    provider_dataset_id: UUID,
    company_id: UUID,
    start: date | None = None,
    end: date | None = None,
) -> QuarterizedFinancialValue:
    default_start, default_end = _period(year, quarter)
    return QuarterizedFinancialValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_code="revenue",
        fiscal_year=year,
        fiscal_quarter=quarter,
        period_start=start or default_start,
        period_end=end or default_end,
        value=Decimal("100"),
        unit="INR",
        derivation_kind="reported",
        operation="reported",
        algorithm_version="period_normalization_v1",
        as_of=_at(2028, 1, 31),
        available_at=_at(2027, 1, 1),
        lineage=(),
    )


def _growth(
    year: int,
    quarter: int,
    value: str | None,
    *,
    provider_dataset_id: UUID,
    company_id: UUID,
    mode: str = "percentage_change",
    available_at: datetime | None = None,
    start: date | None = None,
    end: date | None = None,
) -> QuarterGrowthValue:
    current = _quarter(
        year,
        quarter,
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        start=start,
        end=end,
    )
    return QuarterGrowthValue(
        provider_dataset_id=provider_dataset_id,
        company_id=company_id,
        filing_scope="standalone",
        metric_code="revenue",
        ending_fiscal_year=year,
        ending_fiscal_quarter=quarter,
        comparison_kind="yoy",
        calculation_mode=mode,
        value=Decimal(value) if value is not None else None,
        absolute_change=Decimal("0"),
        unit="ratio" if mode == "percentage_change" else "INR",
        as_of=_at(2028, 1, 31),
        available_at=available_at or _at(2027, 1, 1),
        algorithm_version="growth_v1",
        current_quarter=current,
        comparison_quarter=current,
        transition=None,
        warnings=() if mode == "percentage_change" else ("non_positive_comparison_base",),
    )


class _GrowthSeriesStub:
    def __init__(self, observations: list[QuarterGrowthValue]) -> None:
        self._observations = observations

    def growth_series_as_of(self, **kwargs: object) -> list[QuarterGrowthValue]:
        as_of = kwargs["as_of"]
        assert isinstance(as_of, datetime)
        return [value for value in self._observations if value.available_at <= as_of]


def _history(observations: list[QuarterGrowthValue]) -> GrowthHistoryFeatures:
    return GrowthHistoryFeatures(cast(FinancialInflectionFeatures, _GrowthSeriesStub(observations)))


def _unit_series(
    values: tuple[str | None, ...] = ("0.1", "0.2", "0.3", "0.4"),
    *,
    modes: tuple[str, ...] | None = None,
    available: tuple[datetime, ...] | None = None,
) -> tuple[list[QuarterGrowthValue], UUID, UUID]:
    dataset_id, company_id = uuid4(), uuid4()
    endpoints = ((2026, 1), (2026, 2), (2026, 3), (2026, 4))
    observations = [
        _growth(
            year,
            quarter,
            value,
            provider_dataset_id=dataset_id,
            company_id=company_id,
            mode=modes[index] if modes else "percentage_change",
            available_at=available[index] if available else _at(2027, 1, 1),
        )
        for index, ((year, quarter), value) in enumerate(zip(endpoints, values))
    ]
    return observations, dataset_id, company_id


def _request(
    dataset_id: UUID,
    company_id: UUID,
    *,
    year: int = 2026,
    quarter: int = 4,
    size: int = 4,
    as_of: datetime | None = None,
) -> _WindowRequest:
    return {
        "provider_dataset_id": dataset_id,
        "company_id": company_id,
        "filing_scope": "standalone",
        "metric_code": "revenue",
        "fiscal_year": year,
        "fiscal_quarter": quarter,
        "window_size": size,
        "as_of": as_of or _at(2028, 1, 31),
    }


def test_growth_window_validates_size_endpoint_and_complete_history() -> None:
    observations, dataset_id, company_id = _unit_series()
    history = _history(observations)

    with pytest.raises(ValueError, match="at least 1"):
        history.growth_window_as_of(**_request(dataset_id, company_id, size=0))
    one = history.growth_window_as_of(
        **_request(dataset_id, company_id, year=2026, quarter=1, size=1)
    )
    four = history.growth_window_as_of(**_request(dataset_id, company_id))
    insufficient = history.growth_window_as_of(
        **_request(dataset_id, company_id, year=2026, quarter=3, size=4)
    )
    missing_endpoint = history.growth_window_as_of(
        **_request(dataset_id, company_id, year=2027, quarter=1, size=1)
    )

    assert one is not None and len(one.observations) == 1
    assert four is not None and len(four.observations) == 4
    assert insufficient is None and missing_endpoint is None


def test_growth_window_refuses_gaps_malformed_progression_and_absolute_change() -> None:
    observations, dataset_id, company_id = _unit_series()
    missing = [observations[0], observations[1], observations[3]]
    malformed = observations.copy()
    malformed[2] = _growth(
        2026,
        4,
        "0.3",
        provider_dataset_id=dataset_id,
        company_id=company_id,
        start=date(2026, 10, 1),
        end=date(2026, 12, 31),
    )
    absolute = observations.copy()
    absolute[1] = _growth(
        2026,
        2,
        None,
        provider_dataset_id=dataset_id,
        company_id=company_id,
        mode="absolute_change",
    )

    assert (
        _history(missing).growth_window_as_of(
            **_request(dataset_id, company_id, size=3)
        )
        is None
    )
    assert _history(malformed).growth_window_as_of(**_request(dataset_id, company_id)) is None
    assert _history(absolute).growth_window_as_of(**_request(dataset_id, company_id)) is None


def test_growth_window_orders_oldest_first_and_uses_full_availability() -> None:
    latest_prior = _at(2027, 7, 1)
    observations, dataset_id, company_id = _unit_series(
        available=(
            _at(2027, 1, 1),
            latest_prior,
            _at(2027, 3, 1),
            _at(2027, 4, 1),
        )
    )
    request = _request(dataset_id, company_id)
    window = _history(observations).growth_window_as_of(**request)
    consistency = _history(observations).growth_consistency_as_of(**request)
    persistence = _history(observations).growth_persistence_as_of(
        threshold=Decimal("0"), **request
    )

    assert window is not None
    assert [
        (value.ending_fiscal_year, value.ending_fiscal_quarter)
        for value in window.observations
    ] == [(2026, 1), (2026, 2), (2026, 3), (2026, 4)]
    assert window.available_at == latest_prior
    assert consistency is not None and consistency.available_at == latest_prior
    assert persistence is not None and persistence.available_at == latest_prior


@pytest.mark.parametrize(
    ("values", "positive_count", "share"),
    [
        (("0.1", "0.2", "0.3", "0.4"), 4, Decimal("1")),
        (("0.1", "0.2", "-0.05", "0.04"), 3, Decimal("0.75")),
        (("0.1", "0", "-0.05", "0.04"), 2, Decimal("0.5")),
        (("0", "-0.1", "-0.2", "0"), 0, Decimal("0")),
    ],
)
def test_growth_consistency_counts_strictly_positive_observations(
    values: tuple[str, str, str, str],
    positive_count: int,
    share: Decimal,
) -> None:
    observations, dataset_id, company_id = _unit_series(values)

    result = _history(observations).growth_consistency_as_of(
        **_request(dataset_id, company_id)
    )

    assert result is not None
    assert result.positive_count == positive_count
    assert result.non_positive_count == 4 - positive_count
    assert result.positive_share == share
    assert isinstance(result.positive_share, Decimal)


def test_growth_consistency_refuses_partial_and_absolute_change_windows() -> None:
    observations, dataset_id, company_id = _unit_series()
    absolute = observations.copy()
    absolute[2] = _growth(
        2026,
        3,
        None,
        provider_dataset_id=dataset_id,
        company_id=company_id,
        mode="absolute_change",
    )

    assert (
        _history(observations[:3]).growth_consistency_as_of(
            **_request(dataset_id, company_id)
        )
        is None
    )
    assert (
        _history(absolute).growth_consistency_as_of(**_request(dataset_id, company_id))
        is None
    )


@pytest.mark.parametrize(
    ("values", "threshold", "count", "saturated", "stop_value"),
    [
        (("0.2", "0.2", "0.2", "0.1"), Decimal("0.1"), 0, False, Decimal("0.1")),
        (("0.2", "0.2", "0.05", "0.2"), Decimal("0.1"), 1, False, Decimal("0.05")),
        (("0.05", "0.12", "0.15", "0.2"), Decimal("0.1"), 3, False, Decimal("0.05")),
        (("0.2", "0.2", "0.2", "0.2"), Decimal("0.1"), 4, True, None),
        (("-0.2", "-0.1", "0", "0.1"), Decimal("-0.15"), 3, False, Decimal("-0.2")),
    ],
)
def test_growth_persistence_uses_explicit_strict_threshold_and_lineage(
    values: tuple[str, str, str, str],
    threshold: Decimal,
    count: int,
    saturated: bool,
    stop_value: Decimal | None,
) -> None:
    observations, dataset_id, company_id = _unit_series(values)

    result = _history(observations).growth_persistence_as_of(
        threshold=threshold,
        **_request(dataset_id, company_id),
    )

    assert result is not None
    assert result.threshold == threshold and result.streak_count == count
    assert result.window_saturated is saturated
    assert len(result.streak_observations) == count
    assert (
        None if result.stopping_observation is None else result.stopping_observation.value
    ) == stop_value
    assert tuple(result.streak_observations) == tuple(observations[4 - count :])


def test_growth_persistence_refuses_partial_gap_and_absolute_change_windows() -> None:
    observations, dataset_id, company_id = _unit_series()
    absolute = observations.copy()
    absolute[1] = _growth(
        2026,
        2,
        None,
        provider_dataset_id=dataset_id,
        company_id=company_id,
        mode="absolute_change",
    )
    gap = [observations[0], observations[1], observations[3]]
    request = _request(dataset_id, company_id)

    assert (
        _history(observations[:3]).growth_persistence_as_of(
            threshold=Decimal("0"), **request
        )
        is None
    )
    assert (
        _history(gap).growth_persistence_as_of(threshold=Decimal("0"), **request)
        is None
    )
    assert (
        _history(absolute).growth_persistence_as_of(
            threshold=Decimal("0"), **request
        )
        is None
    )


def _real_history(
    session,
    tmp_path: Path,
    *financials: tuple[str, ProviderMetadata],
) -> GrowthHistoryFeatures:
    service = IngestionService(session, LocalRawObjectStore(tmp_path / "raw"))
    service.ingest_universe(
        CSVUniverseProvider(FIXTURES / "universe_synthetic.csv", UNIVERSE, RETRIEVED_AT)
    )
    for filename, metadata in financials:
        service.ingest_financials(
            CSVFinancialsProvider(FIXTURES / filename, metadata, RETRIEVED_AT)
        )
    reader = PointInTimeFinancialReader(session)
    financial_features = FinancialInflectionFeatures(FiscalQuarterNormalizer(reader))
    return GrowthHistoryFeatures(financial_features)


def _company_id(session) -> UUID:
    company_id = session.scalar(
        select(Company.id).where(Company.legal_name == "Aurora Fabrication Limited")
    )
    assert company_id is not None
    return company_id


def _dataset_id(session, provider_code: str) -> UUID:
    dataset_id = session.scalar(
        select(ProviderDataset.id)
        .join(DataProvider)
        .where(
            DataProvider.code == provider_code,
            ProviderDataset.code == "financials",
        )
    )
    assert dataset_id is not None
    return dataset_id


def _real_request(
    session,
    provider_code: str = "growth_history",
    filing_scope: str = "standalone",
    as_of: datetime = datetime(2027, 5, 31, 12, tzinfo=UTC),
) -> _WindowRequest:
    return {
        "provider_dataset_id": _dataset_id(session, provider_code),
        "company_id": _company_id(session),
        "filing_scope": filing_scope,
        "metric_code": "revenue",
        "fiscal_year": 2026,
        "fiscal_quarter": 4,
        "window_size": 4,
        "as_of": as_of,
    }


def test_growth_history_restatement_changes_aggregates_only_at_pit_boundary(
    session,
    tmp_path: Path,
) -> None:
    history = _real_history(
        session,
        tmp_path,
        ("financials_growth_history_synthetic.csv", HISTORY),
    )
    before_request = _real_request(session, as_of=_at(2027, 5, 31))
    restated_request = _real_request(session, as_of=_at(2027, 6, 1))
    before_consistency = history.growth_consistency_as_of(**before_request)
    before_persistence = history.growth_persistence_as_of(
        threshold=Decimal("0.15"), **before_request
    )
    restated_consistency = history.growth_consistency_as_of(**restated_request)
    restated_persistence = history.growth_persistence_as_of(
        threshold=Decimal("0.15"), **restated_request
    )
    historical_consistency = history.growth_consistency_as_of(**before_request)
    historical_persistence = history.growth_persistence_as_of(
        threshold=Decimal("0.15"), **before_request
    )

    assert before_consistency is not None and before_consistency.positive_share == Decimal("1")
    assert before_persistence is not None and before_persistence.streak_count == 3
    assert restated_consistency is not None
    assert restated_consistency.positive_count == 3
    assert restated_consistency.positive_share == Decimal("0.75")
    assert restated_persistence is not None and restated_persistence.streak_count == 2
    assert restated_consistency.available_at == _at(2027, 6, 1)
    assert historical_consistency is not None
    assert historical_consistency.positive_share == before_consistency.positive_share
    assert historical_persistence is not None
    assert historical_persistence.streak_count == before_persistence.streak_count
    assert before_consistency.window.observations[-1].current_quarter.lineage
    source = before_consistency.window.observations[-1].current_quarter.lineage[0].fact
    assert source.source_record.raw_object_key and source.source_record.raw_payload_reference


def test_growth_history_provider_datasets_and_filing_scopes_are_isolated(
    session,
    tmp_path: Path,
) -> None:
    history = _real_history(
        session,
        tmp_path,
        ("financials_growth_history_provider_a_synthetic.csv", PROVIDER_A),
        ("financials_growth_history_provider_b_synthetic.csv", PROVIDER_B),
        ("financials_growth_history_scope_synthetic.csv", SCOPE),
    )
    for provider_code in ("growth_history_a", "growth_history_b"):
        request = _real_request(session, provider_code=provider_code, as_of=_at(2027, 5, 2))
        assert history.growth_window_as_of(**request) is None
        assert history.growth_consistency_as_of(**request) is None
        assert (
            history.growth_persistence_as_of(threshold=Decimal("0.1"), **request)
            is None
        )
    for filing_scope in ("standalone", "consolidated"):
        request = _real_request(
            session,
            provider_code="growth_history_scope",
            filing_scope=filing_scope,
            as_of=_at(2027, 5, 2),
        )
        assert history.growth_window_as_of(**request) is None
        assert history.growth_consistency_as_of(**request) is None
        assert (
            history.growth_persistence_as_of(threshold=Decimal("0.1"), **request)
            is None
        )


def test_growth_history_normalizes_aware_offsets_and_rejects_naive_cutoffs() -> None:
    observations, dataset_id, company_id = _unit_series()
    history = _history(observations)
    ist = datetime(2028, 2, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    request = _request(dataset_id, company_id, as_of=ist)

    window = history.growth_window_as_of(**request)
    consistency = history.growth_consistency_as_of(**request)
    persistence = history.growth_persistence_as_of(
        threshold=Decimal("0"), **request
    )

    assert window is not None and window.as_of == _at(2028, 2, 1, 0)
    assert consistency is not None and consistency.as_of == window.as_of
    assert persistence is not None and persistence.as_of == window.as_of
    with pytest.raises(ValueError, match="timezone-aware"):
        history.growth_window_as_of(
            **_request(
                dataset_id,
                company_id,
                as_of=datetime(2028, 2, 1),
            )
        )
