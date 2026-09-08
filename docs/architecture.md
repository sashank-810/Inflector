# Architecture

## Decision

Use a modular monolith: one FastAPI application, one separately deployed
scheduler/worker process from the same Python codebase, one Next.js web
application, PostgreSQL as the immutable operational source of truth, and
DuckDB over versioned analytical extracts for research workloads. Docker
Compose is the only deployment orchestration required initially. Redis is not
part of the initial architecture.

This gives the personal product a simple operational shape while keeping
provider ingestion, deterministic research, and user-facing workflow cleanly
separated.

## Logical components

```mermaid
flowchart LR
  P[Licensed / official / CSV providers] --> I[Ingestion and validation]
  I --> PG[(PostgreSQL: facts, provenance, workflow)]
  I --> O[Raw documents / object storage]
  PG --> F[Deterministic feature engines]
  O --> C[Document intelligence]
  C --> PG
  F --> S[Versioned scoring and explanations]
  S --> PG
  PG --> X[Point-in-time snapshot exporter]
  X --> D[(DuckDB + Parquet research mart)]
  D --> B[Backtesting and winner research]
  PG --> A[FastAPI]
  A --> W[Next.js research dashboard]
```

### Applications

| Application | Responsibility | Must not own |
|---|---|---|
| `apps/api` | Auth gate, read/write API, OpenAPI contract, research workflow | calculations, provider-specific parsing |
| `apps/worker` | APScheduler, ingestion, validation, feature/score jobs, alerts | a separate business model |
| `apps/web` | Dashboard and research UI | finance calculations or provider access |
| `packages/core` | domain types, ports, formulas, scoring, backtesting, services | HTTP/UI/database framework glue |
| `packages/database` | ORM models, repositories, migrations, transactional access | scoring decisions |
| `packages/data` | provider adapters, normalization, raw archival, quality rules | UI/API behavior |

Phase 1 should create this physical layout, without creating empty feature
folders solely for appearance. New domains graduate into packages only once
they have a real interface or implementation.

## Data and time model

PostgreSQL holds canonical identities, raw-to-normalized observations,
provenance, user workflow, versioned feature/score output, and job history.
Financial values are append-only observations keyed by source revision,
period, statement scope, metric, and availability time. Prices are immutable
daily bars subject to explicit correction revisions. Documents live in an
S3-compatible object store in production and a mounted local path in
development; PostgreSQL stores hashes and metadata, not PDF blobs.

DuckDB is a disposable analytical projection, rebuilt from a named PostgreSQL
export to partitioned Parquet. It is never a second operational source of
truth. Each exported mart has a manifest containing source watermark, schema
version, model versions, and checksums.

All timestamps are UTC. A fact is usable in an `as_of` decision only when its
`available_at` is at or before that decision timestamp. Its `published_at` and
`reported_at` are retained independently. `ingested_at` permits a stricter
"as actually captured by this system" replay. Revisions append new records;
they never mutate prior knowledge. The default research/backtest policy is
public availability, with a selectable captured-data policy for operations.

## Module dependency graph

```mermaid
flowchart TD
  U[Universe and corporate actions] --> MD[Market data]
  U --> FD[Financial data]
  U --> ED[Events and documents]
  FD --> FE[Financial feature engine]
  MD --> MS[Market structure]
  MD --> VE[Valuation]
  FD --> EQ[Earnings quality]
  ED --> CI[Catalyst intelligence]
  FD --> RE[Risk engine]
  ED --> RE
  MD --> AT[Attention]
  FE --> SC[Scoring + explanations]
  EQ --> SC
  CI --> SC
  RE --> SC
  VE --> SC
  MS --> SC
  AT --> SC
  SC --> AL[Alerts]
  SC --> RP[Research pages]
  SC --> BT[Point-in-time backtests]
  U --> BT
  MD --> BT
  FD --> BT
  WL[Watchlist and notes] --> RP
```

Arrows denote permitted dependency direction. `core` exposes ports such as
`CompanyRepository`, `FactRepository`, `DataProvider`, `DocumentStore`, and
`Clock`; adapter packages implement them. Scoring reads feature snapshots and
reviewed event facts, not raw documents or provider clients.

## External interfaces

All provider adapters conform to a common envelope: `provider_id`, source URL
or object key, retrieval time, source checksum, published/available time,
licence tag, cursor, and validation results.

| Port | Minimum operations | Initial adapter |
|---|---|---|
| `UniverseProvider` | listings, ISINs, classifications, status, aliases | CSV/manual/mock |
| `MarketDataProvider` | daily bars, delivery, market cap, corrections | CSV/manual/mock |
| `FinancialsProvider` | filing headers, line items, restatements | CSV/manual/mock |
| `CorporateActionProvider` | split, bonus, dividend, merge, delisting | CSV/manual/mock |
| `DisclosureProvider` | announcements, filings, document references | CSV/manual/mock |
| `OwnershipProvider` | promoter, MF, FII/FPI, pledge holdings | CSV/manual/mock |
| `AttentionProvider` | coverage/news/search/volume proxies | CSV/manual/mock |
| `DocumentStore` | put/get immutable bytes by checksum | local/S3-compatible |
| `AIInterpreter` | structured extraction with citations | disabled-by-default/mock |
| `NotificationChannel` | create in-app notification | database adapter |

Official NSE/BSE/licensed adapters are deliberately deferred until their
contracts and permitted fields are verified. API adapters must expose cursors,
rate limits, retry classification, and deterministic idempotency keys.

## Deployment, auth, and observability

Compose services are `postgres`, `api`, `worker`, and `web`; an object-store
mount is sufficient locally. Development uses hot reload and a local `.env`.
Production uses a reverse proxy/TLS outside the app, managed PostgreSQL or
volume backups, a private network, and a single local-user login seeded from
environment credentials. The API stores a password hash and secure session or
short-lived signed token; there are no roles or registration flows.

Every job has correlation ID, provider run, start/finish/status, counters,
watermarks, exception details, and freshness measurements. JSON structured
logs go to stdout. Health endpoints cover app, database, migration revision,
and provider freshness—not merely process liveness.

Back up PostgreSQL daily with encrypted off-host retention, test restore
quarterly, and retain raw-source/document objects and Parquet manifests needed
to reproduce research. DuckDB files can be regenerated.

## Key architectural risks and mitigations

| Risk | Mitigation |
|---|---|
| Market-data licensing changes | Provider contracts, licence metadata, manual import fallback, no redistribution features |
| Look-ahead and revised facts | immutable revisions, `available_at` query guard, PIT tests and manifests |
| Symbol/ISIN churn | company-first identity, dated listings and aliases, corporate-action ledger |
| Unit and filing-format errors | unit normalization with source retention, validation/quarantine, human review queue |
| AI hallucination | AI is evidence-linked, confidence-scored, reviewable, and cannot write financial facts |
| Thin-liquidity false positives | liquidity/risk flags, universe eligibility rules, separate market-structure component |

## Licensing and compliance posture

Do not assume that public availability permits automated collection, storage,
analysis, or redistribution. NSE's data policy covers data usage and
redistribution and subjects subscribers to relevant agreements; it also offers
separate paid historical/EOD products. Review the current agreement before any
official adapter is enabled. BSE information products likewise publish datafeed
pricing. This personal tool must record each dataset's licence and prohibit
export/API redistribution by default. Corporate disclosures remain source
documents with immutable links and attribution; the system is research tooling,
not investment advice. See [NSE's data policy](https://www.nseindia.com/static/market-data/nse-data-policy), [NSE historical/EOD subscription](https://www.nseindia.com/static/market-data/eod-historical-data-subscription), [BSE information-products tariff](https://www.bseindia.com/downloads1/Information_Products_Pricing_Sheet.pdf), and [SEBI LODR regulations](https://www.sebi.gov.in/legal/regulations/may-2024/securities-and-exchange-board-of-india-listing-obligations-and-disclosure-requirements-regulations-2015-last-amended-on-may-17-2024-_80422.html).
