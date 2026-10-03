# Production opportunity monitoring

Production M extends the accepted daily operations cycle with orchestration
only:

```text
bounded acquisition and financial refresh
-> current Research V4 / V5 snapshot attempts
-> Production K opportunity discovery
-> Production L opportunity change comparison
```

It does not recompute research, ranking, or change semantics. The monitoring
stages call the accepted K and L module APIs and persist bounded references in
the existing Production E operational stage ledger.

## Operations V4

`nse_daily_operations_v4` preserves the V3 Asia/Kolkata 20:00 schedule,
acquisition bounds, delivery handling, leases, retries, stale-run policy,
missed-job behavior, and one-instance rule. It additionally binds:

- discovery policy `production_opportunity_discovery_v1` and checksum
  `f90e463d3c8940cf9f93210e4d0806bdf4d535340a7d4140ba7d3e517d2e99d2`;
- change policy `production_opportunity_change_v1` and checksum
  `2189bdcf6243545524fa8cca8721cd4c5301b775a8c9f89f83c481c55bd646a9`;
- cutoff semantic `operational_knowledge_cutoff_v1`;
- baseline semantic `latest_prior_compatible_completed_monitoring_stage_v1`;
- no-baseline behavior `complete_without_change_run_v1`.

Startup and doctor checks load the actual policy files and verify their
content-derived checksums. A path or checksum mismatch fails closed. Operations
V1 through V3 do not contain or run monitoring stages.

## Cutoff and universe

The discovery cutoff is the operational run's persisted `knowledge_cutoff`.
It is frozen when the cycle is planned and is reused on every retry; monitoring
never reads the wall clock to create a replacement cutoff.

K receives the exact normalized ordered symbol tuple already bound to the
operational run. Monitoring neither expands nor filters that universe. A later
cycle may use a different symbol tuple; L reports entry and exit from the
compared universe according to its accepted semantics.

## Stage order and results

`opportunity_discovery` is a required stage after `current_research` reaches
its accepted terminal state. Partial or unavailable issuer research is not a
monitoring failure: K retains those states as unranked items. The stage ledger
stores the K run ID/key, cutoff, selected-snapshot-set checksum, requested
symbol checksum, and rankable/unrankable counts. K tables remain authoritative.

`opportunity_change` runs only after discovery completes. Its result stores the
L run ID/key, frozen baseline/current K run IDs, and compared/changed counts.
L tables remain authoritative. The operations layer implements no dense ranks,
score deltas, rank deltas, or component comparison rules.

## Monitored baseline lineage

A baseline candidate must be referenced by a completed
`opportunity_discovery` stage from a previously completed Operations V4 run in
the same monitoring stream. The operations-profile checksum, Research V4
checksum, model family, and K semantic bindings must be compatible. Manual or
ad-hoc K runs are never candidates.

Among compatible candidates with a discovery cutoff strictly before the
current cutoff, the greatest cutoff wins. Universe checksums need not match.
If a day is missed, the next cycle compares with the latest actual earlier
completed monitored cycle; no synthetic daily run is created.

Baseline selection is committed to the current stage ledger before L is
invoked. A failed L retry therefore reuses the same baseline even if an
intermediate monitoring cycle is later backfilled. No compatible baseline is a
successful first-run state: the stage records `no_compatible_baseline` and does
not fabricate an `OpportunityChangeRun`.

## Recovery and scheduling

Existing DB leases, attempt limits, explicit stale-run resume, and completed-
stage reuse remain authoritative. A K failure prevents L. An L failure leaves
research and K immutable; resume skips their completed stages and retries L
with the frozen current and baseline identities. K and L deterministic
identities provide their own duplicate/conflict protection.

The existing `ops_cli doctor-auto` and `run-cycle-auto` commands accept
Operations V4. The existing single Windows scheduled task and
`scripts/run_inflector_scheduled.ps1` require no second task or scheduler: the
profile selects the V4 stage chain. Doctor reports policy readiness and prior
baseline availability; absence of a baseline is informational.

Production M adds no migration or table. Migration head remains
`20261003_0020`. It sends no alerts, creates no notification outbox or user
watchlist, and adds no recommendation, portfolio, API, or frontend behavior.
