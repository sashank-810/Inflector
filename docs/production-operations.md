# Production operations

Production Data Activation E adds a recoverable execution layer around the
accepted Production A-D ingestion, evidence assembly, and V5 orchestration. It
does not add or alter research, accounting, eligibility, confidence, component,
or scoring formulas. The scheduler calls the accepted implementations and
records what happened.

## Operations profile and explicit clock

[`production_operations_v1.json`](../config/operations/production_operations_v1.json)
defines `nse_daily_operations_v1`. Its canonical JSON checksum identifies the
execution policy. The profile fixes `Asia/Kolkata`, local schedule `20:00`, a
120-calendar-day market request, at most 100 stable-deduplicated ordered
symbols, optional GDELT execution, three explicitly resumed stage attempts, a
two-hour lease, ingestion request bounds, one scheduler instance, and
StartWhenAvailable behavior. Research datasets, benchmark, financial context,
listing horizon, catalyst window, confidence inputs, and source reliability
remain exclusively in the research profile.

`run-cycle` requires an offset-aware `--cycle-at`. The knowledge cutoff is
exactly that timestamp converted to UTC. The scheduled PowerShell wrapper reads
the wall clock once, materializes an offset-aware Asia/Kolkata timestamp, and
passes it explicitly. No market-close offset, latest-quarter inference, holiday
calendar, or downstream current-time research decision is hidden in operations.
The fiscal year and quarter remain explicit.

## Run identity and ledger

Migration `20261002_0016` adds `operational_runs`,
`operational_run_stages`, and `operational_run_symbols`. The deterministic run
key binds both profile identities/checksums, model family and immutable model
identity, fiscal endpoint, `cycle_at`/knowledge cutoff, normalized ordered
symbol-set checksum, GDELT execution choice, and caller-supplied source
classification labels. Raw-root paths are retained as immutable resume inputs;
the database URL and credentials are never persisted.

A unique run-key constraint prevents duplicate logical cycles. An exact rerun
of a completed cycle returns the same ledger row with `already_completed=true`.
A different explicit cycle timestamp creates a different run. Run states are
`planned`, `running`, `completed`, `completed_with_symbol_failures`, `failed`,
and `stale`. A valid partial or ineligible V5 snapshot is a successful symbol
result; it is never an operational failure merely because its final score is
null.

Stages are ordered as:

1. preflight
2. universe
3. market history
4. financials
5. corporate actions
6. catalyst evidence
7. optional news attention
8. model/configuration initialization
9. current V5 research

Each stage records status, timestamps, attempt count/history, bounded JSON
summary, and error code/message. Raw source bodies remain only in the existing
archive-first object stores. Required-stage failure blocks dependents. A
bounded GDELT acquisition failure is recorded as attention unavailable and may
allow current research to continue under the accepted missing-evidence rules.
NSE `source_not_available` remains source absence—not zero and not an inferred
holiday.

## Lease, interruption, and recovery

The database row owns an expiring lease with owner token, acquisition time, and
expiry. Conditional database updates prevent a second process from acquiring
the same active logical run. An active, non-expired lease is never stolen. A
crashed process eventually exposes an expired lease; only the explicit
`resume-run` command may recover it. Resume reloads the original immutable
inputs, rejects changed files/profile content, preserves completed stages, and
retries only failed/blocked/not-run stages within the profile attempt bound.
Underlying ingestion and snapshot idempotency remain authoritative; there is
no transaction pretending to roll back external HTTP acquisition.

## Commands and exit behavior

Run the read-only doctor before unattended use:

```powershell
python -m inflector_data.ops_cli doctor `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --operations-profile config/operations/production_operations_v1.json `
  --research-profile config/research/production_research_v1.json `
  --model-family inflector_v1 --symbols-file .\symbols.txt `
  --fiscal-year 2025 --fiscal-quarter 4 `
  --cycle-at 2026-10-02T20:00:00+05:30 `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --nse-license-class $env:INFLECTOR_NSE_LICENSE_CLASS `
  --gdelt-raw-root $env:INFLECTOR_GDELT_RAW_ROOT `
  --gdelt-license-class $env:INFLECTOR_GDELT_LICENSE_CLASS `
  --model-semantic-version 1.0.0 --git-sha <accepted-sha> `
  --model-effective-from 2026-10-01T00:00:00+05:30
```

Replace `doctor` with `run-cycle` to execute. Inspect `status --run-id UUID`,
`status` for the latest run, or `status --last 10` for bounded recent history.
Recover only after investigation with `resume-run --database-url ... --run-id
UUID`. Run `python -m inflector_data.ops_cli burn-in` for a deterministic,
no-network ledger/idempotency check using fictional symbols.

Exit `0` means the cycle completed operationally, including valid partial V5
snapshots. Exit `2` means some symbols failed operationally while later symbols
still ran and successful results remain persisted. Exit `1` means a cycle-
level operational/integrity failure. Every command emits machine-readable JSON;
stage logs carry run ID, run key, and stage without source bodies or database
credentials.

## Research-state comparison

`research_state_projection_v1` compares the new/reused snapshot with the
immediately preceding snapshot for the same company, model family, and
configuration checksum. It compares only persisted snapshot status, component
availability/missingness, top-level coverage, confidence, and legitimate final
score/null. Snapshot IDs and operational metadata are excluded. Results are
`initial_no_baseline`, `unchanged`, or `changed`, with factual before/after
values and created/reused/fingerprint facts. No materiality, attractiveness,
direction, alert, or recommendation label is generated.

## Windows scheduling

[`run_inflector_scheduled.ps1`](../scripts/run_inflector_scheduled.ps1) reads
secrets and archive locations from environment variables, generates `cycle_at`
once, invokes `run-cycle`, emits its JSON (including run ID), and propagates the
exit code. [`register_inflector_scheduled_task.ps1`](../scripts/register_inflector_scheduled_task.ps1)
is optional and never runs automatically. Invoke it first with `-DryRun` or
`-WhatIf`; the preview contains no database URL. Registration uses one-instance
execution, StartWhenAvailable according to the profile, and a four-hour bound.
The host must use Asia/Kolkata local time because Windows Task Scheduler triggers
in host time.

Store `INFLECTOR_PRODUCTION_DATABASE_URL` and other runtime values in the user/machine
environment or an approved local secret store, not in the task command line.
A laptop cannot execute while powered off. StartWhenAvailable may run a missed
cycle when the host returns, but local scheduling is not a 24/7 guarantee.

## Operational limits

The immutable V1 operations profile does not acquire delivery. The V2 profile
adds an optional `delivery_history` stage backed by the accepted delivery CLI;
it contains no delivery formula and preserves resume/lease semantics. This
layer does not acquire market cap or analyst coverage; create
alerts/backtests; add recommendations; or change the frontend. Legitimate
source absence stays missing. Real smoke execution requires an already migrated
production database, separate writable NSE/GDELT archive roots, explicit NSE
and GDELT source classifications, an explicit symbols file, and approved model
identity inputs.

## Operations V3 automatic financial endpoint mode

`nse_daily_operations_v3` preserves the V2 schedule, bounds, delivery stage,
leases, stale recovery, retry behavior, and one-instance policy, but explicitly
enables automatic financial endpoints. Use `doctor-auto` and `run-cycle-auto`;
they deliberately accept no global fiscal year or quarter. The cycle still
runs the bounded existing financial-filing refresh before current research,
then each symbol resolves its own PIT-visible endpoint through Research V4.
Issuer-level no-endpoint results are legitimate and later symbols continue.

The operational run key binds the Operations V3 and Research V4 checksums,
endpoint and primitive policy checksums, scoring-policy definition checksum,
cycle cutoff, source classifications, GDELT choice, and ordered symbol set.
The existing ledger schema is reused; its legacy non-null fiscal columns use a
documented zero sentinel only for AUTO runs, while persisted canonical inputs
and public JSON carry null fiscal coordinates and `financial_endpoint_mode =
auto`. Manual V1/V2 cycles continue to require explicit fiscal coordinates.
Resume, lease, and completed-run reuse semantics are unchanged. See
[`production-financial-endpoint-discovery.md`](production-financial-endpoint-discovery.md).

Production J remains outside the unattended Production E/H cycle. Historical
dataset, forward-outcome, and summary commands are explicit bounded evaluation
jobs with their own immutable run ledger. They reuse production snapshots but
do not add a scheduler stage, change Operations V3 identity, or make future
outcomes available to current research. See
[`production-historical-pit-backtesting.md`](production-historical-pit-backtesting.md).

## Operations V4 opportunity monitoring

`nse_daily_operations_v4` preserves V3 evidence and endpoint behavior and
appends `opportunity_discovery` then `opportunity_change` after current
research. K receives the persisted operational `knowledge_cutoff` and exact
ordered symbols; neither value changes on resume. The stage ledger records
bounded K/L run references rather than copying their artifacts.

L baselines come only from prior completed compatible V4 operational discovery
stages. Manual K runs are excluded, the latest strictly earlier compatible
cutoff wins, and the selected baseline is committed before L starts. A first
run or stream with no compatible baseline completes successfully with
`no_compatible_baseline` and no fabricated change run. Existing leases,
retries, completed-stage skips, stale-run recovery, missed-job handling, and
the single scheduled task remain unchanged. See
[`production-opportunity-monitoring.md`](production-opportunity-monitoring.md).

## Operations V5 notification projection

`nse_daily_operations_v5` preserves the complete V4 chain and appends required
`notification_projection` after `opportunity_change`. It binds the exact
Production N policy asset/checksum and projects only the L run ID already
persisted by that operational stage. It never searches arbitrary or manual L
runs. First-run `no_compatible_baseline` becomes successful `no_change_run`
with zero outbox rows.

The outbox insert and stage completion share the existing operations
transaction. Failure leaves completed research/K/L immutable; resume skips
them and retries projection. Deterministic keys safely reuse prior rows. Doctor
validates the policy, L checksum binding, and migration 0021 without requiring
transport credentials. Scheduling remains the existing single Asia/Kolkata
20:00 task. See
[`production-research-notification-outbox.md`](production-research-notification-outbox.md).
