# Research queue and company research frontend

Phase 5C is the first product-facing UI over the accepted Phase 5A and 5B
read contracts. It is a server-rendered, read-only research workstation: it
displays immutable `score_snapshot_v5` values and never calculates, rescales,
or refreshes a score from source data.

## Explicit research state

`/opportunities` requires the user to enter an exact `model_family`. There is
no default, active-model lookup, or hardcoded family. Queue status,
configuration checksum, persisted-field sort, limit, and offset remain URL
state. The table preserves null scores, valid zero values, partial coverage,
and missing component codes. Its backend sorts are direct views of persisted
fields, not a recommendation or ranking formula.

Each row links to `/companies/{company_id}` with the exact `model_family`,
`security_id`, and `scoring_configuration_id`. The company page discovers and
shows every returned research context instead of selecting the highest score
or an arbitrary security/configuration. Both context IDs are required before
history is loaded. `snapshot_id` selects an immutable historical snapshot;
the UI verifies that it belongs to the current company/security/configuration
before rendering it.

History pagination uses `history_limit` and `history_offset`. It retains the
backend's semantic ordering by cutoff, fiscal endpoint, and fingerprint;
`created_at` does not select the latest snapshot. The latest-change panel
shows exact persisted-value differences without calling them favourable or
unfavourable. Missing values remain null, while zero remains available.

## Exact display and evidence

All API Decimal fields stay canonical strings from transport through render.
The frontend does not call `Number`, `parseFloat`, rounding, fixed-point, or
locale-number formatting on scores, confidence, coverage, contributions, or
deltas. Persisted ISO timestamps are shown explicitly and are not converted
into current-time freshness labels.

The company dossier renders persisted component details and explanations.
Evidence manifests appear in bounded disclosures exactly as returned by the
read API. Its audit summary lists all 14 stored evidence categories and their
counts. `/opportunities/{snapshot_id}/audit/{category}` pages one category in
persisted order and renders its heterogeneous semantic dictionaries without
live joins, source retrieval, deduplication, or replacement.

## Interaction and boundaries

The implementation uses Next.js server components, GET forms, URL query
state, semantic tables, native `details`, Tailwind, and the existing icon set.
It adds no query, table, chart, or state-management dependency. Tables scroll
horizontally on narrow screens; the company dossier becomes linear. Forms are
labelled, table headers have scope, null and status states have text, focus
remains visible, and route-specific loading/error/empty states preserve the
research context.

Phase 5C adds no writes, migration, frontend scoring, current-time policy,
current-status eligibility rewrite, live evidence access, recommendation,
alert, backtest, watchlist/notes persistence, provider work, or AI. Richer
visualization and personal-workspace features remain future review items.
