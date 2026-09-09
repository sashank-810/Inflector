# Point-in-time financial reads

## Purpose

The Phase 3A read layer answers one constrained question: for one explicitly
selected provider dataset, company, filing scope, metric, and `as_of` timestamp,
which immutable reported financial fact was knowable then? It does not derive
TTM, growth, margins, or any investment conclusion.

## Knowledge time is not economic time

`FiscalPeriod.period_end` describes when business activity occurred.
`FinancialFact.available_at` describes when that reported observation became
usable by a market participant. Eligibility is inclusive:

```text
financial_fact.available_at <= as_of
```

A Q2 FY2024 comparative included in a filing available at
`2025-11-01T12:00:00Z` is not visible at `2025-10-31`, despite the period ending
in 2024. It becomes visible exactly at the availability timestamp.

## Selection algorithm

For the economic identity:

```text
provider_dataset + company + fiscal_period + filing_scope + metric
```

the reader filters to accepted canonical facts whose `available_at` is no later
than `as_of`. It orders eligible immutable observations by:

1. `available_at` descending;
2. `coalesce(revision_at, available_at)` descending;
3. `ingested_at` descending only as a deterministic final tie-breaker;
4. fact UUID descending as a final stable tie-breaker.

`ingested_at` is never an availability substitute. A later restatement cannot
leak into an earlier query merely because it was ingested or revised later.

For example, FY2025 PAT reported as 100 crore at `2026-05-01` and restated to
92 crore at `2026-06-01` yields 100 crore at `2026-05-15` and 92 crore at
`2026-06-15`; both rows remain physically stored.

## Scope, provider, and periods

Every Phase 3A query requires `provider_dataset_id`, `filing_scope`, and
`as_of`. It never reconciles providers and never defaults standalone versus
consolidated facts. A provider A query cannot return provider B evidence.

Series retain stored fiscal-period identity (`period_kind`, start/end, fiscal
year, fiscal quarter, and YTD flag) and are sorted by period end, period start,
then period kind. Optional filters match these stored fields exactly; the reader
does not infer a calendar quarter or turn YTD values into quarters.

## Timestamp and provenance policy

Callers must provide a timezone-aware `as_of`; naive timestamps are rejected.
Aware timestamps are normalized to UTC. PostgreSQL is the production source of
UTC-aware timestamps. SQLite test storage loses offset metadata, so the reader
restores UTC on returned values only because ingestion accepts UTC timestamps.

Each selected result retains exact reported and normalized Decimal values,
filing metadata, fiscal-period semantics, and source-record/raw-payload lineage.
Only source records with `validation_status = accepted` are eligible. Quarantined
and `duplicate_economic` observations have no canonical fact in the series.

There is deliberately no `latest` method without `as_of`: such an API makes
look-ahead errors easy to introduce.
