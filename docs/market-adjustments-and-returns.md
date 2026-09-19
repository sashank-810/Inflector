# Corporate-action-aware price adjustments and simple returns

Phase 3G-B derives immutable in-memory evidence from the Phase 3G-A market and
benchmark PIT readers plus a corporate-action PIT reader. The output is a
deterministic price-comparability primitive, not a persisted adjusted-price
feed, total-return series, market feature, valuation, or score.

## Action knowledge and revision selection

Corporate-action queries require an explicit provider dataset, security, and
timezone-aware cutoff. Accepted actions become visible inclusively when
`available_at <= as_of`. `available_at` is knowledge time; `effective_date` is
economic share-basis time. `revision_at` and `ingested_at` only rank already
eligible observations.

Eligible revisions use descending availability, revision-or-availability,
ingestion time, and immutable UUID. The reader first retains the newest row per
`SourceRecord.external_record_id`, then retains the newest row per canonical
`(action_type, event_anchor)` economic event. This removes an old event anchor
when the same external action is corrected. Returned actions remain ordered by
event anchor, action type, availability, and UUID. Every action retains its raw
`SourceRecordView`; mismatched action/source provider datasets fail closed.

Market prices and corporate actions use separate explicit provider dataset
arguments. They may legitimately come from different licensed or exchange
feeds. Neither reader performs provider fallback or reconciliation.

## Supported price factors

Only `split` and `bonus` actions create Phase 3G-B price factors. Both require a
positive ratio and an effective date.

For a split, `numerator:denominator` means post-split shares to pre-split
shares. A 2:1 split has:

```text
share_factor = 2 / 1
price_factor = 1 / 2
```

For a bonus, `numerator:denominator` means bonus shares to existing shares. A
1:2 bonus creates three post-event shares for two existing shares:

```text
share_factor = (2 + 1) / 2 = 3 / 2
price_factor = 2 / (2 + 1) = 2 / 3
```

Provider adapters must normalize provider-specific representations into these
canonical meanings. All calculations use `Decimal` without float conversion or
internal formatting/rounding.

Cash dividends are excluded because these are price returns, not total returns.
Rights are excluded because TERP semantics require separate review. Symbol
changes create no per-share factor, and security replacements are not stitched
across distinct security IDs.

## Basis and adjusted bars

The adjustment basis is the latest selected raw economic trading date, never
the knowledge cutoff date. For a raw bar on `D` and basis `B`, an adjustment is
applied only when:

```text
D < action.effective_date <= B
```

The effective-date bar is already on the post-action basis. A known action
effective after the selected basis remains visible in `selected_actions` but is
not in `applicable_adjustments`. A historical subset ending before a later
action is not rebased. Conversely, an action between a requested subset's first
bar and basis remains applicable even when the query starts after earlier,
irrelevant actions.

Multiple supported factors compound multiplicatively in effective-date order.
Only raw open, high, low, and close are multiplied. Volume, delivery quantity,
delivery percentage, and market cap remain untouched inside the retained raw
bar. Each adjusted bar records its cumulative factor, applied action evidence,
basis, normalized UTC cutoff, and `available_at` equal to the maximum of its raw
bar and applied actions.

## Adjacent simple returns

Security simple price return is calculated between adjacent observations in
the selected adjusted series, not adjacent calendar days:

```text
current_adjusted_close / previous_adjusted_close - 1
```

Benchmark simple return uses adjacent raw PIT benchmark closes and the same
formula. A non-positive previous close produces `value=None` with
`non_positive_previous_close`; a current close of zero after a positive close
produces `-1`. Return availability is the maximum availability of its two
endpoint bars. Both endpoint objects retain recursive raw/action lineage.

There are no log, cumulative, weekly, monthly, relative, excess, alpha, beta,
or dividend-aware returns in this phase. No factors, adjusted bars, or returns
are persisted.
