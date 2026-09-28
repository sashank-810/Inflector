# Market Structure deterministic features

Phase 3G-C composes approved point-in-time market evidence into immutable,
in-memory feature values. It does not score Market Structure, apply the reserved
top-level weight, persist features, or imply a recommendation.

## Explicit context and time

Every bundle declares three independent provider datasets: one for security
market bars, one for corporate actions, and one for benchmark bars. It also
declares a benchmark code, security, interval, and timezone-aware knowledge
cutoff. No provider fallback, reconciliation, or benchmark auto-selection is
performed. The same benchmark code under two providers remains two distinct
evidence identities.

`as_of` is the knowledge boundary and is normalized to UTC. The optional
`market_on_or_before` is an economic-date bound. The bundle basis date is the
latest selected security trading date, never `as_of.date()`. Mutable security,
listing, and benchmark statuses are not historical gates.

Windows count selected observations rather than calendar days. No weekends,
holidays, or missing dates are manufactured:

- a 20-price window uses 20 adjusted bars;
- a 60-price window uses 60 adjusted bars;
- 20 returns require 21 adjusted bars;
- 60 returns require 61 adjusted bars;
- 60-observation relative performance uses 61 security endpoints.

Each feature is independently available. Insufficient history or missing
evidence for one feature does not suppress unrelated features. For unavailable
features, `available_at` is the latest availability among relevant partial
evidence when any exists; otherwise it is null.

## Price structure and relative strength

Price-comparability features consume the Phase 3G-B split/bonus-adjusted series.
They therefore inherit PIT price revisions, corporate-action revision collapse,
the historical-subset basis rule, and recursive source lineage.

`relative_strength_60_to_benchmark` uses the first and last security bars in
the final 61-bar window. Benchmark bars must exist on those exact two economic
dates; nearby dates are never substituted. With adjusted security closes
`S0`, `S1` and raw PIT benchmark closes `B0`, `B1`:

```text
security_growth  = S1 / S0
benchmark_growth = B1 / B0
relative_strength = security_growth / benchmark_growth - 1
```

This is multiplicative relative price performance, not return subtraction,
alpha, or sector-relative strength. Sector benchmark mapping remains deferred;
a caller may select a sector index explicitly, but this layer does not choose
one.

`close_to_sma20` is latest adjusted close divided by the arithmetic mean of the
last 20 adjusted closes, minus one. `sma20_to_sma60` is the latest 20-close mean
divided by the latest 60-close mean, minus one. No EMA is calculated.

`consolidation_range_20` is:

```text
(maximum adjusted high - minimum adjusted low)
/ mean adjusted close
```

It is a range primitive, not ATR, a breakout signal, or a trading label.

## Returns and volatility

Volatility reuses the unchanged Phase 3G-B adjacent simple-return primitive.
It never skips an invalid return. One undefined return makes its containing
window unavailable.

For 20 or 60 simple returns, the feature is the population standard deviation:

```text
mean = sum(return) / N
variance = sum((return - mean) ** 2) / N
volatility = sqrt(variance)
```

It is not annualized. All operations use `Decimal`; square root runs under a
local fixed precision of 50 so ambient process context cannot change results.
`volatility_ratio_20_to_60` divides the 20-return value by the 60-return value.
A zero medium-window volatility leaves that ratio undefined.

## Raw activity and delivery

Activity deliberately uses each selected raw atomic market row:

```text
close_times_volume_inr = raw close_price * Decimal(raw volume)
```

`average_close_times_volume_20_inr` averages this proxy over 20 observations.
`close_times_volume_ratio_20_to_60` divides the latest 20-observation mean by
the 60-observation mean. The unit is `INR_proxy`: provider-supplied official
turnover is not claimed.

Adjusted close multiplied by raw volume is forbidden. Historical share volume
has not been adjusted for capital actions, so that mixture would distort the
pre-action side. Raw close times raw volume is mechanically stable in the
simple 2:1 example `200 × 100 = 100 × 200 = 20,000`. Raw share-volume momentum
and delivery-quantity trends remain deferred pending reviewed share-basis
semantics.

`average_delivery_percentage_20` requires a delivery fraction on every one of
the latest 20 raw bars and computes their arithmetic mean. It never infers a
percentage from quantity or volume, forward-fills, or averages only the
non-null subset.

Raw activity and delivery availability depends only on raw market-bar
availability. A corporate-action correction may update adjusted-price features
without making those raw features newly available.

## Corrections, lineage, and boundaries

Market corrections become visible at their market `available_at` boundary and
can affect adjusted-price and raw-row features that consume the corrected row.
Action corrections affect adjusted-price features only. Benchmark corrections
affect relative strength only. Historical cutoffs remain reproducible, and all
feature evidence recursively reaches the relevant market, corporate-action,
and benchmark `SourceRecord` views.

Phase 3G-C does not implement RSI, MACD, breakout classification, sector
benchmark inference, Low Market Attention, eligibility wiring, valuation,
scoring curves, a 0–100 score, persistence, or score-snapshot orchestration.
The close-times-volume proxy is not silently substituted for the eligibility
policy's official average-daily-traded-value input. Calibration and policy
interpretation belong to the separately reviewed Phase 4D-F scorer.
Phase 4D-F now consumes this bundle without recomputing it; see
[`market-structure-scoring.md`](market-structure-scoring.md).
