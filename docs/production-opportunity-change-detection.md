# Production opportunity change detection

Production L compares two completed, immutable Production K discovery runs. It
describes factual research-state transitions for operational review; it does
not produce a score, signal, recommendation, alert, watchlist, or portfolio
action.

## Policy and compatibility

`config/opportunity/production_opportunity_change_v1.json` binds the accepted
Production K discovery policy checksum and versions every comparison semantic.
A comparison is valid only when the baseline and current runs are completed,
the baseline cutoff strictly precedes the current cutoff, and both runs have
identical discovery, scoring-configuration, Research V4, financial primitive,
financial endpoint, ranking, snapshot-selection, and model-family semantics.
Incompatible runs fail closed as `incompatible_runs`; no valid comparison
items are written.

## Identity and universe membership

Items match by `security_id` whenever it exists. Thus a symbol rename creates
one `symbol_changed` record rather than an exit and entry. When identity was
unavailable in both K runs, exact symbol is the deterministic fallback and the
record says its identity basis is `symbol`.

Baseline-only and current-only identities use `left_compared_universe` and
`entered_compared_universe`. These codes do not imply lost or gained
rankability because there is no comparable K state on the absent side.

## Factual transition semantics

Snapshot identity compares the exact persisted snapshot ID and fingerprint.
Rankability, unranked reason, freshness, and eligibility come directly from K
items and their referenced snapshots. Partiality comes specifically from the
persisted `ScoreSnapshot.snapshot_status == "partial_component_set"`, not K's
single prioritized unranked reason. A partial-to-complete transition records
`coverage_completed` plus `became_rankable`; complete-to-partial records
`coverage_regressed` plus `lost_rankability`. These codes remain independent
of freshness and eligibility, and no missing score is imputed.

`score_delta = current final_score - baseline final_score` only when both K
items are rankable. Any exact non-zero Decimal is recorded as changed.
`rank_delta = baseline score_rank - current score_rank`, so a positive value
means the dense score rank moved up. Rank is relative: an unchanged score can
move rank, and a changed score can retain rank. K `display_order` is never
compared as merit.

Top-level coverage, confidence, available components, persisted component
scores, and persisted final contributions are compared descriptively. Missing
components are gained or lost, never treated as zero. Confidence cannot affect
score, rank, severity, or priority. V1 does not diff subfactors and creates no
change-severity score.

## Persistence and commands

Migration `20261003_0020` adds only `opportunity_change_runs` and
`opportunity_change_items`. The deterministic run key binds the policy, exact
baseline/current K run IDs and keys, both selected-snapshot-set checksums, and
the comparison version. Exact reruns reuse the completed record; conflicts
fail closed. Later K runs cannot mutate a frozen L comparison.

```powershell
python -m inflector_data.opportunity_change_cli compare `
  --database-url $env:INFLECTOR_DATABASE_URL `
  --change-policy config/opportunity/production_opportunity_change_v1.json `
  --baseline-run-id <uuid> `
  --current-run-id <uuid>

python -m inflector_data.opportunity_change_cli summarize `
  --database-url $env:INFLECTOR_DATABASE_URL `
  --change-policy config/opportunity/production_opportunity_change_v1.json `
  --change-run-id <uuid> `
  --changed-only `
  --change-code score_increased `
  --limit 50 `
  --export-csv changes.csv
```

Summary filters and limits affect presentation only. CSV is derivative; the
database, the exact K runs, and immutable ScoreSnapshots remain authoritative.
The output is deterministically ordered by comparison identity and symbols.

Production L performs no acquisition, current research, endpoint resolution,
V5 computation, K ranking, historical-outcome analysis, scheduling, alerting,
user watchlist management, or frontend work.
