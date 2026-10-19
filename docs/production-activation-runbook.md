# Controlled production activation runbook

This is the executable operator sequence for the accepted single-host Linux
architecture. It does not provision infrastructure, create secrets, fabricate
data, complete Production T, or activate a challenger. Replace every bracketed
placeholder only with reviewed real-environment evidence.

## Operator-only prerequisites

Before running machine commands, the operator must provide and record:

- one reviewed full 40-character Git SHA with successful authoritative CI;
- an already-provisioned Ubuntu/Linux host, DNS, and HTTPS routing;
- external PostgreSQL 16+ with TLS and a tested backup/restore method;
- persistent volumes mounted at `/srv/inflector/archive/nse` and
  `/srv/inflector/archive/gdelt`;
- reviewed NSE/GDELT licence classifications and legitimate provider access;
- an operator-reviewed 1–3-security symbol file (never fixture symbols);
- a timezone-aware model effective-from selected under the accepted release
  process; and
- production secrets in the host secret store, never Git or shell arguments.

The authoritative historical NSE calendar remains unavailable. Therefore
`historical_model_validation=data_blocked` is expected and truthful. It does not
prevent operational launch of the accepted V5 champion.

## 1. Review source and bootstrap the host

From a clean reviewed checkout whose `HEAD` is the intended release:

```bash
export INFLECTOR_REVIEWED_SHA='<40-character-reviewed-sha>'
test "$(git rev-parse HEAD)" = "$INFLECTOR_REVIEWED_SHA"
test -z "$(git status --porcelain --untracked-files=all)"
sudo python3.12 scripts/inflector_linux_deploy.py bootstrap-host
```

The bootstrap is idempotent. It creates the non-login `inflector` identity and
only the documented `/opt`, `/srv`, and `/etc` paths. It never creates secrets
or broadly changes release ownership.

## 2. Install the exact immutable release

Set the reviewed public HTTPS API origin for the frontend build; this value is
public configuration, not a secret. The installer uses the local reviewed
checkout as its explicit trust source, verifies a clean exact SHA, creates a
Python 3.12 environment, installs the package, runs `npm ci` and the production
build, validates required assets, and only then atomically moves `current`.

```bash
sudo --preserve-env=INFLECTOR_REVIEWED_SHA,NEXT_PUBLIC_API_BASE_URL \
  python3.12 scripts/inflector_linux_deploy.py install-release \
  --sha "$INFLECTOR_REVIEWED_SHA" --source-dir "$PWD"
```

The release tree remains root-owned/read-only to the runtime identity. The sole
runtime-writable release exception is `apps/web/.next/cache`.

## 3. Install environment files and reverse proxy

Copy service-specific templates, fill them using the host secret store/editor,
then atomically install root-owned mode `0600` files:

```bash
sudo install -o root -g root -m 0600 deploy/env/inflector-api.env.example /etc/inflector/api.env
sudo install -o root -g root -m 0600 deploy/env/inflector-web.env.example /etc/inflector/web.env
sudo install -o root -g root -m 0600 deploy/env/inflector-cycle.env.example /etc/inflector/cycle.env
sudo install -o root -g root -m 0600 deploy/env/inflector-caddy.env.example /etc/inflector/caddy.env
```

Never echo these files. Web receives no database or Telegram secret; API
receives no Telegram secret. Keep Telegram credentials empty through the first
controlled cycle. Install `deploy/caddy/Caddyfile.example` only after providing
its reviewed host variables through `/etc/inflector/caddy.env`. The systemd
installer places the reviewed Caddy drop-in that loads that file. Only Caddy is
public: FastAPI stays on `127.0.0.1:8000` and Next.js on `127.0.0.1:3000`.

## 4. Back up and migrate PostgreSQL

Operator-only: create a managed snapshot or encrypted `pg_dump --format=custom`,
record timestamp/database/source migration/size/checksum, and restore it to a
separate verification database. Never run a production downgrade as a test.

This bridge reads `/etc/inflector/cycle.env` and invokes Alembic in process, so
the database URL never appears in a child argv:

```bash
cd /opt/inflector/current
sudo .venv/bin/python scripts/inflector_production_operator.py migration-heads
sudo .venv/bin/python scripts/inflector_production_operator.py migrate
sudo .venv/bin/python scripts/inflector_production_operator.py migration-current
```

Both reported heads must be `20261009_0028`.

## 5. Run accepted initialization as the cycle identity

Use a transient systemd service so systemd reads the root-only environment and
the command runs as `inflector`. This helper places no credential in argv:

```bash
run_inflector_operator() {
  sudo systemd-run --quiet --wait --collect \
    --uid=inflector --gid=inflector \
    --property=WorkingDirectory=/opt/inflector/current \
    --property=EnvironmentFile=/etc/inflector/cycle.env \
    /opt/inflector/current/.venv/bin/python \
    /opt/inflector/current/scripts/inflector_production_operator.py \
    --use-process-environment "$@"
}
```

Acquire legitimate current provider/dataset metadata using accepted arguments
from `production-linux-deployment.md`, but through the secret-safe bridge.
Examples (supply reviewed real dates):

```bash
run_inflector_operator nse ingest-universe \
  --raw-root /srv/inflector/archive/nse --license-class '<reviewed-nse-class>'
run_inflector_operator nse ingest-market-range \
  --raw-root /srv/inflector/archive/nse --license-class '<reviewed-nse-class>' \
  --from-date '<reviewed-start>' --to-date '<reviewed-end>'
run_inflector_operator nse ingest-delivery-range \
  --raw-root /srv/inflector/archive/nse --license-class '<reviewed-nse-class>' \
  --from-date '<reviewed-start>' --to-date '<reviewed-end>'
```

Run the documented financial, corporate-action, catalyst, and GDELT acquisition
commands the same way. They must create real provider/dataset identities; never
insert fixture rows. Then initialize the exact database-bound Research V4 model:

```bash
run_inflector_operator research init-model \
  --model-family inflector_v1 \
  --model-semantic-version '<reviewed-semantic-version>' \
  --git-sha "$INFLECTOR_REVIEWED_SHA" \
  --effective-from '<reviewed-timezone-aware-effective-from>' \
  --research-profile config/research/production_research_v4.json
```

## 6. Bootstrap and verify the accepted champion

```bash
run_inflector_operator bootstrap-champion \
  --activated-at '<reviewed-timezone-aware-activation-time>'
run_inflector_operator release-readiness
```

Require `active_subject=champion`, the accepted V5 checksum, and operational
readiness. `historical_model_validation=data_blocked` remains expected. Do not
create a T run or challenger.

## 7. Install services; keep the timer disabled

```bash
sudo /opt/inflector/current/.venv/bin/python \
  /opt/inflector/current/scripts/inflector_linux_deploy.py install-systemd
systemctl is-enabled inflector-cycle.timer && exit 1 || true
sudo systemctl enable --now inflector-api.service inflector-web.service
```

The installer validates reviewed units, reloads systemd, and never enables the
timer. Validate API, web, Caddy HTTPS, restart behavior, and graceful shutdown.

## 8. Preflight and one controlled Operations V6 cycle

```bash
sudo /opt/inflector/current/.venv/bin/python \
  /opt/inflector/current/scripts/inflector_linux_deploy.py preflight \
  --expected-sha "$INFLECTOR_REVIEWED_SHA"
sudo systemctl start inflector-cycle.service
sudo systemctl status --no-pager inflector-cycle.service
```

The 1–3-symbol file is operator-reviewed before this run. With Telegram
credentials absent, delivery must remain `transport_unavailable` with pending
durable backlog and zero network I/O. Verify one release binding is frozen and
reused, subject is champion, release score equals frozen V5 final score,
opportunity/change/notification facts share that release, no shadow/challenger
fact is used, and no T outcome is accessed.

## 9. Operator-only one-message Telegram smoke

After inspecting projection/outbox evidence, choose exactly one delivery UUID.
Populate credentials in `/etc/inflector/cycle.env`, set
`INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE=1`, and keep the timer disabled. Codex and
automation must not perform this send.

```bash
run_inflector_operator telegram-smoke --delivery-id '<reviewed-delivery-uuid>'
run_inflector_operator telegram-smoke --delivery-id '<same-reviewed-delivery-uuid>'
```

Require one persisted provider message ID, then `already_delivered` with no
resend. Immediately reset the smoke flag to `0`. Normal Production O delivery
later uses its accepted credentials/policy and does not depend on the smoke flag.

## 10. Final factual gate and timer enablement

Run preflight again and save its sanitized JSON. Copy
`deploy/launch-attestations.example.json` outside the release and fill every
field only from reviewed evidence; false/missing stays false:

```bash
sudo /opt/inflector/current/.venv/bin/python \
  /opt/inflector/current/scripts/inflector_linux_deploy.py preflight \
  --expected-sha "$INFLECTOR_REVIEWED_SHA" > /root/inflector-preflight.json
sudo /opt/inflector/current/.venv/bin/python \
  /opt/inflector/current/scripts/production_launch_gate.py \
  --expected-sha "$INFLECTOR_REVIEWED_SHA" \
  --preflight-report /root/inflector-preflight.json \
  --operator-attestations /root/inflector-launch-attestations.json
```

Only `GO_FOR_CONTROLLED_PRODUCTION_LAUNCH` authorizes the operator’s final step:

```bash
sudo systemctl enable --now inflector-cycle.timer
systemctl list-timers inflector-cycle.timer
```

The timer runs at 20:00 Asia/Kolkata with `Persistent=true`; the accepted DB
lease/idempotence is the second one-owner barrier.

## Rollback and recovery

- Application: verify schema compatibility, atomically repoint
  `/opt/inflector/current` to the prior reviewed release, then restart API/web.
- Database: restore the recorded pre-migration/provider snapshot into a separate
  database first; switch only under the provider's reviewed recovery procedure.
- Raw archives: restore volume snapshots without overwriting hash-conflicting
  immutable evidence.
- Model release: initial state is champion-only. Do not fabricate a challenger
  to test rollback; future challenger rollback uses accepted Production U.

Every operator-only claim remains an attestation. The tooling never backs up,
sends Telegram, enables the timer, or declares scientific T completion itself.
