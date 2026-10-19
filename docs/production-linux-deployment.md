# Production Linux deployment and cold-start activation

The exact repository-owned installation and activation command sequence is in
[`production-activation-runbook.md`](production-activation-runbook.md). This
document remains the architecture and cold-start rationale; use the activation
runbook for execution so credentials never enter process arguments.
The `run_inflector_operator` helper used below is defined there; systemd loads
the root-only environment and executes the accepted CLI as the `inflector` user.

This runbook deploys the accepted V5 champion. It does not complete Production T,
create a challenger, or change scoring semantics. All placeholders are resolved by
the operator; no production secret belongs in the repository or shell history.

## Host layout and identity

- Create the `inflector` system user/group without an interactive login.
- Install each reviewed commit under `/opt/inflector/releases/<full-git-sha>`.
- Make the release directory immutable to the runtime user after installation,
  except for the explicitly created Next.js runtime cache directory described
  below.
- Atomically point `/opt/inflector/current` at the reviewed release.
- Mount persistent storage at `/srv/inflector/archive` with distinct `nse` and
  `gdelt` children. The cycle worker owns archive writes; the frontend has no
  archive access.
- Install `/etc/inflector/api.env`, `/etc/inflector/web.env`, and
  `/etc/inflector/cycle.env` as `root:root` mode `0600`, using the matching
  `deploy/env/*.example` files. The aggregate
  `deploy/inflector-production.env.example` is an operator inventory only and
  is not loaded by systemd. The web service receives no database or Telegram
  credential, and the API receives no Telegram credential.
  systemd reads each service's declared file before dropping to the `inflector`
  runtime identity; the processes cannot read the other root-only files.

Create the Python environment and frontend build during deployment, never during
service startup:

```bash
cd /opt/inflector/releases/<full-git-sha>
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
cd apps/web
npm ci
test -n "$NEXT_PUBLIC_API_BASE_URL"
NEXT_PUBLIC_API_BASE_URL="$NEXT_PUBLIC_API_BASE_URL" npm run build
install -d -o inflector -g inflector \
  /opt/inflector/releases/<full-git-sha>/apps/web/.next/cache
```

`NEXT_PUBLIC_API_BASE_URL` must be the operator-reviewed public HTTPS API origin
at build time. Next.js embeds this `NEXT_PUBLIC_*` value in the browser bundle;
putting it only in `/etc/inflector/web.env` after the build cannot change that
bundle. The `.next/cache` directory above is the sole runtime-writable exception
inside the otherwise immutable release. Do not broadly change ownership of the
release tree.

Install the reviewed systemd units from `deploy/systemd/`, then run
`systemd-analyze verify` before enabling anything. Install the provider-neutral
Caddy example from `deploy/caddy/Caddyfile.example` only after setting the two
public host variables. Ports 8000 and 3000 stay bound to loopback; only the HTTPS
reverse proxy is internet-facing.

## Database backup and migration

Create and record a managed-provider snapshot or encrypted `pg_dump --format=custom`
backup before migration. Record the source database, timestamp, source Alembic
revision, object size, and checksum. Do not test downgrade on production.

```bash
.venv/bin/python -m alembic heads
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Both `heads` and `current` must report `20261009_0028`.

## Operator-required model effective time

`INFLECTOR_MODEL_EFFECTIVE_FROM` is an immutable, timezone-aware activation input.
The dates shown in older command examples are examples, not an accepted global
model date. Before initialization, the operator records the reviewed activation
instant in the release record and environment. The Linux wrapper refuses a naive
or missing value. It never substitutes the current time.

## Fresh-database provider and model initialization

Production U validates the portable V5 asset checksum separately from the live
Research V4 scoring configuration. Consequently, a fresh database must first
obtain the real provider/dataset metadata used by `bind_production_policy()`.
Use only the accepted acquisition commands and genuine current evidence. Never
insert provider rows or a fixture scoring configuration manually.

With the timer disabled and a reviewed 1–3-symbol file, run the accepted current
acquisitions needed by Research V4. The operator selects truthful current PIT
date bounds; the placeholders below are not inferred or backdated:

```bash
run_inflector_operator nse ingest-universe \
  --raw-root "$INFLECTOR_PRODUCTION_RAW_ROOT" \
  --license-class "$INFLECTOR_NSE_LICENSE_CLASS"

run_inflector_operator nse ingest-market-range \
  --raw-root "$INFLECTOR_PRODUCTION_RAW_ROOT" \
  --license-class "$INFLECTOR_NSE_LICENSE_CLASS" \
  --from-date <reviewed-current-lookback-start> --to-date <reviewed-current-date>

run_inflector_operator nse ingest-delivery-range \
  --raw-root "$INFLECTOR_PRODUCTION_RAW_ROOT" \
  --license-class "$INFLECTOR_NSE_LICENSE_CLASS" \
  --from-date <reviewed-current-lookback-start> --to-date <reviewed-current-date>

run_inflector_operator nse ingest-financials \
  --raw-root "$INFLECTOR_PRODUCTION_RAW_ROOT" \
  --license-class "$INFLECTOR_NSE_LICENSE_CLASS" \
  --symbols-file "$INFLECTOR_SYMBOLS_FILE" --max-symbols 3 \
  --max-filings 20 \
  --financial-primitive-policy config/financial/production_financial_primitives_v1.json \
  --to-date <reviewed-current-date>

run_inflector_operator nse ingest-corporate-actions \
  --raw-root "$INFLECTOR_PRODUCTION_RAW_ROOT" \
  --license-class "$INFLECTOR_NSE_LICENSE_CLASS" \
  --from-date <reviewed-catalyst-window-start> --to-date <reviewed-current-date>

run_inflector_operator nse ingest-catalyst-evidence \
  --raw-root "$INFLECTOR_PRODUCTION_RAW_ROOT" \
  --license-class "$INFLECTOR_NSE_LICENSE_CLASS" \
  --from-date <reviewed-catalyst-window-start> --to-date <reviewed-current-date> \
  --max-announcements 100 --max-documents 100

run_inflector_operator research ingest-gdelt-news \
  --research-profile "$INFLECTOR_RESEARCH_PROFILE" \
  --symbols-file "$INFLECTOR_SYMBOLS_FILE" --max-symbols 3 \
  --knowledge-cutoff <reviewed-timezone-aware-cutoff> \
  --gdelt-raw-root "$INFLECTOR_GDELT_RAW_ROOT" \
  --gdelt-license-class "$INFLECTOR_GDELT_LICENSE_CLASS"
```

Now create the one legitimate database-bound Research V4 configuration:

```bash
run_inflector_operator research init-model \
  --model-family "$INFLECTOR_MODEL_FAMILY" \
  --model-semantic-version "$INFLECTOR_MODEL_SEMANTIC_VERSION" \
  --git-sha "$INFLECTOR_DEPLOYED_GIT_SHA" \
  --effective-from "$INFLECTOR_MODEL_EFFECTIVE_FROM" \
  --research-profile "$INFLECTOR_RESEARCH_PROFILE"
```

The configuration name/version must be `nse_current_research_v4`/`4`. Its checksum
is database-bound and may differ from the portable V5 checksum because it contains
the production provider-dataset UUIDs. Production U recomputes and verifies both.

## Champion release and first bound cycle

Only after successful model initialization, bootstrap the accepted champion:

```bash
run_inflector_operator bootstrap-champion \
  --activated-at <reviewed-timezone-aware-activation-time>
```

Run `model-release-readiness` using the accepted U/T/S2/S1 policy paths and both
raw roots. `active_subject=champion` with historical validation `data_blocked` is
the honest initial state. No T row is created.

Keep Telegram credentials absent and the timer disabled. Run `doctor-auto`, then
one explicit `run-cycle-auto` with the same immutable inputs used by the Linux
wrapper. Because the champion pointer already exists, `ProductionCycleService`
freezes the U release before executing any stage. Verify the current-research V5
snapshot, release production score, opportunity, change, and notification facts
all bind the same cycle release; the champion release score must equal the V5
snapshot final score. Retry/resume must reuse that binding.

The first production opportunity discovery must come from this bound cycle. A
pre-bootstrap or unbound cycle is not an accepted production monitoring result.

## Telegram, enablement, and rollback

The first cycle is projection-only: missing Telegram credentials must preserve a
pending `transport_unavailable` delivery without network I/O. After inspection,
the operator may select exactly one delivery UUID and use the dedicated launch
smoke command with `INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE=1`. Repeat the same UUID
to prove `already_delivered`, then unset the flag. The scheduler never invokes it.

Enable `inflector-api`, `inflector-web`, and finally `inflector-cycle.timer` only
after the controlled cycle and health/readiness checks pass.

Application rollback atomically repoints `/opt/inflector/current` to the prior
reviewed release and restarts services after schema compatibility review. Database
recovery restores the pre-migration snapshot into a separate verified database.
Raw archives recover from volume snapshots without overwriting conflicting hashes.
Future challenger rollback uses Production U; do not fabricate a challenger now.
