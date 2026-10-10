# Production readiness and operator health

Production P hardens the accepted production chain without changing its
business semantics:

`Research V4 -> K V1 -> L V1 -> N V1 -> O V1 -> Operations V6`

The accepted database migration head is `20261005_0024`; Production P itself
still has no migration or Operations V7 profile. The newer Q migration is an
evaluation-only historical-universe and label schema.

## Runtime configuration

The read-only health command recognizes these environment variable names:

- `INFLECTOR_PRODUCTION_DATABASE_URL`
- `INFLECTOR_PRODUCTION_RAW_ROOT`
- `INFLECTOR_GDELT_RAW_ROOT`
- `INFLECTOR_MODEL_FAMILY`
- `INFLECTOR_TELEGRAM_BOT_TOKEN`
- `INFLECTOR_TELEGRAM_CHAT_ID`

Only Telegram credential presence is reported. Values and hashes are never
included in output. The scheduler and existing production commands retain
their documented source-license and model-version inputs.

## Read-only health command

Run a deterministic report with an explicit timezone-aware observation time:

```powershell
python -m inflector_data.ops_cli health `
  --repository-root . `
  --observed-at 2026-10-04T20:00:00+05:30
```

The database URL, raw roots, and model family may be supplied through the
environment variables above or the corresponding CLI options. The JSON report
validates the exact Research/K/L/N/O/Operations V6 checksums and bindings,
database connectivity and migration head, raw-root availability, model-family
presence, latest operational run, run-status counts, pending N outbox count,
delivery status counts, due retries, expired claims, and credential presence.

`configuration_unavailable` means required runtime configuration was not
supplied. `broken` means supplied/configured state failed validation. The
command issues SELECT queries only: it does not create runs, acquire leases,
prepare or claim deliveries, replay dead letters, send Telegram, ingest data,
or run research/scoring.

## Scheduler registration and CI portability

`register_inflector_scheduled_task.ps1 -DryRun` is a cross-platform PowerShell
contract. It parses and validates profiles and arguments and returns the
deterministic Asia/Kolkata 20:00 preview without invoking Windows Task
Scheduler or requiring the CI host itself to use the production timezone.

Real registration remains Windows-only. Before any Task Scheduler cmdlet runs,
the script verifies Windows and requires the host timezone to be either
`Asia/Kolkata` or `India Standard Time`. Thus portable DryRun does not weaken
production registration safety.

GitHub Actions keeps separate Ubuntu backend and frontend jobs. Backend setup
verifies `pwsh`, then runs Ruff, Pyright, and pytest. Scheduler tests resolve
PowerShell Core on non-Windows and pass the active Python interpreter instead
of assuming a Windows virtualenv path. Frontend retains `npm ci`, lint,
typecheck, and build.

## Startup and diagnostics

1. Configure the named runtime environment variables in the local secret
   store; never place their values in Git or command-line previews.
2. Upgrade the production database to `20261005_0024`.
3. Run the V6 doctor/preflight and this read-only health command.
4. Confirm raw roots, model family, policy bindings, and scheduler DryRun.
5. Register the single task only on the correctly configured Windows host.
6. Inspect due retries, expired claims, and dead-letter counts through health;
   do not mutate or replay them from this command.

A real production health smoke requires the production database, raw roots,
model-family identity, and (for delivery readiness) Telegram credential
presence. It never sends Telegram. After a manual Production P push, acceptance
also requires both backend and frontend GitHub Actions jobs to succeed for the
exact pushed SHA.
