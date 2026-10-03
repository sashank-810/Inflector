# Inflector

Inflector is a personal Indian-equity research workstation. It identifies and
explains potential business and financial inflections for research; it is not a
stock-tip, brokerage, or prediction product.

The current product slice carries canonical company identity and immutable V5
Opportunity Score research from PostgreSQL through explicit FastAPI read
contracts into a dark, desktop-first Next.js research workstation. Scores
prioritize investigation; they are not recommendations or predictions.

## Architecture

- **PostgreSQL** is the operational source of truth.
- **SQLAlchemy + Alembic** own the Company → Security → ExchangeListing schema
  and migration history.
- **FastAPI** provides a versioned, explicit-schema read API.
- **Next.js App Router + TypeScript + Tailwind** renders server-fetched company
  identity, persisted research queues, exact-context dossiers, and bounded
  audit views. The small owned UI foundation uses Lucide icons.
- Docker Compose starts **PostgreSQL only**. FastAPI and Next.js run locally
  with hot reload during development.

See [architecture documentation](docs/architecture.md),
[data model](docs/data-model.md), and [product design](docs/product-design.md)
for the authoritative long-term design.

## Prerequisites

- Python 3.12+
- Node.js 22+ and npm
- Docker Desktop with Docker Compose

## Local development

### 1. Configure environment

From the repository root in PowerShell:

```powershell
Copy-Item .env.example .env
Copy-Item .env.example apps\web\.env.local
```

`DATABASE_URL` configures Alembic and FastAPI. `NEXT_PUBLIC_API_BASE_URL`
configures the Next.js-to-FastAPI boundary. The included values are local-only
defaults; change the database password before any shared or production use.

### 2. Start PostgreSQL

```powershell
docker compose up postgres
```

Leave this terminal running. PostgreSQL is exposed at `localhost:5432` for
local development and stores data in the `inflector_postgres_data` volume.

### 3. Install Python dependencies, migrate, and seed

In a second terminal from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
alembic upgrade head
python -m inflector_database.seed
```

The seed command is idempotent. It inserts three clearly fictional companies;
Aranya Engineering Limited has one security listed on both NSE and BSE.

### 4. Start FastAPI locally

In the same activated terminal:

```powershell
uvicorn inflector_api.main:app --app-dir apps/api --reload --port 8000
```

OpenAPI documentation is available at http://localhost:8000/docs.

### 5. Start Next.js locally

In a third terminal:

```powershell
Set-Location apps\web
npm install
npm run dev
```

Open http://localhost:3000. The Overview table is loaded from FastAPI. Select
a company to view its canonical identity, or enter an explicit model family at
`/opportunities` to inspect persisted V5 research contexts.

### Official NSE production ingestion

Use a separate migrated database and raw archive root. The production command
requires an explicit source-terms classification and exact trading date:

```powershell
.\scripts\ingest_nse_daily.ps1 -Date 2026-09-30
```

The runner reads `INFLECTOR_PRODUCTION_DATABASE_URL`,
`INFLECTOR_PRODUCTION_RAW_ROOT`, and `INFLECTOR_NSE_LICENSE_CLASS`. It ingests
the official NSE EQ master, CM UDiFF Final daily file, and all valid rows from
the daily index snapshot. See
[NSE production ingestion](docs/nse-production-ingestion.md) for live and
local-file commands, PIT limitations, source terms, and scheduling.

Current NSE Integrated Filing financial XBRLs can be bootstrapped separately
for one explicit symbol or a bounded symbols file:

```powershell
.\scripts\ingest_nse_financials.ps1 -Symbol ITC -MaxFilings 20
```

The financial path archives each exact XBRL independently, uses observed
retrieval time as availability, and applies only the documented exact taxonomy
mapping. Historical bootstrap does not reconstruct historical PIT knowledge.
Research V3 may additionally bind the explicit Production G primitive policy:
`RevenueFromOperations` can become a lineage-preserving normalized `revenue`,
while direct finance cost, depreciation/amortisation and current/non-current
borrowings remain distinct facts. Reported EBITDA and total debt remain
unavailable rather than derived. See
[NSE production financials](docs/nse-production-financials.md) and
[production financial primitives](docs/production-financial-primitives.md).

Research V4 adds explicit per-company latest-endpoint selection without
changing financial or scoring semantics. Use `run-current-auto` or Operations
V3 `run-cycle-auto`; manual FY/Q research remains supported. The resolver uses
only PIT-visible accepted fiscal metadata and never falls back to an older,
more complete period. See
[financial endpoint discovery](docs/production-financial-endpoint-discovery.md).

Production I completes the remaining source qualification without weakening
evidence standards. Official market-cap candidates do not yet provide an
approved archiveable daily atomic value compatible with the UDiFF bar, and
official analyst/meeting/registry sources do not provide a complete dated count
of active analysts covering each company. Both remain `NOT_APPROVED`; Research
V4 remains current, Operations V4 only adds monitoring orchestration, and
missing evidence remains missing. See
[evidence-gap qualification](docs/production-evidence-gap-qualification.md).

Production J adds bounded strict knowledge-time evaluation without changing
V5 research semantics. Research snapshots are frozen first; forward adjusted
price outcomes are built in a separate command and summarized descriptively:

```powershell
python -m inflector_data.backtest_cli build-dataset <explicit policy/profile/cutoff arguments>
python -m inflector_data.backtest_cli build-outcomes <explicit run/outcome-cutoff arguments>
python -m inflector_data.backtest_cli summarize <explicit run arguments>
```

Only actual persisted `available_at` is authoritative. Current downloaded
history is not backdated, partial V5 snapshots remain in the dataset, and cash
dividends are excluded from the declared adjusted-close price return. See
[historical PIT backtesting](docs/production-historical-pit-backtesting.md).

Production K freezes current opportunity-discovery runs from already persisted
V5 snapshots. The only headline merit key is the accepted `final_score`; stale,
partial, ineligible, ambiguous, mismatched, and unavailable states remain
explicit and unranked:

```powershell
python -m inflector_data.opportunity_cli build-ranking <explicit policy/profile/cutoff/symbol arguments>
python -m inflector_data.opportunity_cli summarize <explicit policy/run arguments>
```

Dense score ranks preserve equal-score ties, while symbol/security ordering is
display-only. No backtest outcomes, market-cap substitute, analyst estimate,
recommendation, portfolio, or alert affects discovery. See
[production opportunity discovery](docs/production-opportunity-discovery.md).

Production L compares two completed Production K runs without rerunning
research, scoring, or ranking. It persists exact score/rank, rankability,
freshness, eligibility, coverage, component, symbol, and compared-universe
transitions. Score deltas exist only when both sides were rankable; display
order is never treated as merit. Commands are
`python -m inflector_data.opportunity_change_cli compare ...` and
`python -m inflector_data.opportunity_change_cli summarize ...`. See
[production opportunity change detection](docs/production-opportunity-change-detection.md).

Production M adds Operations V4 to run the accepted current-research, K, and L
chain as one resumable daily cycle. The operational cutoff and symbol tuple are
shared with K; L selects only a prior compatible K run referenced by a
completed monitored operational stage, then freezes that baseline before
comparison. A first run with no baseline succeeds without fabricating a change
run. Existing leases, retries, stale recovery, missed-job handling, and the
single Windows scheduled task are reused. See
[production opportunity monitoring](docs/production-opportunity-monitoring.md).

Production N adds Operations V5 and a deterministic factual projection from
the exact monitored L run into an immutable transport-neutral pending outbox.
Eligibility is only the versioned intersection of persisted L change codes and
configured high-level triggers: there are no numeric thresholds, severity,
recommendations, or delivery. Low-level snapshot/confidence/component-internal
changes do not trigger alone, multiple matching codes remain one event, and
null values remain null. See
[production research notification outbox](docs/production-research-notification-outbox.md).

Production O adds Operations V6 and a separate reliable delivery lifecycle for
immutable N events. Telegram V1 uses deterministic internal identities,
transactional expiring claims, append-only attempts, bounded retries, and a
factual plain-text renderer. Bot token and chat ID remain runtime-only secrets;
missing credentials leave backlog pending without invalidating research. The
external guarantee is explicitly at-least-once because Telegram cannot bind an
application idempotency key across the provider-success/local-commit crash
window. See [production notification delivery](docs/production-notification-delivery.md).

Corporate actions and announcement/catalyst evidence can be ingested for an
explicit bounded date window:

```powershell
.\scripts\ingest_nse_catalysts.ps1 -FromDate 2026-10-01 -ToDate 2026-10-01
```

The action mapping is anchored and versioned; exact announcement and official
attachment bytes flow through the existing deterministic PDF text, business-
event, and quantitative-fact pipeline. Availability is observed retrieval time,
not an exchange/event date. See
[NSE production corporate filings](docs/nse-production-corporate-filings.md).

Current production research can be bootstrapped and persisted through the
unchanged V5 orchestrator with explicit dates, fiscal endpoint, model family,
profile, and bounded symbols file:

```powershell
.\scripts\run_inflector_current.ps1 -SymbolsFile .\symbols.txt `
  -MarketFromDate 2026-06-01 -MarketToDate 2026-09-30 `
  -FinancialFromDate 2025-03-01 -FinancialToDate 2026-09-30 `
  -CatalystFromDate 2026-09-01 -CatalystToDate 2026-09-30 `
  -FiscalYear 2025 -FiscalQuarter 4 `
  -KnowledgeCutoff 2026-10-01T23:59:59+05:30 `
  -ModelFamily inflector_v1 -ModelSemanticVersion 1.0.0 `
  -GitSha <accepted-git-sha> -EffectiveFrom 2026-10-01T00:00:00+05:30
```

The profile binds exact datasets and `NIFTY 50`; optional GDELT news counts use
exact-name `TimelineVolRaw` aggregate evidence. Analyst coverage is deliberately
unavailable. Missing source facts produce a valid partial V5 snapshot with a
null final score, never an inferred or renormalized score. See
[current production research](docs/production-current-research.md).

Repeated current-research execution is managed by the persistent operations
ledger and explicit cycle clock:

```powershell
python -m inflector_data.ops_cli doctor <explicit production arguments>
python -m inflector_data.ops_cli run-cycle <the same explicit arguments>
python -m inflector_data.ops_cli status --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL
```

The cycle lease prevents concurrent ownership, exact completed reruns are
reused, failed/stale runs require explicit resume, and partial V5 snapshots are
successful research results. Windows Task Scheduler wrappers support dry-run
registration without putting the database URL in the task command. See
[production operations](docs/production-operations.md).

## Web routes

| Route | Purpose |
|---|---|
| `/opportunities` | Explicit-model persisted V5 research queue |
| `/companies/{company_id}` | Canonical identity plus explicit security/configuration research dossier |
| `/opportunities/{snapshot_id}/audit/{category}` | Bounded persisted audit-category browser |

Research selection is URL-addressable. It requires an explicit model family
and, on the company dossier, an exact security and scoring configuration.

## API

| Endpoint | Description |
|---|---|
| `GET /health` | API and database connectivity status |
| `GET /api/v1/companies?limit=50&offset=0` | Paginated canonical company identity list |
| `GET /api/v1/companies/{company_id}` | Company with securities and dated exchange listings |
| `GET /api/v1/opportunity-scores?model_family=inflector_v1` | V5 research queue; `model_family` is required |
| `GET /api/v1/opportunity-scores/{snapshot_id}` | Immutable V5 Opportunity Score detail and explanations |
| `GET /api/v1/companies/{company_id}/research-contexts` | Explicit V5 security/configuration contexts for a model family |
| `GET /api/v1/companies/{company_id}/opportunity-score-history` | Exact-context semantic V5 history and display change |
| `GET /api/v1/opportunity-scores/{snapshot_id}/audit` | Bounded persisted audit category and algorithm index |
| `GET /api/v1/opportunity-scores/{snapshot_id}/audit/{category}` | Paginated stored lineage for one typed audit category |

## Validation

### Phase 2A synthetic ingestion

See [the ingestion guide](docs/data-ingestion.md) before using development CSV
fixtures. They require a dedicated synthetic database and raw archive root.

Backend commands, from the repository root after installing Python dev
dependencies:

```powershell
ruff check .
pyright
pytest
```

Frontend commands, from `apps/web` after installing npm dependencies:

```powershell
npm run lint
npm run typecheck
npm run build
```

`pytest` covers the repository identity graph, idempotent seed behavior,
health/API responses, clean 404 behavior, and an Alembic upgrade test.
GitHub Actions runs the same backend and frontend checks on push and pull
request.

## Project structure

```text
apps/api/                 FastAPI routes, schemas, services
apps/web/                 Next.js application shell and identity views
packages/core/            Shared domain constants and configuration
packages/database/        SQLAlchemy models, repository, session, seed data
migrations/               Alembic migration history
tests/                    Backend repository, API, and migration tests
docs/                     Architecture, research, and product design records
```

## Current limitations

The default identity seed remains fictional, and populated research views
require separately ingested and persisted V5 snapshots. The web UI is
read-only. Official NSE ingestion currently covers the listed EQ universe,
CM UDiFF Final daily OHLCV, daily index snapshots, and the controlled current
Integrated Filing Ind-AS financial XBRL mapping, corporate actions, bounded
announcement/document catalyst evidence, and official Full Bhavcopy daily
delivery observations. Financial-industry/legacy taxonomies remain deferred.
It does not yet cover BSE, approved market capitalization, analyst coverage,
authentication, alerts, strategy simulation, recommendations, charts, or
watchlist/notes persistence. Production scoring is not activated.

Delivery range ingestion is explicit and bounded:

`python -m inflector_data.nse_cli ingest-delivery-range --database-url ... --raw-root ... --license-class ... --from-date YYYY-MM-DD --to-date YYYY-MM-DD`

Use `production_research_v2.json` with `production_operations_v2.json` to enable
delivery evidence operationally. V1 profiles remain immutable. See
[`docs/production-market-evidence.md`](docs/production-market-evidence.md).
