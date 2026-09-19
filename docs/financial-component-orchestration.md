# Phase 4D-D coherent financial-component orchestration

Phase 4D-D adds `score_snapshot_v2`, an explicitly versioned orchestration path
for the four approved financial components: Financial Inflection, Business
Quality, Cash-Flow Quality, and Balance Sheet. The existing
`score_snapshot_v1` path remains Financial-Inflection-only and retains its
historical fingerprint and status semantics. V1 and v2 snapshots may coexist
for the same company, policy, endpoint, and cutoff.

## One coherent financial context

A `FinancialComponentContextCandidate` groups all evidence for one exact
`(provider_dataset_id, filing_scope)` context. The orchestrator validates the
company, fiscal endpoint, knowledge cutoff, provider, and scope before invoking
the pure component scorers. Components are never borrowed across contexts.

Each configured supplied context is scored independently. A context becomes
selectable when it contains at least one scoreable implemented component whose
configured top-level weight is positive. Selection then uses the configured
provider-first lexicographic order:

1. provider dataset priority;
2. filing-scope priority within that provider.

Score magnitude, component count, and available top-level weight never change
this ordering. Missing coverage is disclosed rather than repaired by source
shopping. Context attempt audit data records policy/evidence availability,
subfactor coverage, warnings, scoreability, and available weight, but omits
non-selected score magnitudes and full non-selected lineage.

## Partial snapshot semantics

The selected context contributes every scoreable positive-weight implemented
component in this fixed order: Financial Inflection, Business Quality,
Cash-Flow Quality, Balance Sheet. Overall available and missing component codes
retain the established eight-component order.

Top-level coverage is the unrenormalized sum of configured weights for persisted
components. Under the current development weights, all four financial
components cover `0.25 + 0.15 + 0.10 + 0.10 = 0.60`. This is 60% policy
coverage, not a 60% score. Business Catalyst, Valuation, Market Structure, and
Low Market Attention remain unimplemented.

`final_score` and every component `final_contribution` remain `NULL`. Component
scores remain on their original 0–100 scales; top-level weights are audit
metadata only. Confidence is persisted independently and is not multiplied
into any component. A zero-top-level-weight component is neither selectable,
available, missing, persisted, nor explained.

Eligible attempts with no scoreable financial context use
`financial_components_unavailable`. Historical v1 rows retain
`financial_inflection_unavailable`. Ineligible attempts still persist
eligibility and confidence without invoking component scorers.

## Explanations and transforms

Each persisted component receives one structured explanation per available
subfactor. Ranks restart at one within each component, sort by contribution
descending, and use the component's canonical subfactor order to break ties.
Templates are component-specific; `direction` remains null and no investment
prose is generated.

Cash-Flow Quality and Balance Sheet explanations keep the accounting raw value
in the explanation columns. Their evidence manifest also contains a
`scoring_transform` object with normalized raw value/unit, scoring value/unit,
and transform code. For example, receivable days retain raw days while exposing
the negative-days signal; net debt retains INR while exposing net debt/EBITDA
and its negated scoring signal.

## Lineage, fingerprinting, and PIT behavior

One recursive lineage walker handles all four immutable Phase 3 evidence
graphs. Subfactor explanations retain only their supporting facts. Component
manifests contain the deterministic union for that component, and the snapshot
manifest contains selected component manifests in canonical order. Facts are
deduplicated by financial-fact UUID and retain source record, external record,
raw object, payload reference, metric, and `available_at` provenance.

The v2 fingerprint includes model/configuration identity, cutoff and endpoint,
eligibility, confidence, deterministic context attempts, selected context,
coverage, component detail, and complete selected lineage. It excludes full
non-selected evidence. An exact rerun resolves idempotently to the existing
snapshot; changed selected lineage creates a new immutable snapshot even when
the numerical scores match. PIT-visible restatements create a new fingerprint,
while a historical rerun at the earlier cutoff resolves to the preserved old
snapshot.

No schema migration or scoring-policy field is introduced in Phase 4D-D. No
final Opportunity Score, risk cap, future component scorer, calibration, or
frontend scoring surface is included.
