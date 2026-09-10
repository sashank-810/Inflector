# Implementation roadmap

## Phase 0 — architecture and design (complete)

- Inspected the clean `Inflector` repository and removed its setup-only prompt.
- Added repository engineering rules and the architecture, data model, scoring,
  module-dependency, provider-interface, licensing, and risk decisions.
- Chosen architecture: modular monolith, FastAPI + Next.js, PostgreSQL source
  of truth, DuckDB/Parquet research mart, APScheduler worker, no Redis yet.

## Phase 1 — foundation

1. Create only used modules: `apps/api`, `apps/web`, `packages/core`,
   `packages/database`, migrations, tests, and scripts.
2. Pin Python 3.12/Node toolchains; add `.env.example`, `.gitignore`, Makefile,
   PostgreSQL-only Compose service, health check, and persistent volume.
3. Implement settings, error contracts, and API/web shells. Authentication and
   a worker are explicitly deferred because this slice has no background work.
4. Add SQLAlchemy/Alembic with the initial Company/Security/Listing schema,
   migration test, core repositories, and idempotent fictional seed data.
5. Add Ruff, mypy/pyright choice, pytest, frontend ESLint/typecheck/test setup,
   GitHub Actions CI, and development/deployment/backup documentation.

**Exit gate:** clean PostgreSQL Compose boot; migration upgrade/downgrade; API
health and company-read smoke test; web shell; lint/type/test commands pass.

## Phase 2 — data layer

### Phase 2A — ingestion spine

Build provider-neutral universe and daily-market CSV/mock adapters, immutable
content-addressed raw archival, provenance tables, deterministic quarantine,
and append-only revised price observations. This subphase deliberately excludes
real providers, financial statements, provider health UI, and analytical marts.

### Phase 2B — financial reporting spine

Add provider-neutral CSV/mock financial filings, multiple reported fiscal
periods per filing, controlled reported-metric definitions, exact INR-scale
normalization, and append-only restatement facts. Keep TTM, ratios, PIT read
selection, and all feature calculations out of this subphase.

### Phase 2C — corporate actions and historical identity

Add provider-neutral action ingestion, exact split/bonus/rights/dividend terms,
dated symbol transitions, and explicit security replacement lineage. Do not
derive adjusted prices, returns, or factors until a later deterministic phase.

1. Define provider ports and shared ingestion envelope; build mock, CSV, and
   manual-import adapters first.
2. Implement raw archival, idempotent ingestion runs, normalization, company
   match/review workflow, corporate actions, price and financial fact storage.
3. Implement unit/currency normalization, quality rules, quarantine, provider
   health, freshness, and sample datasets. Enable official/licensed adapters
   only after licence and access review.

**Exit gate:** re-running a fixture is idempotent; bad rows quarantine with an
explanation; a filing revision and symbol rename survive normalization.

## Phase 3 — feature engine

### Phase 3A — point-in-time financial read layer

Implement explicit-provider, explicit-scope financial PIT selection before any
formula. Require UTC-aware `as_of`, use `available_at <= as_of`, preserve
reported fiscal-period semantics, and prove late filings/restatements cannot
leak future knowledge. No derived feature table, calculation, or provider
reconciliation belongs here.

### Phase 3B — period normalization and individual-quarter derivation

Derive only PIT-visible individual quarters from compatible additive monetary
duration facts: Q2 from H1/Q1, Q3 from 9M/H1, and Q4 from annual/9M. Preserve
reported-quarter precedence and complete component lineage. Do not persist
derived values or compute TTM, growth, or ratios.

### Phase 3C — TTM and base deterministic fundamentals

Add only properly selected TTM/base financial values from normalized quarters;
retain the same PIT lineage and missing-data safeguards.

### Phase 3D — growth, acceleration, and margin features

Add tested YoY/QoQ growth, persistence, acceleration, and margin calculations
over PIT-normalized periods.

### Phase 3E — quality, balance-sheet, and cash-flow features

Add deterministic capital-efficiency, leverage, working-capital, and cash-flow
quality features with source lineage.

**Exit gate:** formula and edge-case tests pass; late-report/restatement tests
prove no future facts appear at a historical cutoff.

## Phase 4 — scoring

1. Implement model/configuration versioning, eligibility and risk gates.
2. Build financial inflection, quality, cash, balance-sheet, valuation, market
structure, attention, and final scoring components.
3. Persist explanations and score-change comparisons; run sensitivity and
historical sanity checks before enabling rankings.

**Exit gate:** scores are deterministic, auditable, versioned, and explainable.

## Phase 5 — research dashboard

1. Build dark desktop-first dashboard, explorer filters, company research page,
   score explanations, inflection radar, and watchlist/notes.
2. Include loading, empty, error, freshness, confidence, source, and exclusion
   states from the start.

**Exit gate:** a user can trace any displayed score to inputs and evidence.

## Phase 6 — catalyst intelligence

1. Add announcements/documents ingestion and deterministic rule classification.
2. Add `AIInterpreter` behind a reviewed, evidence-required interface for
   catalyst/risk/guidance extraction; track management commitments.

**Exit gate:** unstructured outputs cannot alter financial facts or silently
enter a production score.

## Phase 7 — alerts

1. Detect meaningful score/catalyst/risk changes with deduplication and
   quiet-period rules.
2. Deliver in-app alerts and retain a channel interface for email/Telegram.

**Exit gate:** no duplicate alert on an idempotent rerun; every alert explains
what changed, why, evidence, confidence, and score movement.

## Phase 8 — point-in-time backtesting

1. Export named, immutable DuckDB/Parquet manifests and historical universe
   memberships.
2. Implement ranked portfolio construction, rebalance schedules, holding
   periods, costs, corporate actions, benchmarks, and results.
3. Report return distribution, hit rate, drawdown, volatility, Sharpe, Sortino,
   and decile comparison, with all assumptions visible.

**Exit gate:** fixture backtests demonstrate no look-ahead and preserve
delisted/survivorship-sensitive constituents where data permits.

## Phase 9 — historical research

Build winner/false-positive cohorts (3x/3-year, 5x/5-year definitions),
pre-outcome feature windows, and cohort comparison tools. Treat findings as
research, not automatic model weight changes.

## Phase 10 — hardening

Tune queries and exports, add data-quality/operations dashboards, restore-test
backups, review security, complete documentation, and run end-to-end tests.

## Decision log and assumptions

- Initial active universe is liquid listed common equities; ETFs, REITs, SME,
  preference shares and suspended issues are configured separately.
- Provider and consolidated/standalone preference are later research-policy
  choices; Phase 3A requires both provider dataset and filing scope explicitly.
- Daily bars are first; intraday is a later provider and storage extension.
- No real data is fabricated. Mock/CSV datasets unblock development.
- Exchange and vendor permissions must be approved before production feeds or
  any use outside personal research; Phase 2 can proceed without them.

## Immediate Phase 1 plan

Create the foundation vertically: Compose + settings + database migration + one
Company API read path + web dashboard shell + health/logging + CI tests. This
proves the delivery spine before broad schema or UI work.
