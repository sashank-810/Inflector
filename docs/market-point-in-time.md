# Point-in-time market and benchmark reads

Phase 3G-A selects immutable raw observations that were knowable at an explicit
cutoff. It performs no price adjustment, return calculation, valuation, market
structure analysis, provider reconciliation, or scoring.

## Explicit identities

Every security query requires `provider_dataset_id`, `security_id`, exact
`interval`, and a timezone-aware `as_of`. Its economic bar identity is:

```text
provider dataset + security + trading date + interval
```

Symbols, exchange listings, and mutable security status are not consulted.
Provider choice belongs above this deterministic reader; there is no fallback.

Benchmark identity is provider-local:

```text
provider dataset + benchmark code
```

Equal benchmark codes from different providers remain independent. Mutable
benchmark status is not a historical eligibility gate. An accepted benchmark
bar whose source dataset conflicts with its series dataset raises
`MarketDataIntegrityError`; inconsistent provenance is never returned.

## Knowledge eligibility and revision order

A bar is eligible when its source is `accepted` and:

```text
bar.available_at <= as_of
```

The boundary is inclusive. Aware offsets are normalized to UTC and naive
cutoffs are rejected. `ingested_at` is audit metadata, not a knowledge gate.
Likewise, `revision_at` is not independently required to precede the cutoff.
It ranks observations that are already eligible by `available_at`.

Within one economic identity revisions are ordered by:

1. `available_at` descending;
2. `coalesce(revision_at, available_at)` descending;
3. `ingested_at` descending;
4. immutable bar UUID descending.

Content magnitude and enrichment completeness never influence selection.

## Atomic rows and series

A selected `PriceBar` is atomic. OHLC, volume, market cap, delivery quantity,
and delivery percentage all come from the same immutable row. If a correction
has a new close and null market cap/delivery fields, those fields remain null;
the reader does not borrow enrichment from an older revision.

Series return one selected revision per `trading_date`, sorted by economic date
ascending. Optional start/end bounds are inclusive and apply to trading dates.
No weekends, holidays, or missing sessions are filled, interpolated, or
forward-filled. Repeated reads have deterministic ordering and preserve exact
`Decimal` values plus complete `SourceRecordView` raw lineage.

The `latest_*_as_of` methods choose the greatest eligible economic
`trading_date`, optionally bounded by `on_or_before`. A late correction to an
older date does not make that date economically latest. No freshness or
staleness policy is embedded in this convenience read.

## Deliberate boundary

Phase 3G-A returns raw PIT evidence only. It does not consult corporate actions,
create adjusted OHLC, calculate security or benchmark returns, calculate
relative strength or other market-structure features, combine market cap with
financial facts for valuation, score completeness, or infer attention. Those
behaviors require separately approved deterministic and policy phases. Phase
3G-B remains planned for corporate-action-aware price transformations.
