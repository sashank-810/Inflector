# Cross-domain partial score orchestration

## Scope

Phase 4D-G introduces `score_snapshot_v3` without changing the historical v1
or v2 paths. V3 may persist Financial Inflection, Business Quality, Cash-Flow
Quality, Balance Sheet, Valuation, and Market Structure in that order. Their
standard top-level weights sum to `0.75`. This is coverage, not a final score:
`final_score` and every component `final_contribution` remain `NULL`, and the
top level is never renormalized or multiplied by confidence.

Business Catalyst (20%) and Low Market Attention (5%) remain absent. V1 retains
maximum coverage `0.25`; v2 retains `0.60`; v3 reaches at most `0.75`.

## Context selection

Each immutable `CrossDomainContextCandidate` associates the four financial
component bundles and one Valuation bundle with exactly one financial provider
and filing scope. A context is selectable when at least one positive-weight
financial-context-dependent component is scoreable. Valuation therefore
participates in scoreability, but provider priority followed by filing-scope
priority remains authoritative. Score magnitude, component count, coverage,
and cheapness never select the context.

Market Structure is evaluated only after a financial context is selected. It
cannot make a financial candidate selectable and has no provider fallback or
benchmark inference. The caller supplies one explicit security, market
provider, corporate-action provider, benchmark provider/code, interval,
market-date bound, and basis date. When Valuation and Market Structure both
persist, their security, market provider, cutoff, interval, and economic market
endpoint must agree.

Canonical `Security.company_id` is validated before persistence without using
mutable security status. Migration 0010 adds nullable
`score_snapshots.selected_security_id`; v1/v2 rows and writers keep it null,
while v3 requires it.

## Status and coverage

V3 uses only:

- `ineligible` — eligibility failed;
- `implemented_components_unavailable` — eligible but no financial context is
  selectable;
- `partial_component_set` — a context is selected and at least one supported
  component is persisted.

Market Structure alone cannot produce `partial_component_set`. Missing or
unavailable components are not zero-filled. Standard examples are `0.60` for
the financial four, `0.65` when Market Structure is also present, `0.70` when
Valuation is also present, and `0.75` for all six.

## Audit, lineage, and fingerprint

Context audit separates `financial_context`, explicit `market_context`, and the
single `market_structure_attempt`. Financial candidate attempts contain only
configuration/evidence flags, scoreability, subfactor coverage, warnings, and
available top-level weight; non-selected score magnitudes are excluded.

Valuation and Market Structure explanation rows use
`valuation_subfactor_v1` and `market_structure_subfactor_v1`. Their manifests
retain raw and scoring values/units, identity or negation transform, normalized
score, configured/effective weights, contribution, availability, evidence
type, and curve version. Unavailable subfactors remain in component detail and
do not create explanations.

V3 recursively retains and deduplicates the evidence used by each component:
financial facts and source archives, raw market bars, benchmark bars,
corporate actions, and every participating algorithm version. Component
manifests are isolated; the snapshot manifest is their deterministic union.

The fingerprint includes v3 identity, company and security, model/configuration
checksum, cutoff and financial endpoint, eligibility and confidence audits,
financial attempts/selection, explicit market context, component coverage and
summaries, and the complete selected-component manifest. Generated database
IDs and timestamps are excluded. Exact reruns return the existing snapshot;
PIT financial, market, action, or benchmark corrections create a new
fingerprint only when selected evidence changes, while historical cutoffs
remain reproducible.

## Deliberate exclusions

Eligibility and confidence evaluators are unchanged. The Market Structure
close-times-volume proxy is not wired into eligibility. Confidence does not
gate or multiply any score. V3 does not activate a final score, implement
Business Catalyst or Low Market Attention, infer sector benchmarks, or alter
v1/v2 fingerprints.

## V4 extension

Phase 4D-I adds a separate `score_snapshot_v4` path; it does not change this V3
contract. V4 reuses the exact V3 financial-context selector, then evaluates
Market Structure and Business Catalyst only after selection. Neither can make
a financial context selectable. Business Catalyst candidates bind one explicit
event provider to one financial provider/scope and must preserve the same event
identity set across contexts.

V4 inserts Business Catalyst in canonical top-level order after Financial
Inflection. Maximum reference coverage becomes `0.95`, while Low Market
Attention remains missing and all final scores/contributions remain null. See
[`business-catalyst-snapshot-orchestration.md`](business-catalyst-snapshot-orchestration.md).
