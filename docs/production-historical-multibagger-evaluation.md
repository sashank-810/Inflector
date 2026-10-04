# Production historical multibagger evaluation

Production Q adds an outcome-only ground-truth layer above the frozen
Production J historical observations. It does not train, tune, score, rank, or
classify model predictions. In particular, Q does not produce TP, FP, FN, or
TN: those concepts require a separately frozen prediction rule.

## Historical universe

`config/backtest/production_historical_universe_v1.json` defines an NSE `EQ`
ordinary-equity cohort. It binds the accepted `nse_official` /
`nse_cm_mii_security_daily` archive of the official
`NSE_CM_security_ddmmyyyy.csv.gz` CM MII daily security master rather than
accepting a caller-selected provider or treating UDiFF prices as membership.
Each transport row contains the exact normalized MII security semantic row,
not independently authoritative membership fields. Q canonicalizes that row,
requires its SHA-256 to equal the accepted `SourceRecord.content_sha256`,
validates `CM` / `STK` / `EQ`, cross-checks the source external identity, and
then derives the trading date, symbol, and ISIN. Provenance retains the source
ID, completed ingestion-run ID, content hashes, raw object/reference, derived
fields, and semantic-row verification version. The source record's
retrieval/availability time remains
distinct from the historical membership date. A source retrieved later may
prove an outcome sampling frame; it does not become research evidence at the
old cutoff.

One cohort binds exactly one completed ingestion of one archived MII artifact.
The ingestion must have no quarantined or duplicate rows, its accepted/received
counts must equal the persisted eligible source-record count, and every row
must share the source date, raw-content SHA-256, raw object, and source URI.
The evidence transport must contain exactly the accepted eligible SourceRecord
ID set from that ingestion: missing, extra, duplicate, or mixed-ingestion rows
fail before identity resolution. The ordered verified member fingerprints and
their complete-set checksum are part of the run identity and provenance.

For a calendar-month-end cohort, the snapshot must be the latest supported MII
security-master date on or before the cutoff and that date must be in the
cutoff month. An arbitrary older artifact, current `EQUITY_L.csv`, UDiFF subset,
or current survivor list cannot silently define the historical cohort. If the
required archived MII security-master snapshot is absent, the build is
data-blocked rather than synthesized.

Identity resolution prefers exact ISIN/security identity over exact historical
symbol plus listing interval. It never fuzzy-matches. Inactive and subsequently
delisted securities remain members when their historical listing interval
contains the membership date; current listing/security status is not queried.
For outcomes, all NSE listing intervals belonging to the exact canonical
security are followed. A subsequent symbol/listing interval continues the same
security; only the final end of its complete NSE listing history is a terminal
delisting, and an open-ended later listing has no terminal end.
Unresolved, ambiguous, unsupported-series, and unsupported-security cases are
persisted explicitly and included in coverage counts.

Production J's reviewed 25-symbol bound is unchanged. Q sorts eligible resolved
members by the policy fields and emits deterministic shards of at most 25.
Shard checksums bind every member fingerprint. Recomposition has neither
duplicates nor omissions.

## Outcome contracts and states

`config/backtest/production_multibagger_outcomes_v1.json` defines three peer
diagnostic contracts:

- `MB_2X_2Y`: adjusted close reaches 2x within two calendar years;
- `MB_3X_3Y`: adjusted close reaches 3x within three calendar years;
- `MB_5X_5Y`: adjusted close reaches 5x within five calendar years.

All arithmetic is exact `Decimal`. The entry is the exact raw PriceBar that was
PIT-visible at the frozen J knowledge cutoff, expressed on the existing
split/bonus-adjusted outcome price basis. Cash dividends remain excluded.
Rights or replacement actions inside the contract window fail closed.

Each observation/contract has one of four factual states:

- `positive`: any adjusted close strictly after entry and on or before the
  inclusive calendar horizon reaches the multiple. A later decline does not
  erase the hit. Maturity uses the raw entry and hit availability plus only
  split/bonus evidence economically between entry and hit. A later rebasing
  action that cancels from both sides of the ratio does not delay maturity.
- `negative`: the full horizon has elapsed and every expected NSE trading date
  from the bound NIFTY 50 daily session reference has an accepted security
  price bar. One post-horizon bar or a bar count is insufficient. Because V1
  cannot distinguish missing ingestion from a genuine no-trade day, either
  case fails closed as `incomplete_outcome_window`. Maturity is never earlier
  than the horizon and the evidence proving complete coverage.
- `unmatured`: no hit yet and the calendar horizon has not elapsed.
- `unavailable`: integrity is insufficient. Reasons include missing/rebased
  entry, ambiguous history, unsupported actions, insufficient matured history,
  and delisting without an approved terminal-value rule. Unavailable is never
  imputed as negative.

Continuous diagnostics retain entry, peak, hit and endpoint observations,
price multiple and returns, time to threshold, observation counts, listing end,
source/action provenance, and running-peak maximum drawdown within the contract
window. Missing values stay null. Maximum drawdown is security adjusted-close
drawdown only and never uses prices after the contract horizon.

## Persistence and immutability

Migration `20261004_0023` adds:

- `historical_universe_runs` and `historical_universe_members`;
- `multibagger_label_runs` and `multibagger_outcome_labels`.

Cohort identity binds policy, cutoff, provider dataset, completed ingestion,
source date, raw artifact SHA-256, eligible source-row count, verified semantic
rows, ordered member fingerprints, and complete-member-set checksum. Label-run
identity binds the Q policy,
frozen J run, explicit outcome cutoff, market/action/benchmark calendar state,
ordered contracts, and the versioned completeness/maturity algorithms.
Identical input reuses immutable state; a later outcome
cutoff or changed source state creates a new run rather than rewriting history.

## Research/outcome firewall

The dependency direction is evaluation to frozen research state. Research,
scoring, K discovery, L changes, N projection, and O delivery neither import nor
query Q labels. Q never reads a final score or rank to establish ground truth.
Financial or market evidence retrieved later is still excluded from historical
research by `available_at`; future price evidence is read only after a J
observation is frozen and only inside the outcome subsystem.

## Operator commands

All output is deterministic sorted-key JSON:

```powershell
python -m inflector_data.backtest_cli build-historical-universe `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --universe-policy config/backtest/production_historical_universe_v1.json `
  --source-dataset-id <uuid> --source-ingestion-run-id <uuid> `
  --cutoff <aware-time> --completed-at <aware-time> `
  --evidence-json <source-linked-projection.json>

python -m inflector_data.backtest_cli inspect-historical-universe `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --universe-policy config/backtest/production_historical_universe_v1.json `
  --run-id <uuid>

python -m inflector_data.backtest_cli build-multibagger-labels `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --multibagger-policy config/backtest/production_multibagger_outcomes_v1.json `
  --research-profile config/research/production_research_v4.json `
  --backtest-run-id <uuid> --outcome-data-cutoff <aware-time>

python -m inflector_data.backtest_cli summarize-multibagger-labels `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --multibagger-policy config/backtest/production_multibagger_outcomes_v1.json `
  --run-id <uuid> [--contract MB_2X_2Y]
```

The evidence JSON transports `source_record_id` plus the exact full
`semantic_row` whose canonical hash created that SourceRecord. It cannot attach
fabricated membership fields to an unrelated accepted record, select only a
subset of the completed source snapshot, add an unrelated row, or mix rows from
two snapshots. The
summary reports membership resolution, factual label-state counts, unavailable
reasons, coverage, and entry/maturity bounds. It reports no score-performance
claim or recommendation.

## Limitations

Q does not assert that a production archive currently contains a complete NSE
history. A real cohort requires licensed archived official historical MII
security-master artifacts and uniquely resolvable identities. In particular,
no current-list or bhavcopy fallback exists. Delisting remains unavailable
without reviewed terminal value evidence. The contracts are price-only and do
not represent total shareholder return. Fixture tests prove mechanics, not
real-data coverage or model quality.
