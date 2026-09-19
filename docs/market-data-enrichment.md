# Phase 2D-A point-in-time market-data enrichment foundation

Phase 2D-A extends the append-only raw market-data spine. It stores optional
provider-reported market capitalization, delivery quantity, delivery fraction,
and provider-local daily benchmark bars. Phase 3G-A now selects those immutable
observations at explicit knowledge cutoffs; storage still calculates no
analytical feature.

## Price-bar enrichment

`price_bars` retains the existing raw OHLCV columns and adds nullable
`market_cap`, `delivery_quantity`, and `delivery_percentage` fields. Market cap
is an exact provider-reported INR value and is never derived from price or
shares. Delivery percentage is an exact fraction in `[0, 1]`; `0.42` means 42%.
It is never inferred from volume or silently converted from percentage points.
Legacy OHLCV-only records remain valid with all three columns null.

Supplied market cap and delivery quantity must be non-negative. Supplied
delivery percentage must be within `[0, 1]`. Malformed or invalid enrichments
quarantine the source observation. An enrichment change participates in the
economic bar comparison, so a strictly later corrected observation appends a
new `PriceBar` and `SourceRecord` rather than overwriting history.

## Benchmark identity and bars

`benchmark_series` identifies a benchmark by
`(provider_dataset_id, benchmark_code)`. Equal codes from unrelated providers
remain separate; there is no provider reconciliation or embedded exchange
meaning. Each series retains display name, three-letter currency, status, and
creation time.

`benchmark_bars` stores raw daily OHLC observations with a unique immutable
source record, trading date, interval, `available_at`, optional `revision_at`,
and ingestion time. Daily intervals and non-negative, internally consistent
OHLC values are required. A correction for the same series/date/interval must
have strictly later availability/revision ordering and appends a new row.

`BenchmarkBarRecord`, `BenchmarkDataProvider`, `CSVBenchmarkDataProvider`, and
`MockBenchmarkDataProvider` use the same batch/envelope and archive-first
contract as existing ingestion. Identical source identity/content is
idempotent. Raw payload bytes, content hashes, source URI, object key, and row
reference remain auditable. Failed normalized writes roll back bars, series,
sources, and accepted counters while retaining the failed ingestion-run audit.

## Deliberate boundaries

This phase stores raw observations only. Phase 3G-A implements the separate
read-only PIT selection layer described in
[`market-point-in-time.md`](market-point-in-time.md). Adjustment factors,
adjusted prices, returns, relative strength, moving averages, volatility,
delivery ratios, valuation ratios, market-structure scoring, and attention
proxies remain deferred. Phase 3G-B now supplies non-persisted split/bonus
adjustments and adjacent simple price returns. Valuation and Market Structure
scoring must still wait for their approved deterministic foundations.
