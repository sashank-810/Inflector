# Production launch gate

For the exact fail-closed Linux commands, immutable release installer, host
preflight, and operator-attestation gate, follow
[`production-activation-runbook.md`](production-activation-runbook.md). The
gate does not enable the recurring timer or perform the Telegram smoke.
The `run_inflector_operator` shell helper referenced below is defined by that
runbook and does not put the database URL in a process argument vector.

This runbook launches only the accepted V5 champion. It does not complete
historical Production T, activate a challenger, tune a model, or change any
accepted scoring or evaluation policy.

## Authority and current scientific boundary

The launch source must be the reviewed commit, with a clean worktree and index.
The database migration head is `20261009_0028`. The accepted V5 scoring checksum
is `702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569`,
and the Production U checksum is
`7094654224d0e305ae46de8babfec4c0c5368936605b807eeca0468a15ec1691`.

V5 may be operationally live while historical model validation remains
`data_blocked`. A challenger remains blocked until the reviewed official NSE
session-calendar acquisition/parser enables an authoritative S1 -> S2 -> T
chain. Never substitute fixtures, current-survivor membership, or an unofficial
calendar.

## Environment contract

Values are supplied through the deployment platform's secret/configuration
store. Readiness reports presence only and must never print values.

| Variable | Class | Purpose |
|---|---|---|
| `INFLECTOR_PRODUCTION_DATABASE_URL` | required | PostgreSQL URL used by production operations and release commands |
| `INFLECTOR_PRODUCTION_RAW_ROOT` | required | persistent, writable, non-ephemeral NSE raw archive |
| `INFLECTOR_NSE_LICENSE_CLASS` | required | explicit NSE source-terms classification |
| `INFLECTOR_MODEL_FAMILY` | required | accepted live model family, currently `inflector_v1` |
| `INFLECTOR_RESEARCH_PROFILE` | required | accepted Research V4 asset path |
| `INFLECTOR_OPERATIONS_PROFILE` | required | accepted Operations V6 asset path |
| `INFLECTOR_SYMBOLS_FILE` | required | bounded operator-reviewed production symbol file |
| `INFLECTOR_TELEGRAM_BOT_TOKEN` | required for delivery | Telegram secret; presence only in diagnostics |
| `INFLECTOR_TELEGRAM_CHAT_ID` | required for delivery | intended Telegram destination; presence only in diagnostics |
| `INFLECTOR_GDELT_RAW_ROOT` | required when GDELT is enabled | separate persistent GDELT raw archive |
| `INFLECTOR_GDELT_LICENSE_CLASS` | required when GDELT is enabled | explicit GDELT source classification |
| `INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE` | optional, operator-only | authorizes only the dedicated one-delivery launch-smoke CLI when exactly `1`; normal O delivery does not read it |
| `DATABASE_URL` | development/API deployment | Alembic and FastAPI database URL |
| `RAW_ARCHIVE_ROOT` | development | local ingestion archive default |
| `CORS_ORIGINS` | deployment | comma-separated permitted browser origins |
| `NEXT_PUBLIC_API_BASE_URL` | frontend build time | public FastAPI base URL; not a secret |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT` | development/Compose | local PostgreSQL service only |

The production service should map its managed PostgreSQL secret into both
`DATABASE_URL` (API/Alembic) and `INFLECTOR_PRODUCTION_DATABASE_URL`
(operations/release CLI) without logging either value.

## CI authority

GitHub Actions runs with read-only repository permissions. The backend job uses
Python 3.12, installs `.[dev]`, runs Ruff, Pyright, and pytest, verifies the
single Alembic head, and exercises upgrade, downgrade to `20261008_0027`, and
upgrade to head against PostgreSQL 16. The frontend job uses Node 22 and runs
`npm ci`, lint, typecheck, and the production build. CI never receives
production credentials and performs no deployment. It also fails on high or
critical advisories in the production dependency tree; development-only audit
findings remain separately reviewable and are not shipped in the runtime build.

Authoritative local equivalents are:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\pyright.exe
.\.venv\Scripts\python.exe -m pytest
Set-Location apps\web
npm ci
npm audit --omit=dev --audit-level=high
npm run lint
npm run typecheck
npm run build
```

## Database, backup, and migration

Production uses PostgreSQL. Confirm the database identity, expected schema,
UTC-capable clock, connectivity, and a single Alembic head before proceeding:

```powershell
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic current
```

Before changing an existing production database, an operator creates an
encrypted off-host provider snapshot or logical backup. The following commands
are examples; substitute operator-controlled paths and never put a password in
the command line or report:

```powershell
pg_dump --format=custom --file <backup-path> --dbname $env:INFLECTOR_PRODUCTION_DATABASE_URL
Get-FileHash -Algorithm SHA256 -LiteralPath <backup-path>
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Record the database identifier, UTC timestamp, source migration, backup size,
SHA-256, retention location, and restore owner. Test restore into a separate
database:

```powershell
createdb <restore-test-database>
pg_restore --clean --if-exists --no-owner --dbname <restore-test-database> <backup-path>
```

Do not test destructive downgrade on production. For a migration emergency,
stop writers and restore the verified pre-migration backup. Application rollback
uses the prior reviewed artifact only when its schema compatibility is known.
Future challenger rollback uses Production U's immutable rollback operation;
do not fabricate a challenger for the initial launch.

## Storage gate

Both configured archive roots must exist on persistent storage, survive service
restart/redeployment, be readable and writable by the worker identity, and have
operator-monitored capacity. Container-local ephemeral paths are not accepted.
NSE and GDELT archives remain separate. Never delete older raw evidence during
launch.

## Champion bootstrap and release readiness

On a fresh database, do not bootstrap U before the legitimate Research V4 scoring
configuration exists. Follow `docs/production-linux-deployment.md`: acquire the
real current provider/dataset metadata through accepted ingestion commands, run
`research_cli init-model`, and only then bootstrap the accepted champion. The
portable V5 checksum and database-bound configuration checksum are distinct,
independently validated identities. The bootstrap and readiness commands are
idempotent, but run them against production only through an explicit operator
session:

Use `run_inflector_operator bootstrap-champion --activated-at
<timezone-aware-operator-time>` from the controlled activation runbook. The
root-only environment is loaded by systemd and the database URL never enters a
process argument vector.

Then run `model-release-readiness` with the accepted U/T/S2/S1 policy paths,
repository root, observation time, raw roots, and model family. The launchable
champion state is:

- `operational_status = live_operationally_ready`
- `active_subject = champion`
- active checksum equals the accepted V5 checksum
- `historical_model_validation = data_blocked`
- no release-integrity blocker

Verify the singleton pointer has `pointer_name = production`, subject
`champion`, and a positive pointer version. Do not create a T run.

## Controlled one-cycle smoke

Use an operator-reviewed file containing one to three safe real symbols. First
run Operations V6 doctor and scheduler registration with `-DryRun`. Confirm the
host timezone is Asia/Kolkata, the preview has one-instance execution and no
secret in its command line, and only one scheduler owns the job.

Run one explicit `run-cycle-auto` with recurring scheduling still disabled.
Capture only non-secret identifiers and counts:

- operational run ID and knowledge cutoff;
- release cycle binding ID/key, deployment ID/key, and release candidate ID/key;
- subject `champion`, V5 checksum, and U checksum;
- V5 snapshot and release-production-score counts;
- opportunity discovery/change run IDs and counts;
- notification projection/outbox counts and timestamps.

Verify the release binding is reused on retry, the release production score
equals the immutable V5 final score, discovery consumes that score, and change
and notification provenance retain the same cycle binding. A shadow row is not
valid input to any production opportunity or notification stage.

Keep Telegram credentials absent for the first projection-only pass. This must
produce `transport_unavailable` without a network attempt and leave eligible
backlog pending. Inspect the outbox and delivery rows, then select exactly one
operator-reviewed delivery UUID. Keep the recurring scheduler disabled and
confirm the intended destination out of band.

The real one-message smoke is a separate, explicit operator action. Supply the
credentials through the secret store, set
`INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE=1` only for this invocation, and run:

```bash
run_inflector_operator telegram-smoke --delivery-id '<reviewed-delivery-uuid>'
```

The command validates that exact existing N/O identity, claims only that row,
and uses the accepted O renderer, Telegram transport, attempt history, retry,
and dead-letter lifecycle. It starts at most one network request; all other due
deliveries remain untouched. Verify the persisted result and provider message
ID, then run the same command again and confirm it returns
`already_delivered` without resending. Finally unset
`INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE` and the credentials as appropriate.
Codex does not perform this send.

The smoke flag is not a global delivery switch. During later operator-approved
production enablement, normal scheduled Production O delivery continues to be
authorized by its configured Telegram credentials and accepted policy without
reading the launch-smoke flag.

## API, scheduler, monitoring, and rollback gate

Start the production ASGI command supplied by the deployment platform using
`inflector_api.main:app`, not the reload development server. Confirm `/health`,
graceful stop, restart, and migration compatibility. The initial hosting shape
is intentionally simple: one API process group, one separately owned scheduler
process, managed PostgreSQL, persistent NSE/GDELT volumes, platform secret
storage, persistent logs, and an operator-controlled backup schedule. No queue
or orchestration platform is required.

The health/doctor output must expose database/migration state, latest operational
run, stage failures, stale leases, archive roots, model/release identity, outbox
backlog, retries, expired claims, and Telegram credential presence. The host or
platform separately monitors process uptime, scheduler heartbeat, disk/quota,
and log retention. Logs include run/release identifiers and timestamps, never
credentials or full environment dumps.

The final decision is `GO_FOR_CONTROLLED_PRODUCTION_LAUNCH` only when CI and all
real-environment gates above pass. Missing database, backup, persistent storage,
secrets, API/scheduler smoke, operator Telegram smoke, or real one-cycle evidence
is `NO_GO_ENVIRONMENT`; code/test failure is `NO_GO_CODE_OR_CI`; corrupted
release/evidence state is `NO_GO_INTEGRITY`.
