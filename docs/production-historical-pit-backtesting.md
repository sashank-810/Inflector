# Production historical PIT backtesting

Production J freezes historical V5 research states and evaluates later price
outcomes without allowing outcomes to influence research construction. It is a
measurement layer, not a ranking, portfolio, recommendation, or calibration
system.

## Strict knowledge time

The official mode is `strict_knowledge_time`. Every research reader continues
to require `available_at <= knowledge_cutoff`. Economic dates such as a fiscal
period end, trading date, ex-date, or announcement date do not establish what
Inflector knew. A 2024 artifact first retrieved in October 2026 is therefore
invisible to every 2024 cutoff.

Retrospective event-time research is conceptually distinct. It may describe
historically dated observations where publication time is unproven, but its
results are not leakage-safe and are never included in Production J performance
metrics.

The versioned manifest
`config/backtest/production_historical_availability_v1.json` classifies each
source:

| Source | Class | Strict treatment |
|---|---:|---|
| canonical NSE universe | B | persisted identity creation plus PIT-visible market evidence |
| integrated financials | B | actual persisted availability only |
| UDiFF prices | B | actual persisted availability only |
| NIFTY benchmark | B | actual persisted availability only |
| delivery | B | actual persisted availability only |
| corporate actions | B | actual persisted availability only |
| announcements/events | B | actual persisted availability only |
| GDELT attention | B | actual persisted availability only |
| analyst attention | D | not configured |
| market capitalization | C | unavailable historically |

Class A means safely PIT-usable under a separately qualified historical
availability clock; B means usable only from actual observed ingestion onward;
C means unavailable historically; D means not configured. The initial manifest
adopts no new Class A timestamp and never rewrites source records.

## Frozen policy

`config/backtest/production_backtest_v1.json` binds the accepted scoring asset,
Research V4, the financial primitive and endpoint policies, and the historical
availability manifest by checksum. Its reviewed evaluation semantics are:

- UTC calendar month-end cutoffs;
- requested, bounded `observed_pit_universe` with at most 25 symbols;
- forward horizons of 21, 63, 126, and 252 trading observations;
- split/bonus-adjusted close **price return** using the accepted market
  adjustment primitive;
- cash dividends excluded, so results are not total shareholder return;
- NIFTY 50 benchmark values on compatible entry/exit trading dates;
- no imputation for missing, suspended, delisted, or truncated outcomes;
- all observations retained for audit, while headline state-change statistics
  use `backtest_research_state_projection_v1` to exclude volatile IDs and
  timestamps;
- predefined score buckets are reported only when their minimum sample size is
  met.

No threshold, holding period, score curve, weight, filter, or bucket is selected
after observing returns.

## Universe and survivorship limitation

The first universe is an explicit symbols file, not today's exchange universe
projected backward. A symbol enters a cutoff only when its persisted company,
security, and NSE listing identities existed by the cutoff, its historical
listing interval contains the cutoff, and an accepted market observation was
PIT-visible. A listing with a non-null later `valid_to` remains eligible before
that end date; current-listing status is not projected backward. A listing
whose `valid_to` predates the cutoff, or whose identity was persisted only
after the cutoff, is excluded.

This is deliberately conservative but not a complete historical NSE universe.
The current assembler cannot reconstruct every delisted, merged, renamed,
suspended, or historical-series security. Results must therefore be described
as the performance of the observed PIT-supported requested universe, not as
market-wide unbiased history. A missing future price after a known delisting is
`outcome_unavailable_due_to_delisting`; no terminal value means no imputed zero
return.

## Three-stage workflow

Build research snapshots without reading future outcomes:

```powershell
python -m inflector_data.backtest_cli build-dataset `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --backtest-policy config/backtest/production_backtest_v1.json `
  --availability-manifest config/backtest/production_historical_availability_v1.json `
  --research-profile config/research/production_research_v4.json `
  --model-family inflector_v1 --symbols-file .\symbols.txt `
  --from-cutoff 2026-10-31T23:59:59.999999+00:00 `
  --to-cutoff 2027-03-31T23:59:59.999999+00:00
```

For each cutoff and eligible company this calls the accepted automatic endpoint
resolver, Production G/F/D evidence assembler, and unchanged V5 orchestrator.
The newest PIT-visible endpoint wins even when it is partial. Partial snapshots
are retained with `final_score = null`; an older complete quarter is never used
as a coverage fallback.

After snapshots are frozen, build outcomes from data available by a separate,
explicit evaluation cutoff:

```powershell
python -m inflector_data.backtest_cli build-outcomes `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --backtest-policy config/backtest/production_backtest_v1.json `
  --research-profile config/research/production_research_v4.json `
  --run-id <backtest-run-uuid> `
  --outcome-data-cutoff 2027-12-31T23:59:59+00:00
```

The immutable entry observation is the latest raw market bar whose trading date
is on or before the research cutoff date **and** whose actual persisted
`available_at` is on or before that knowledge cutoff. The outcome builder keeps
that bar ID, constructs the fully split/bonus-adjusted series at the separate
outcome-data cutoff, and uses the adjusted value for that exact raw bar. It
fails closed if that exact bar is no longer representable in the selected
outcome-time series; it never substitutes a later-downloaded historical bar.

Horizon 21 uses the 21st eligible security trading observation strictly after
the research cutoff date; 63, 126, and 252 follow the identical observation
rule. The NIFTY 50 entry uses the security entry's factual trading date, and its
exit uses the security exit date, from benchmark evidence visible by the
outcome-data cutoff. Missing compatible benchmark data can leave the security
price return available while benchmark and excess returns remain null. Split
and bonus factors come from the existing adjustment code. Rights and security
replacement inside a window fail closed as unsupported outcome states.

Finally summarize and optionally export deterministic derivative CSV:

```powershell
python -m inflector_data.backtest_cli summarize `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --backtest-policy config/backtest/production_backtest_v1.json `
  --run-id <backtest-run-uuid> `
  --export-csv .\local-artifacts\backtest.csv
```

The summary reports counts, component availability, final-score and partial
cohorts, outcome availability, mean/median price and excess returns, positive
return rate, and the predeclared 252-observation 100%-return count when data is
available. Small cohorts receive explicit warnings. CSV is derivative; the
database and versioned assets remain authoritative.

## Persistence and reproducibility

Migration `20261003_0018` adds only:

- `backtest_runs`: deterministic policy/source-state identity and lifecycle;
- `backtest_observations`: audited universe/research states referencing existing
  immutable `score_snapshots`;
- `backtest_outcomes`: one immutable status/value per observation and horizon.

The run key binds policy and manifest checksums, the actual scoring
configuration, Research V4 and its semantic-policy checksums, cutoff range,
cadence, universe, benchmark, horizons, ordered symbols, and the source state
visible at the range endpoint. Identical completed work is reused. Changed
semantic configuration or visible source state produces a different identity.
Outcome data never enters research imports or fingerprints.

## Current depth and limitations

Strict usable depth begins only when the relevant records were actually
ingested and the accepted scoring configuration was active. Production J does
not manufacture earlier history. Recent observations may have a 21-observation
outcome while longer horizons remain unavailable. Market cap and analyst
coverage remain unavailable, cash dividends are excluded from price returns,
and complete historical universe/delisting settlement evidence is not yet
available. These limitations constrain inference and must accompany every
reported result.
