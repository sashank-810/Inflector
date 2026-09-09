# Inflector

Inflector is a personal Indian-equity research workstation. It identifies and
explains potential business and financial inflections for research; it is not a
stock-tip, brokerage, or prediction product.

Phase 1 proves the smallest vertical slice: fictional canonical company
identity data in PostgreSQL flows through FastAPI to a dark, desktop-first
Next.js research shell. It intentionally excludes market data, financials,
scores, providers, AI, alerts, authentication, charts, and backtesting.

## Architecture

- **PostgreSQL** is the operational source of truth.
- **SQLAlchemy + Alembic** own the Company → Security → ExchangeListing schema
  and migration history.
- **FastAPI** provides a versioned, explicit-schema read API.
- **Next.js App Router + TypeScript + Tailwind** renders server-fetched company
  identity views. The small owned UI foundation follows shadcn/ui conventions
  and uses Lucide icons.
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
a company to view its canonical identity, ISIN, securities, and listings.

## API

| Endpoint | Description |
|---|---|
| `GET /health` | API and database connectivity status |
| `GET /api/v1/companies?limit=50&offset=0` | Paginated canonical company identity list |
| `GET /api/v1/companies/{company_id}` | Company with securities and dated exchange listings |

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

All displayed companies are fictional seed identities. There is no market or
financial data, scoring, recommendation logic, live provider, authentication,
worker, charting, or watchlist persistence in Phase 1. The next approved scope
is Phase 2: provider abstraction, manual/CSV/mock ingestion, normalization,
and data-quality handling.
