# Point-in-time growth history, consistency, and persistence

## Scope

Phase 3F adds three ephemeral deterministic primitives over the public Phase 3D
YoY growth series:

- a complete comparable growth window;
- the positive-growth share within that window; and
- the current consecutive streak above an explicit threshold.

`GrowthHistoryFeatures` receives an explicit `FinancialInflectionFeatures`.
It does not instantiate quarter readers, query facts directly, reinterpret
quarter arithmetic, persist results, or apply scores. Algorithm versions are
`growth_window_v1`, `growth_consistency_v1`, and `growth_persistence_v1`.

## Comparable growth window

Every request specifies provider dataset, company, filing scope, metric,
ending fiscal year and quarter, window size, and `as_of`. The implementation
calls `FinancialInflectionFeatures.growth_series_as_of(...)` with
`comparison_kind="yoy"` and selects exactly the requested number of
observations ending at the requested endpoint.

Every member must use `calculation_mode="percentage_change"` and contain a
Decimal percentage value. Absolute-change observations remain valid Phase 3D
loss-transition evidence, but they are not numerically comparable percentage
YoYs and invalidate a required Phase 3F window.

The endpoint quarters must be consecutive in both stored dates and fiscal
labels. Each next quarter must begin the day after its predecessor ends, and
labels must advance Q1 through Q4 and then to the next fiscal year's Q1. A
missing endpoint, missing internal observation, gap, overlap, malformed label
progression, or absolute-change member returns `None`. Observations are retained
oldest to newest with their complete nested quarter, filing, source-record,
raw-object, and payload-locator lineage.

Partial windows are never returned. A configured four-observation request with
only three comparable YoYs is unavailable rather than being silently evaluated
with a smaller denominator.

## Growth consistency

For a complete window:

```text
positive = observation.value > 0
positive_share = positive_count / window_size
non_positive_count = window_size - positive_count
```

Zero is non-positive. Arithmetic uses Decimal exactly. This is an unweighted
raw proportion: there is no confidence adjustment, data-quality weighting,
magnitude weighting, winsorisation, or scoring. Those policies belong to a
future Phase 4 configuration.

## Growth persistence

Persistence receives an explicit Decimal `threshold`. Beginning with the
newest observation and walking backward inside the complete fixed window, it
counts consecutive observations for which:

```text
observation.value > threshold
```

The comparison is strictly greater than: equality terminates the streak. The
calculation never skips a failure. `streak_observations` retain the passing
observations in chronological order, and `stopping_observation` is the first
immediately preceding value at or below the threshold. If the current value
fails, the streak is empty and the current value is the stopping observation.

If every member passes, `streak_count == window_size`,
`window_saturated=True`, and there is no stopping observation. Saturation means
the streak is at least as long as the requested lookback; it does not claim the
economic streak began at the oldest member. Phase 3F never searches beyond the
configured window. Phase 4 will choose the lookback, threshold, and any score
cap.

## Anti-bridging and point-in-time behavior

A missing or invalid observation is not removed so surrounding values can be
joined. For example, endpoints Q1, Q2, Q4, and next-year Q1 are not a valid
four-observation window. An absolute-change observation inside a requested
window likewise makes the aggregate unavailable.

Only Phase 3D observations visible at the explicit cutoff participate.
Restatements change a window and its aggregates only when their source facts
become PIT-visible. Re-querying an earlier cutoff reproduces the original
window. The window's `available_at` is the maximum availability of all its
observations; consistency and persistence inherit that timestamp because the
complete configured history is required.

Aware `as_of` values are normalized to UTC, and naive timestamps are rejected.
Provider dataset and filing scope remain explicit throughout. There is no
provider reconciliation, provider fallback, consolidated preference, or scope
fallback.
