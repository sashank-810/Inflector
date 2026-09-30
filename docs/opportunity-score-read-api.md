# Opportunity Score read API

Phase 5A is the first product-facing, read-only boundary over immutable
`score_snapshot_v5` records. It never runs a scorer, selects a provider,
recalculates current state, or mutates a snapshot. V1 through V4 remain valid
internal audit records but are not resources in this API.

## Research queue

`GET /api/v1/opportunity-scores` requires an explicit, non-blank
`model_family`. Optional filters are the exact scoring-configuration checksum
and V5 snapshot status. The endpoint first selects the latest snapshot for each
exact `(company_id, selected_security_id, scoring_configuration_id)` context,
then filters, sorts, and paginates that result.

Latest means greatest semantic `knowledge_cutoff`, followed by ending fiscal
year, ending fiscal quarter, and the lexicographically smallest snapshot
fingerprint. Persistence `created_at`, score, confidence, and coverage never
select the context representative. Consequently, a newer partial snapshot
correctly replaces an older final snapshot in the queue; there is no score
shopping across configurations or securities.

Supported direct sorts are `knowledge_cutoff_desc`,
`opportunity_score_desc`, `confidence_desc`, `coverage_desc`, and
`company_name_asc`. They operate only after latest-context selection and do
not constitute a composite rank. Pagination uses `limit` 1–100 and a
non-negative `offset`; `total` is the post-selection, post-filter count before
pagination.

Each queue row contains canonical company and security identity, all dated
listings, immutable model/configuration identifiers, status, persisted score,
confidence and coverage, available/missing component codes, and bounded
component summaries. It deliberately omits component details and evidence
manifests.

## Snapshot detail

`GET /api/v1/opportunity-scores/{snapshot_id}` returns one integrity-checked
V5 snapshot. Unknown UUIDs and IDs belonging to older snapshot versions both
return `404` with `Opportunity score snapshot not found`.

Detail adds immutable model-version and scoring-configuration identity,
persisted eligibility and confidence audit values, context resolution,
component detail, and explanations. Explanations retain the persisted
evidence manifest; the API performs no live evidence lookup. The much larger
snapshot input manifest and fingerprint payload remain internal audit
contracts and are not exposed as top-level blobs in Phase 5A.

## Exact values and partial results

All Decimal fields are JSON strings in canonical fixed-point form, without
binary-float conversion or exponent notation. For example, `Decimal("1.00")`
is `"1"`, `Decimal("70.675")` is `"70.675"`, and an exact zero is `"0"`.
Null remains distinct from zero.

A partial V5 snapshot remains visible with its persisted missing-component
codes, context warnings, and exact coverage, but `final_score` and every
top-level final contribution remain null. The API never divides by available
coverage or otherwise manufactures a score.

## Integrity and historical semantics

The read repository uses a SQL window function for latest-per-context
selection, joined identity projections, and select-in eager loading for
components and listings. Detail separately loads bounded explanations. Every
selected row passes the existing `ScoreSnapshotRepository` V5 fingerprint,
component-order, coverage, algorithm, contribution, and final-score checks. A
single corrupt selected row fails the whole request closed with HTTP 500 and
the stable detail `Opportunity score snapshot integrity check failed`.

Current company/security/listing status is display metadata only. It does not
rewrite historical eligibility or gate a persisted snapshot. The endpoints
use no current-time default and issue no writes.

Phase 5A adds no frontend, migration, alert, backtest, provider, AI, ranking
formula, or investment recommendation.
