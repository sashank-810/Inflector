# Point-in-time leverage and capital-efficiency primitives

## Scope

Phase 3E-B adds five ephemeral deterministic calculations:

- net debt;
- debt/equity;
- TTM interest coverage;
- return on equity (ROE); and
- return on capital employed (ROCE).

`CapitalEfficiencyFeatures` receives an explicit
`InstantFinancialSnapshotReader` and `TrailingTwelveMonthNormalizer`. It does
not instantiate hidden readers, persist features, reconcile providers or
filing scopes, apply thresholds, or produce scores.

Algorithm versions are `net_debt_v1`, `debt_equity_v1`,
`interest_coverage_v1`, `roe_v1`, and `roce_v1`.

## Exact period-end snapshots

ROE and ROCE use
`InstantFinancialSnapshotReader.common_snapshot_for_period_end_as_of(...)`.
Every requested metric must be PIT-visible, eligible instant monetary INR, and
share one exact `FiscalPeriod.id` whose `period_end` equals the requested date.

There is no nearest-date substitution. If a boundary is 31 March, a 28
February or 30 June snapshot is not usable. Multiple complete period IDs with
the same requested end date are ambiguous and return `None`; quarter, annual,
availability, revision, and ingestion order do not break the tie.

## Net debt and debt/equity

Net debt uses the latest complete common debt-and-cash snapshot:

```text
net debt = total debt - cash and cash equivalents
```

Negative net debt is retained as net cash. It is never clamped to zero.

Debt/equity uses a latest complete common debt-and-equity snapshot:

```text
debt/equity = total debt / total equity
```

The ratio is defined only for positive equity. Zero or negative equity returns
a result with no ratio and warning `non_positive_equity`. The implementation
does not use absolute equity or a near-zero threshold.

## Interest coverage

Both inputs are independent Phase 3C TTMs ending at the requested fiscal year
and quarter. Provider, company, filing scope, endpoint, period boundaries, and
INR unit must match exactly.

```text
interest coverage = TTM EBIT / TTM finance cost
```

Negative EBIT with positive finance cost produces a valid negative ratio. A
zero or negative finance cost returns no ratio and warning
`non_positive_finance_cost`. The sign is not repaired with `abs()` because the
metric dictionary does not yet encode provider sign conventions.

## ROE

For a TTM PAT window, the exact balance-sheet dates are:

```text
beginning date = TTM period_start - 1 day
ending date    = TTM period_end
average equity = (beginning equity + ending equity) / 2
ROE            = TTM PAT / average equity
```

Both exact equity snapshots are required. Beginning and ending equity must be
positive; otherwise the result has no ratio and warning
`non_positive_equity`. Negative PAT with positive equity produces a valid
negative ROE.

## ROCE

ROCE uses TTM EBIT and exact beginning/ending snapshots containing equity,
debt, and cash:

```text
capital employed = total equity + total debt - cash and cash equivalents
average capital employed = (beginning capital employed + ending capital employed) / 2
ROCE = TTM EBIT / average capital employed
```

Both boundary capital-employed values must be positive. A non-positive value
returns no ratio and warning `non_positive_capital_employed`. Negative EBIT
with positive capital employed produces a valid negative ROCE. Phase 3E-B does
not introduce NOPAT, effective tax rates, or ROIC.

## Point-in-time behavior and lineage

Every method requires an explicit provider dataset, company, filing scope, and
timezone-aware `as_of`, which is normalized to UTC. There is no provider or
standalone/consolidated fallback.

Restatements affect results only when `available_at <= as_of`. Re-querying an
earlier cutoff retains the original immutable inputs. Feature availability is
the maximum availability of every required input:

- snapshot availability for net debt and debt/equity;
- both TTM availabilities for interest coverage; and
- TTM plus beginning and ending snapshot availability for ROE and ROCE.

Results retain the complete snapshot and/or TTM objects. Their nested facts,
quarters, filings, source records, raw object keys, and payload locators remain
available for explanation and reproduction.
