# Business Catalyst cross-domain snapshot orchestration

## Immutable V4 contract

Phase 4D-I adds the separate `score_snapshot_v4` persistence path. It may
persist, in canonical top-level order, Financial Inflection, Business Catalyst,
Business Quality, Cash-Flow Quality, Balance Sheet, Valuation, and Market
Structure. Low Market Attention remains missing. With the reference weights,
maximum persisted coverage is `0.95`; this is not renormalized to one.

V4 always stores `final_score = NULL`, and every component stores
`final_contribution = NULL`. Business Catalyst is persisted as its exact pure
0-100 component score. For example, the accepted 30-day order event with
order/revenue `0.25` persists `68`, not its inactive weighted contribution
`13.6`. A valid zero score is available evidence and retains the full 0.20
coverage weight.

The statuses are exactly `ineligible`, `implemented_components_unavailable`,
and `partial_component_set`. Only a partial snapshot owns children. V4 requires
an explicit security and uses the existing generic snapshot/component/
explanation tables, so no migration is introduced.

## Context selection and anti-shopping rules

Financial context selection is the unchanged V3 provider-first and
scope-priority process. Financial Inflection, Business Quality, Cash-Flow
Quality, Balance Sheet, and Valuation can make a financial context selectable.
Market Structure and Business Catalyst are evaluated only after selection and
cannot make a context selectable. A strong catalyst under a preferred but
otherwise unscoreable financial context therefore cannot defeat a scoreable
fallback context.

Each `BusinessCatalystContextCandidate` binds one explicit event provider and a
non-empty immutable event-bundle set to one financial provider and filing
scope. Across supplied financial contexts, the event provider and canonical
event identity set must be identical; only denominator-dependent feature
values may differ. Candidate and event tuple order has no effect. Bundles must
match the snapshot company, cutoff, selected security when non-null, financial
provider, scope, and event provider. Company-level events with null security
remain valid.

Absence of a candidate means evidence is unavailable, never a zero catalyst.
If no V3-style financial context is selected, Business Catalyst is not
persisted even when its supplied evidence would score. The attempt audit
distinguishes missing context, missing policy, missing evidence, unscoreable
event sets, and zero-weight configuration.

## Persistence and explanations

A scoreable Business Catalyst child uses
`business_catalyst_component_v1`, exact component score, configured top-level
weight, internal coverage `1`, and no final contribution. Component detail
retains the explicit event/financial providers, scope, security, selected
event, max-event aggregation identity, component availability, and every event
score audit. Component availability is the newest source availability across
the complete supplied event set.

Exactly one `business_catalyst_selected_event_v1` explanation represents the
winning max event. Its factor code is `selected_business_catalyst_event`, its
raw/normalized/contribution values equal the component score, and its internal
weights are one. Its input availability is the selected event's source time,
which can be older than component availability. Non-winning events are not
additive explanations; they remain auditable in detail and lineage.

## Lineage and fingerprints

The V4-only manifest union preserves deterministic immutable identity and
knowledge-time lineage for financial facts, market and benchmark bars,
corporate actions, announcements, documents, acquired assets, text
extractions, BusinessEvents and evidence, quantitative derivations and facts,
and event-time TTM financial facts. It retains source hashes/archive pointers,
document byte/text hashes, offsets/page hashes, detection/evidence/fact/
derivation fingerprints, and explicit extractor and algorithm versions.

Operational times such as ingestion, retrieval, extraction, and derivation do
not affect V4 semantic fingerprints. Candidate and event ordering is
canonicalized. Exact reruns reuse the immutable snapshot; corrected PIT events
produce new lineage and a new fingerprint, while replaying the original
PIT-selected bundle reproduces the original snapshot.

This lineage and union logic is a new V4 path. V1, V2, and V3 manifest shapes,
component orders, statuses, context selection, fingerprints, coverage, and
null-final-score semantics are unchanged.

## Deliberate exclusions

V4 does not implement Low Market Attention, calculate an Opportunity Score,
apply the Business Catalyst 0.20 weight, sum events, select providers, recompute
event features, add AI, or create a migration. Final-score activation and Low
Market Attention require separate review.
