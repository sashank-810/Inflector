# Production opportunity discovery

Production K turns immutable current `score_snapshot_v5` records into a
reproducible research-prioritization list. It does not score companies, acquire
evidence, predict returns, construct portfolios, or make recommendations.

## Bound policy

`config/opportunity/production_opportunity_discovery_v1.json` binds the exact
accepted V5 scoring asset, Research V4, the financial primitive and endpoint
policy checksums, and all selection/ranking semantics. Every invocation needs
an explicit timezone-aware discovery cutoff. V1 accepts an ordered, stably
deduplicated symbols file with at most 5,000 exact current NSE symbols; it does
not fuzzy-match identities or acquire missing research.

For each resolved security, selection considers only immutable snapshots with
`knowledge_cutoff <= discovery_cutoff`. The newest cutoff under the bound
scoring configuration wins even if its score is lower than an older score.
Future snapshots are invisible. Conflicting fingerprints at the same newest
cutoff produce `ambiguous_latest_snapshot`; UUID order is never used to choose
between conflicting states.

## Rankability and freshness

Snapshot age is the difference between the discovery cutoff calendar date and
snapshot knowledge-cutoff date. V1 treats age up to seven days as fresh and age
above seven days as stale. This is a binary operational gate: no freshness
multiplier or decay changes a score.

A snapshot is headline-rankable only when it:

- matches the bound V5 configuration and Research V4 semantics;
- is fresh;
- has the persisted `final_score` produced by V5;
- retains persisted V5 eligibility;
- passes the existing immutable snapshot-integrity contract.

Partial snapshots remain valid research records but are unranked with
`partial_score`; their components are never renormalized. Ineligible, stale,
ambiguous, wrong-configuration, missing-snapshot, and unresolved-identity
states remain explicit audit items. Market capitalization and analyst coverage
remain unavailable and are neither filters nor fallbacks.

## Ranking and explanation

The sole merit key is persisted `final_score DESC`. Dense score rank gives
equal scores equal rank: `100, 100, 95` becomes `1, 1, 2`. Equal-score rows use
`symbol ASC, security_id ASC` only for deterministic display. Confidence,
coverage, liquidity, age within the fresh window, component count, market cap,
analyst coverage, and backtest outcomes do not affect rank.

Mechanical output includes eligibility, confidence, component coverage,
available/missing components, and persisted positive component contributions.
Contributions are displayed by `final_contribution DESC, component_code ASC`;
they are not recomputed and their display order does not change opportunity
rank. No generated investment thesis is produced.

## Commands

Freeze and persist a ranking:

```powershell
python -m inflector_data.opportunity_cli build-ranking `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --discovery-policy config/opportunity/production_opportunity_discovery_v1.json `
  --research-profile config/research/production_research_v4.json `
  --model-family inflector_v1 `
  --discovery-cutoff 2026-10-03T20:00:00+05:30 `
  --symbols-file .\symbols.txt
```

Summarize and optionally export a deterministic derivative CSV:

```powershell
python -m inflector_data.opportunity_cli summarize `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --discovery-policy config/opportunity/production_opportunity_discovery_v1.json `
  --run-id <discovery-run-uuid> --limit 20 `
  --export-csv .\local-artifacts\opportunity-discovery.csv
```

`--limit` affects displayed summary rows only. Persistence and CSV export retain
the complete requested universe. CSV contains exact run/policy/configuration,
snapshot identity/fingerprint, cutoff/age, classification, rank, persisted V5
score and evidence metadata. The database, immutable snapshots, and versioned
configuration remain authoritative.

## Persistence and isolation

Migration `20261003_0019` adds only `opportunity_discovery_runs` and
`opportunity_discovery_items`. A deterministic run key binds policy and
configuration checksums, cutoff, ordered universe checksum, selection/ranking
versions, and the exact selected snapshot IDs/fingerprints. Identical frozen
inputs reuse the run; changed selected snapshot state creates a different key.
Neither `ScoreSnapshot` nor Production J tables are mutated.

Discovery modules do not import or query backtest outcomes, summaries, returns,
hit rates, or multibagger labels. Production J cannot affect current ranking,
and no Production J result is used to tune K or V5.
