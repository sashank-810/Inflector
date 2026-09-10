"""Focused Phase 3D semantic regression tests using PIT-quarterized inputs."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from inflector_data.financial_features import FinancialInflectionFeatures
from inflector_data.period_normalization import (
    FiscalQuarterNormalizer,
    QuarterizationLineage,
    QuarterizedFinancialValue,
)


def _at(day: int) -> datetime:
    return datetime(2027, 1, day, tzinfo=UTC)


def _q(
    year: int,
    quarter: int,
    value: str,
    metric: str = "pat",
    available: int = 1,
    start: date | None = None,
    end: date | None = None,
) -> QuarterizedFinancialValue:
    starts = {
        (2025, 1): (date(2025, 4, 1), date(2025, 6, 30)),
        (2025, 2): (date(2025, 7, 1), date(2025, 9, 30)),
        (2025, 3): (date(2025, 10, 1), date(2025, 12, 31)),
        (2025, 4): (date(2026, 1, 1), date(2026, 3, 31)),
        (2026, 1): (date(2026, 4, 1), date(2026, 6, 30)),
        (2026, 2): (date(2026, 7, 1), date(2026, 9, 30)),
        (2026, 3): (date(2026, 10, 1), date(2026, 12, 31)),
        (2026, 4): (date(2027, 1, 1), date(2027, 3, 31)),
    }
    if start is None or end is None:
        start, end = starts.get((year, quarter), (date(2025, 4, 1), date(2025, 6, 30)))
    fact = SimpleNamespace(
        metric=SimpleNamespace(semantic_type="duration", unit_category="monetary")
    )
    return QuarterizedFinancialValue(
        uuid4(),
        uuid4(),
        "standalone",
        metric,
        year,
        quarter,
        start,
        end,
        Decimal(value),
        "INR",
        "reported",
        "reported",
        "v",
        _at(30),
        _at(available),
        (cast(QuarterizationLineage, SimpleNamespace(fact=fact)),),
    )


def _features(series):
    feature = FinancialInflectionFeatures(cast(FiscalQuarterNormalizer, SimpleNamespace()))
    feature._series = lambda *args, **kwargs: series  # type: ignore[method-assign]
    return feature


def _growth(series, kind="qoq"):
    f = _features(series)
    q = series[-1]
    return f.quarter_growth_as_of(
        provider_dataset_id=q.provider_dataset_id,
        company_id=q.company_id,
        filing_scope="standalone",
        metric_code=q.metric_code,
        fiscal_year=q.fiscal_year,
        fiscal_quarter=q.fiscal_quarter,
        comparison_kind=kind,
        as_of=_at(30),
    )


def test_profit_transition_matrix_is_loss_aware() -> None:
    cases = [
        ("100", "125", "percentage_change", Decimal("0.25"), None),
        ("100", "-50", "percentage_change", Decimal("-1.5"), "profit_to_loss"),
        ("-100", "50", "absolute_change", None, "loss_to_profit"),
        ("-100", "-50", "absolute_change", None, "loss_narrowing"),
        ("-50", "-100", "absolute_change", None, "loss_widening"),
        ("-50", "-50", "absolute_change", None, "loss_unchanged"),
        ("0", "5", "absolute_change", None, "zero_base_positive"),
        ("0", "-5", "absolute_change", None, "zero_base_negative"),
        ("0", "0", "absolute_change", None, "zero_base_zero"),
    ]
    for base, current, mode, value, transition in cases:
        result = _growth([_q(2025, 4, base), _q(2026, 1, current)])
        assert (
            result
            and result.calculation_mode == mode
            and result.value == value
            and result.transition == transition
        )
        if Decimal(base) <= 0:
            assert result.absolute_change == Decimal(current) - Decimal(base)
            assert "non_positive_comparison_base" in result.warnings


def test_growth_continuity_eligibility_and_full_path_availability() -> None:
    series = [
        _q(2025, 4, "100", available=1),
        _q(2026, 1, "110", available=1),
        _q(2026, 2, "120", available=9),
        _q(2026, 3, "130", available=1),
        _q(2026, 4, "140", available=1),
    ]
    result = _growth(series, "yoy")
    assert result and result.value == Decimal("0.4") and result.available_at == _at(9)
    gap = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120", start=date(2026, 7, 2), end=date(2026, 9, 30)),
    ]
    assert _growth(gap) is None
    eps = [_q(2025, 4, "1", "eps_basic"), _q(2026, 1, "2", "eps_basic")]
    instant = [_q(2025, 4, "1", "total_assets"), _q(2026, 1, "2", "total_assets")]
    for values in (eps, instant):
        for q in values:
            object.__setattr__(
                q.lineage[0].fact.metric,
                "semantic_type",
                ("instant" if q.metric_code == "total_assets" else "duration"),
            )
            object.__setattr__(
                q.lineage[0].fact.metric,
                "unit_category",
                ("per_share" if q.metric_code == "eps_basic" else "monetary"),
            )
        assert _growth(values) is None


def test_acceleration_uses_median_not_mean_and_retains_lineage() -> None:
    series = [
        _q(2025, 1, "100"),
        _q(2025, 2, "100"),
        _q(2025, 3, "100"),
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120"),
        _q(2026, 3, "190"),
        _q(2026, 4, "150", available=8),
    ]
    f = _features(series)
    result = f.growth_acceleration_as_of(
        provider_dataset_id=series[0].provider_dataset_id,
        company_id=series[0].company_id,
        filing_scope="standalone",
        metric_code="pat",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(30),
    )
    assert (
        result and result.prior_median == Decimal("0.2") and result.acceleration == Decimal("0.3")
    )
    assert (
        len(result.prior_yoys) == 3
        and result.current_yoy.value == Decimal("0.5")
        and result.available_at == _at(8)
    )


def test_acceleration_refuses_insufficient_and_absolute_change_observations() -> None:
    insufficient = [
        _q(2025, 1, "100"),
        _q(2025, 2, "100"),
        _q(2025, 3, "100"),
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120"),
        _q(2026, 3, "130"),
    ]
    feature = _features(insufficient)
    assert (
        feature.growth_acceleration_as_of(
            provider_dataset_id=insufficient[0].provider_dataset_id,
            company_id=insufficient[0].company_id,
            filing_scope="standalone",
            metric_code="pat",
            fiscal_year=2026,
            fiscal_quarter=3,
            as_of=_at(30),
        )
        is None
    )


def test_qoq_and_yoy_continuity_matrix_and_series_ordering() -> None:
    first = [_q(2025, 4, "100")]
    valid = first + [_q(2026, 1, "110")]
    qoq = _growth(valid)
    assert _growth(first) is None and qoq is not None and qoq.value == Decimal("0.1")
    assert (
        _growth(
            [_q(2025, 4, "100"), _q(2026, 1, "110", start=date(2026, 4, 2), end=date(2026, 6, 30))]
        )
        is None
    )
    five = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120"),
        _q(2026, 3, "130"),
        _q(2026, 4, "140"),
    ]
    f = _features(five)
    assert (
        f.growth_series_as_of(
            provider_dataset_id=five[0].provider_dataset_id,
            company_id=five[0].company_id,
            filing_scope="standalone",
            metric_code="pat",
            comparison_kind="qoq",
            as_of=_at(30),
        )[-1].ending_fiscal_quarter
        == 4
    )
    yoy = _growth(five, "yoy")
    assert yoy is not None and yoy.value == Decimal("0.4")


def test_public_margin_refuses_bad_revenue_and_preserves_negative_numerator() -> None:
    revenue = _q(2026, 1, "100", "revenue")
    profit = _q(2026, 1, "-10", "operating_profit")

    class Lookup:
        def quarter_as_of(self, **kwargs):
            return {"revenue": revenue, "operating_profit": profit}.get(kwargs["metric_code"])

        def quarter_series_as_of(self, **kwargs):
            return [profit] if kwargs["metric_code"] == "operating_profit" else [revenue]

    f = FinancialInflectionFeatures(cast(FiscalQuarterNormalizer, Lookup()))
    result = f.quarter_margin_as_of(
        provider_dataset_id=revenue.provider_dataset_id,
        company_id=revenue.company_id,
        filing_scope="standalone",
        margin_code="operating_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(30),
    )
    assert result and result.value == Decimal("-0.1")


def test_public_margin_refuses_zero_negative_and_period_mismatched_revenue() -> None:
    profit = _q(2026, 1, "10", "operating_profit")
    for revenue in (
        _q(2026, 1, "0", "revenue"),
        _q(2026, 1, "-100", "revenue"),
        _q(2026, 1, "100", "revenue", start=date(2026, 4, 2), end=date(2026, 6, 30)),
    ):

        class Lookup:
            def quarter_as_of(self, **kwargs):
                return profit if kwargs["metric_code"] == "operating_profit" else revenue

            def quarter_series_as_of(self, **kwargs):
                return [profit] if kwargs["metric_code"] == "operating_profit" else [revenue]

        feature = FinancialInflectionFeatures(cast(FiscalQuarterNormalizer, Lookup()))
        assert (
            feature.quarter_margin_as_of(
                provider_dataset_id=profit.provider_dataset_id,
                company_id=profit.company_id,
                filing_scope="standalone",
                margin_code="operating_margin",
                fiscal_year=2026,
                fiscal_quarter=1,
                as_of=_at(30),
            )
            is None
        )


def test_qoq_refuses_overlap_and_malformed_quarter_progression() -> None:
    overlap = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110", start=date(2026, 3, 31), end=date(2026, 6, 30)),
    ]
    malformed = [
        _q(2026, 1, "100"),
        _q(2026, 3, "110", start=date(2026, 7, 1), end=date(2026, 9, 30)),
    ]
    assert _growth(overlap) is None
    assert _growth(malformed) is None


def test_yoy_refuses_missing_gap_overlap_and_malformed_paths() -> None:
    missing = [_q(2025, 4, "100"), _q(2026, 1, "110"), _q(2026, 3, "130"), _q(2026, 4, "140")]
    gap = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120", start=date(2026, 7, 2), end=date(2026, 9, 30)),
        _q(2026, 3, "130"),
        _q(2026, 4, "140"),
    ]
    overlap = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120", start=date(2026, 6, 30), end=date(2026, 9, 30)),
        _q(2026, 3, "130"),
        _q(2026, 4, "140"),
    ]
    malformed = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 3, "120", start=date(2026, 7, 1), end=date(2026, 9, 30)),
        _q(2026, 4, "130"),
        _q(2027, 1, "140"),
    ]
    assert all(_growth(values, "yoy") is None for values in (missing, gap, overlap, malformed))
    absolute = [
        _q(2025, 1, "-100"),
        _q(2025, 2, "100"),
        _q(2025, 3, "100"),
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120"),
        _q(2026, 3, "130"),
        _q(2026, 4, "140"),
    ]
    feature = _features(absolute)
    assert (
        feature.growth_acceleration_as_of(
            provider_dataset_id=absolute[0].provider_dataset_id,
            company_id=absolute[0].company_id,
            filing_scope="standalone",
            metric_code="pat",
            fiscal_year=2026,
            fiscal_quarter=4,
            as_of=_at(30),
        )
        is None
    )


class _MarginLookup:
    """PIT-aware quarter lookup used only for focused margin tests."""

    def __init__(self, quarters):
        self._quarters = quarters

    def quarter_as_of(self, **kwargs):
        matches = [
            quarter
            for quarter in self._quarters
            if quarter.metric_code == kwargs["metric_code"]
            and quarter.fiscal_year == kwargs["fiscal_year"]
            and quarter.fiscal_quarter == kwargs["fiscal_quarter"]
            and quarter.available_at <= kwargs["as_of"]
        ]
        return matches[-1] if matches else None

    def quarter_series_as_of(self, **kwargs):
        return sorted(
            [
                quarter
                for quarter in self._quarters
                if quarter.metric_code == kwargs["metric_code"]
                and quarter.available_at <= kwargs["as_of"]
            ],
            key=lambda quarter: (
                quarter.period_end,
                quarter.fiscal_year,
                quarter.fiscal_quarter,
            ),
        )


def test_acceleration_refuses_nonconsecutive_yoy_endpoints() -> None:
    endpoints = [
        _q(2025, 4, "100"),
        _q(2026, 1, "110"),
        _q(2026, 2, "120"),
        _q(2026, 4, "140"),
    ]
    growth_values = [
        SimpleNamespace(
            ending_fiscal_year=quarter.fiscal_year,
            ending_fiscal_quarter=quarter.fiscal_quarter,
            calculation_mode="percentage_change",
            value=Decimal("0.1"),
            current_quarter=quarter,
            available_at=_at(1),
        )
        for quarter in endpoints
    ]

    feature = FinancialInflectionFeatures(
        cast(FiscalQuarterNormalizer, SimpleNamespace())
    )
    feature.growth_series_as_of = (  # type: ignore[method-assign]
        lambda **kwargs: growth_values
    )

    result = feature.growth_acceleration_as_of(
        provider_dataset_id=endpoints[-1].provider_dataset_id,
        company_id=endpoints[-1].company_id,
        filing_scope="standalone",
        metric_code="pat",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(30),
    )

    assert result is None


def test_acceleration_availability_includes_prior_yoys() -> None:
    series = [
        _q(2025, 1, "100", available=1),
        _q(2025, 2, "100", available=9),
        _q(2025, 3, "100", available=1),
        _q(2025, 4, "100", available=1),
        _q(2026, 1, "110", available=1),
        _q(2026, 2, "120", available=1),
        _q(2026, 3, "130", available=1),
        _q(2026, 4, "140", available=1),
    ]
    feature = _features(series)

    result = feature.growth_acceleration_as_of(
        provider_dataset_id=series[-1].provider_dataset_id,
        company_id=series[-1].company_id,
        filing_scope="standalone",
        metric_code="pat",
        fiscal_year=2026,
        fiscal_quarter=4,
        as_of=_at(30),
    )

    assert result is not None
    assert len(result.prior_yoys) == 3
    assert result.current_yoy.available_at == _at(1)
    assert max(value.available_at for value in result.prior_yoys) == _at(9)
    assert result.available_at == max(
        result.current_yoy.available_at,
        *(value.available_at for value in result.prior_yoys),
    )
    assert result.available_at == _at(9)


def test_margin_expansion_supports_contraction_and_explicit_identity() -> None:
    positive_quarters = [
        _q(2025, 1, "10", "operating_profit"),
        _q(2025, 2, "11", "operating_profit"),
        _q(2025, 3, "12", "operating_profit"),
        _q(2025, 4, "13", "operating_profit"),
        _q(2026, 1, "24", "operating_profit"),
        _q(2025, 1, "100", "revenue"),
        _q(2026, 1, "120", "revenue"),
    ]
    provider_id = positive_quarters[0].provider_dataset_id
    company_id = positive_quarters[0].company_id

    positive_feature = FinancialInflectionFeatures(
        cast(FiscalQuarterNormalizer, _MarginLookup(positive_quarters))
    )
    positive = positive_feature.margin_expansion_as_of(
        provider_dataset_id=provider_id,
        company_id=company_id,
        filing_scope="standalone",
        margin_code="operating_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(30),
    )

    assert positive is not None
    assert positive.change == Decimal("0.1")
    assert positive.basis_points == Decimal("1000")
    assert positive.basis_points == positive.change * Decimal("10000")
    assert positive.provider_dataset_id == provider_id
    assert positive.company_id == company_id
    assert positive.filing_scope == "standalone"

    contraction_quarters = [
        _q(2025, 1, "10", "operating_profit"),
        _q(2025, 2, "11", "operating_profit"),
        _q(2025, 3, "12", "operating_profit"),
        _q(2025, 4, "13", "operating_profit"),
        _q(2026, 1, "6", "operating_profit"),
        _q(2025, 1, "100", "revenue"),
        _q(2026, 1, "120", "revenue"),
    ]
    contraction_feature = FinancialInflectionFeatures(
        cast(FiscalQuarterNormalizer, _MarginLookup(contraction_quarters))
    )
    contraction = contraction_feature.margin_expansion_as_of(
        provider_dataset_id=provider_id,
        company_id=company_id,
        filing_scope="standalone",
        margin_code="operating_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(30),
    )

    assert contraction is not None
    assert contraction.change == Decimal("-0.05")
    assert contraction.basis_points == Decimal("-500")
    assert contraction.basis_points == contraction.change * Decimal("10000")


def test_margin_expansion_waits_for_full_continuity_evidence() -> None:
    quarters = [
        _q(2025, 1, "10", "operating_profit", available=1),
        _q(2025, 2, "11", "operating_profit", available=1),
        _q(2025, 3, "12", "operating_profit", available=9),
        _q(2025, 4, "13", "operating_profit", available=1),
        _q(2026, 1, "24", "operating_profit", available=1),
        _q(2025, 1, "100", "revenue", available=1),
        _q(2026, 1, "120", "revenue", available=1),
    ]
    provider_id = quarters[0].provider_dataset_id
    company_id = quarters[0].company_id
    feature = FinancialInflectionFeatures(
        cast(FiscalQuarterNormalizer, _MarginLookup(quarters))
    )

    before = feature.margin_expansion_as_of(
        provider_dataset_id=provider_id,
        company_id=company_id,
        filing_scope="standalone",
        margin_code="operating_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=datetime(2027, 1, 8, 23, 59, 59, tzinfo=UTC),
    )
    at = feature.margin_expansion_as_of(
        provider_dataset_id=provider_id,
        company_id=company_id,
        filing_scope="standalone",
        margin_code="operating_margin",
        fiscal_year=2026,
        fiscal_quarter=1,
        as_of=_at(9),
    )

    assert before is None
    assert at is not None
    assert at.available_at == _at(9)


def test_growth_series_does_not_bridge_broken_quarter_history() -> None:
    series = [
        _q(2025, 1, "100"),
        _q(2025, 2, "105"),
        _q(2025, 3, "110"),
        # FY2025 Q4 deliberately missing.
        _q(2026, 1, "120"),
        _q(2026, 2, "125"),
        _q(2026, 3, "130"),
        _q(2026, 4, "135"),
        _q(
            2027,
            1,
            "140",
            start=date(2027, 4, 1),
            end=date(2027, 6, 30),
        ),
    ]
    feature = _features(series)
    arguments = {
        "provider_dataset_id": series[0].provider_dataset_id,
        "company_id": series[0].company_id,
        "filing_scope": "standalone",
        "metric_code": "pat",
        "as_of": _at(30),
    }

    qoq = feature.growth_series_as_of(
        comparison_kind="qoq",
        **arguments,
    )
    yoy = feature.growth_series_as_of(
        comparison_kind="yoy",
        **arguments,
    )

    assert [
        (value.ending_fiscal_year, value.ending_fiscal_quarter)
        for value in qoq
    ] == [
        (2025, 2),
        (2025, 3),
        (2026, 2),
        (2026, 3),
        (2026, 4),
        (2027, 1),
    ]

    assert [
        (value.ending_fiscal_year, value.ending_fiscal_quarter)
        for value in yoy
    ] == [
        (2027, 1),
    ]
