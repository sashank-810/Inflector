# Project Inflection engineering rules

## Purpose and boundaries

Project Inflection is a single-user Indian-equity research system. It ranks
*research opportunities*, never recommendations, targets, or predictions. Keep
the product a modular monolith; do not introduce tenancy, billing, RBAC,
microservices, Kubernetes, or a distributed queue without an approved need.

## Non-negotiable data rules

- Preserve raw inputs and immutable source provenance. Never silently overwrite
  a fact, a source document, a score, or an AI interpretation.
- All time-dependent facts require `fiscal_period` where applicable,
  `reported_date`, `published_date`, `available_date`, and `revision_date`.
  The backtest default must filter on `available_date <= as_of_date`.
- A canonical company identity is independent of NSE/BSE symbol. Securities,
  listings, aliases, renames, mergers, delistings, and corporate actions are
  historical records.
- Financial features and scores are deterministic, tested Python calculations.
  LLM output is limited to unstructured-document interpretation and is stored
  with source, evidence, confidence, model metadata, and review state.
- Keep monetary values in reported units plus an explicit normalized INR value
  and scale. Never infer lakh/crore/million conversion without recording it.

## Architecture and code

- Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, PostgreSQL,
  DuckDB, Polars, NumPy. Use APScheduler in the worker until a real queue is
  justified.
- Use domain modules with typed interfaces. Keep HTTP, persistence, provider,
  feature, and scoring concerns separate. No hidden business logic in routes,
  ORM models, or UI components.
- Type hints are required. Prefer small functions, explicit errors, UTC-aware
  timestamps, `Decimal` for stored finance values, and UUID primary keys.
- Next.js uses strict TypeScript; avoid `any`. The default interface is dark,
  desktop-first, accessible, data-dense, and calm.

## Data-provider and security rules

- Providers implement stable ports; no core workflow may depend on a fragile or
  undocumented scraper. Respect licences, terms, rate limits, and attribution.
- Secrets belong only in environment variables or local secret stores. Do not
  commit API keys, tokens, passwords, production data, or generated DuckDB/
  Parquet files.
- Validate data at ingestion. Quarantine invalid rows with a reason; do not
  discard them invisibly. Log provider run IDs on every resulting record.

## Testing and delivery

- Add unit, integration, validation, API, backtest, and relevant frontend tests
  with every feature. Financial formula and point-in-time tests are mandatory
  for related changes.
- Before claiming a phase complete, run relevant tests, lint, type checks,
  migrations, Docker smoke checks, API checks, UI checks, and update docs.
- Preserve unrelated user changes. Use reversible edits and never reset or
  delete broadly. State constraints and unverified assumptions plainly.
