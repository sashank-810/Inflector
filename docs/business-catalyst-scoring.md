# Business Catalyst pure component scoring

Phase 4D-H adds a versioned, pure Business Catalyst scorer over explicit
Phase 6C-C feature bundles. It performs no reads, feature recomputation,
provider selection, persistence, or snapshot orchestration. The output is an
auditable 0–100 component value, not a recommendation or final score.

## Policy and versions

`BusinessCatalystScoringPolicy` is optional so historical policies remain
readable and checksum-stable. The reference development policy uses:

- a 365-day maximum event age;
- a negated-age recency curve with points `-365/0`, `-180/30`, `-90/60`,
  `-30/85`, and `0/100`;
- an order-value/TTM-revenue curve with points `0/0`, `0.02/20`, `0.05/40`,
  `0.10/60`, `0.25/80`, `0.50/90`, and `1.00/100`;
- a capacity-change curve with points `0/0`, `0.05/20`, `0.10/40`,
  `0.20/65`, `0.50/90`, and `1.00/100`;
- configurable base strengths of 80 for commercial commencement and 90 for
  regulatory approval; and
- `max_event_score_v1` aggregation.

The component, event-score, and aggregation identities are respectively
`business_catalyst_component_v1`, `business_catalyst_event_score_v1`, and
`max_event_score_v1`. Curve interpolation reuses the existing
`piecewise_linear_curve_v1` implementation. Decimal arithmetic is exact and
unrounded.

## Scoreable and neutral event types

Business Catalyst v1 scores exactly four neutral source event types:

1. `order_award`
2. `capacity_expansion`
3. `commercial_commencement`
4. `regulatory_approval`

Orders require an available `order_value_to_ttm_revenue` feature. Capacity
expansions require an available `capacity_change_ratio`. Recency alone cannot
score either event when strength evidence is unavailable. Commercial
commencement and regulatory approval use their configurable base strengths;
the scorer does not invent production impact, market size, probability, or
revenue.

`capex_announcement` and `acquisition_agreement` remain neutral and explicitly
unscoreable in v1. Large capex or transaction observations are not assumed to
be value-creating. They stay in the event audit with
`event_type_not_scoreable_in_business_catalyst_v1`; no direction or sentiment
field is introduced.

## Recency and event score

For an event inside the configured window, the scorer negates exact Decimal
`event_age_days` and evaluates the recency curve. Event strength is then
multiplied by recency:

`event score = strength score × recency score / 100`

This is not a weighted average. Exactly 365 days is inside the reference
window and has zero recency score. Any greater age is unavailable with
`outside_business_catalyst_window`; it is not clamped back into eligibility.
The calculation has no confidence multiplier and does not apply the top-level
Business Catalyst weight of 0.20.

At age 30, the reference results are: an order/revenue ratio of 0.25 scores
68; a capacity change of 0.50 scores 76.5; commercial commencement scores 68;
and regulatory approval scores 76.5.

## Event-set aggregation

Every supplied event is scored independently and retained. Only defined event
scores enter component selection, and the component is their maximum. Scores
are never summed or averaged, and event counts or repeated disclosures add no
bonus. This conservative rule prevents duplicate announcements from inflating
the component before cross-announcement economic deduplication exists.

Equal scores select lineage deterministically: newer source availability,
then the canonical Phase 6C-A event-type order, then the lexicographically
smaller event UUID. The numeric score is unchanged. Component `available_at`
is the latest source availability across the full supplied event set, even if
an older event wins. If no supplied event is scoreable, the component is
unavailable rather than zero.

## Context and lineage

The scorer requires one coherent company, announcement/event provider,
financial provider, filing scope, and UTC-normalized request cutoff. Company-
level events may have no security. A set may mix those with one security, but
two distinct non-null securities fail closed. Duplicate event IDs, mixed
contexts, unknown feature/ruleset versions, or a materiality cutoff different
from source availability also fail closed. The scorer never shops across
providers or selects an accounting context.

Each event result retains its complete `BusinessEventFeatureBundle`, allowing
audit traversal through the BusinessEvent and evidence, quantitative
derivation and facts, document citations, and event-time TTM financial
lineage. Inputs are validated, not recomputed or flattened.

## Deliberate boundaries

Phase 4D-H creates no database rows, migration, explanation template, snapshot
version, or final contribution. It performs no financial or market reads, AI,
sentiment, confidence adjustment, or cross-event semantic deduplication.
`score_snapshot_v3` therefore remains capped at 0.75 persisted coverage and
`final_score` remains null. Pure component scorers now cover 0.95 of standard
top-level weight; that is scorer availability, not persisted snapshot
coverage. Low Market Attention remains unimplemented.
