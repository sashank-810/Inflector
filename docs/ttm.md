# Point-in-time trailing-twelve-month construction

## Scope

Phase 3C is a read-only aggregation layer. It answers, for one explicit
provider dataset, company, filing scope, metric, endpoint quarter, and
timezone-aware `as_of`, which trailing-four-quarter value was knowable then.
It does not persist a TTM fact or fiscal period, calculate growth or margins,
or read raw annual/YTD facts directly.

## One construction path

`TrailingTwelveMonthNormalizer` consumes only
`FiscalQuarterNormalizer.quarter_series_as_of(...)`. Version `ttm_v1` has one
rule:

```text
TTM(Q_t) = Q_(t-3) + Q_(t-2) + Q_(t-1) + Q_t
```

Each input can be a reported quarter or a Phase 3B YTD difference. There is no
annual-plus-YTD, annual-only, or other fallback algebra. A missing, gapped,
overlapping, or incorrectly labelled quarter means no TTM for that endpoint.
This single path keeps the calculation and its provenance auditable.

## Eligibility and continuity

Every selected quarter must be Decimal normalized `INR`, and every nested
financial metric definition must prove `semantic_type = duration` and
`unit_category = monetary`. EPS/per-share facts, ratios, percentages, shares,
and instant balance-sheet values never form a TTM.

The four quarters must have one provider dataset, company, scope, and metric.
They must be distinct and ordered by stored `period_end`. Adjacent periods must
be exactly date-contiguous (`next.period_start = previous.period_end + 1 day`)
and labels must progress Q1→Q2→Q3→Q4→next fiscal-year Q1. No calendar-quarter
convention is inferred. Consequently, a valid window may cross the fiscal-year
boundary; TTM does not reset at Q4.

## Point-in-time behavior

The normalizer receives only quarters selected at `available_at <= as_of`.
TTM availability is the maximum availability of its four components, and the
boundary is inclusive. A late restatement changes the affected quarter and TTM
only from that restatement's availability forward. Likewise, a later direct
quarter replaces a formerly valid Phase 3B derived quarter only once the direct
fact becomes PIT-visible; earlier cutoffs retain the derivation.

Provider datasets and filing scopes are explicit and never reconciled or mixed.
Negative quarters and negative totals remain valid exact arithmetic; they are
not quality judgements.

## Result and lineage

`ttm_as_of(...)` requires an explicit endpoint fiscal year and quarter.
`ttm_series_as_of(...)` emits every valid window in ascending endpoint period
order, without zero filling or interpolation.

Each `TrailingTwelveMonthValue` records `construction_kind = four_quarters`,
`operation = sum_four_quarters`, `algorithm_version = ttm_v1`, its endpoint,
window boundaries, Decimal value/unit, PIT availability, and four structured
roles: `quarter_t_minus_3`, `quarter_t_minus_2`, `quarter_t_minus_1`, and
`quarter_t`. Each component retains the complete `QuarterizedFinancialValue`,
including its reported/derived operation and the underlying selected filing,
financial fact, source record, raw object, and raw payload locator.
