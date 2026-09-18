# Point-in-time cash-flow quality and working-capital primitives

## Scope

Phase 3E-C adds four ephemeral deterministic calculations:

- TTM CFO / TTM PAT conversion;
- TTM CFO / TTM reported EBITDA conversion;
- receivable days; and
- trade working-capital change.

`CashFlowQualityFeatures` receives an explicit
`InstantFinancialSnapshotReader` and `TrailingTwelveMonthNormalizer`. It does
not instantiate hidden readers, persist features, reconcile providers or
filing scopes, apply scoring thresholds, or produce scores.

The algorithm versions are `cfo_conversion_v1`, `cfo_ebitda_v1`,
`receivable_days_v1`, and `trade_working_capital_change_v1`.

## TTM input contract

Two-input TTM calculations require both inputs to match exactly on provider
dataset, company, filing scope, ending fiscal year and quarter, period start,
period end, and normalized INR unit. A missing or mismatched input returns
`None`; the feature layer never repairs or joins incompatible windows.

All arithmetic uses `Decimal`. Negative numerators are preserved rather than
converted to positive amounts.

## CFO conversion

```text
cash_minus_pat = TTM CFO - TTM PAT
CFO conversion = TTM CFO / TTM PAT
```

The ratio is defined only when PAT is positive. For zero or negative PAT, the
result retains CFO, PAT, and `cash_minus_pat`, but its ratio is `None` and its
warning is `non_positive_pat`. Negative CFO with positive PAT is a valid
negative conversion ratio. PAT is never replaced with its absolute value.

## CFO to EBITDA

```text
CFO / EBITDA = TTM CFO / TTM reported EBITDA
```

The ratio is defined only when reported EBITDA is positive. Zero or negative
EBITDA produces no ratio and warning `non_positive_ebitda`. Negative CFO with
positive EBITDA produces a valid negative ratio. No absolute-value or
near-zero policy is applied.

## Receivable days

For a TTM revenue window, exact balance-sheet boundaries are required:

```text
beginning date       = TTM period_start - 1 day
ending date          = TTM period_end
average receivables  = (beginning receivables + ending receivables) / 2
receivable days      = average receivables / TTM revenue * 365
```

Both snapshots must be complete, unambiguous, PIT-visible snapshots for the
exact requested date. A nearby date is never substituted. Zero receivables are
valid and produce zero days. Non-positive revenue produces no value and warning
`non_positive_revenue`; a negative boundary receivable produces no value and
warning `negative_trade_receivables`. Values are not repaired with `abs()`.

## Trade working-capital change

TTM revenue supplies only the economic window. It is not part of the
working-capital arithmetic. At both exact TTM boundaries:

```text
trade working capital = trade receivables + inventory - trade payables
change = ending trade working capital - beginning trade working capital
```

A positive change means an increase in trade working capital. A negative
change means a decrease or release. Phase 3E-C does not invert the sign or
label the amount as cash consumed or generated. If any component at either
boundary is negative, the result retains the raw components but has no value
and warning `negative_working_capital_component`.

## Point-in-time behavior and lineage

Every method requires an explicit provider dataset, company, filing scope,
ending fiscal quarter, and timezone-aware `as_of`. Aware timestamps are
normalized to UTC; naive timestamps are rejected. There is no provider or
standalone/consolidated fallback.

Restatements affect a calculation only when their `available_at` timestamp is
at or before the requested cutoff. Re-querying an earlier cutoff reproduces
the original immutable inputs. Availability is the maximum knowledge time of
all inputs required by the calculation:

- both TTMs for CFO conversion and CFO/EBITDA;
- TTM revenue plus both boundary snapshots for receivable days and trade
  working-capital change.

Every result retains complete TTM and/or snapshot objects. Their nested
quarterized facts, filings, source records, raw object keys, and payload
locators remain available for audit and reproduction.

## Deliberate omissions

Phase 3E-C does not calculate free cash flow. Although `capex_reported` exists,
the controlled metric model does not yet encode whether a provider reports
capital expenditure as a positive expenditure magnitude or a negative cash
flow. Subtracting it without that convention could reverse the formula.

Inventory days, payable days, and the cash conversion cycle are also deferred.
The controlled vocabulary does not yet provide reliable COGS or purchases
denominator semantics. Phase 3E-C does not fabricate those metrics or infer
them from revenue, margins, or inventory movements.
