# Opportunity Score activation

Phase 4D-K introduces the immutable `score_snapshot_v5` contract and the
`opportunity_score_weighted_sum_v1` aggregation. It orchestrates the eight
accepted deterministic component scorers without changing their curves,
weights, provider identities, or evidence semantics.

## Selection and execution order

V5 preserves the V3/V4 provider-first, scope-priority financial-context
selection. Only Financial Inflection, Business Quality, Cash-Flow Quality,
Balance Sheet, and Valuation can make a context selectable. Business Catalyst,
Market Structure, and Low Market Attention run only after that context exists
and cannot change it.

Low Market Attention accepts one explicit `AttentionFeatureBundle`, never a
provider candidate list. Its policy already binds the news and analyst
provider, scope, methodology, definition hash, and evidence level. The bundle
must match the orchestration company and cutoff. Company-level evidence may
retain a null security; any non-null security must match the snapshot.

## Immutable V5 contract

The canonical component order is Financial Inflection, Business Catalyst,
Business Quality, Cash-Flow Quality, Balance Sheet, Valuation, Market
Structure, and Low Market Attention.

V5 supports `ineligible`, `implemented_components_unavailable`,
`partial_component_set`, and `final_score_available`. Ineligible and
no-context snapshots contain no components. A partial snapshot contains at
least one positive-weight component but leaves every top-level final
contribution and `final_score` null.

The final score is activated only when every positive-weight component is
present and exact configured weight coverage is `1`. There is no top-level
renormalization. Zero is a defined component score: it contributes zero while
still satisfying its configured weight. Confidence remains separate and never
scales the score.

For a complete snapshot:

```text
component final contribution = component score × configured top-level weight
Opportunity Score = exact sum of all component final contributions
```

All arithmetic uses `Decimal` without rounding or clipping. The accepted
reference contributions are `16.6875`, `13.60`, `10.425`, `7.3875`, `6.70`,
`7.95`, `4.05`, and `3.875`, producing exactly `70.675`. If Low Market
Attention is missing, coverage is `0.95` and the final score remains null. If
Low Market Attention is a valid zero, the otherwise identical reference score
is `66.8` and remains final.

## Attention audit and lineage

A Low Market Attention component persists the exact pure score, internal
coverage, warnings, series identities, deterministic window, and scored and
unavailable subfactor audit. One `low_market_attention_subfactor_v1`
explanation is stored per scored count subfactor. Explanation contributions
are internal component contributions, not the top-level `0.05` contribution.

The V5-only manifest retains every present selected attention observation,
including partial, unknown, stale, or blocked observations, plus its raw
`SourceRecord` archive identity. Semantic fields include immutable IDs,
counts, coverage, series identity, economic date/window, source availability
and revision, hashes, and archive references. `retrieved_at`, source and
observation `ingested_at`, and snapshot `created_at` are excluded from the
semantic fingerprint.

V5 deterministically unions attention lineage with V4 financial, market,
corporate-action, announcement, document, event, and quantitative lineage. It
also records feature, component, curve, and weighted-sum algorithm identities.
Exact reruns return the existing immutable snapshot; PIT corrections create
new later snapshots without mutating historical results.

## Repository integrity and boundaries

Repository validation is version-gated. Only a V5 `final_score_available`
record may persist `final_score` and component `final_contribution` values. V1
through V4 retain their historical null-only behavior and fingerprint shapes.

V5 validates canonical unique codes and ordering, exact coverage, score and
unit ranges, exact component algorithms, the contribution equation, and the
exact contribution sum. No migration is needed because the existing nullable
columns already represent the new state.

Opportunity Score is a deterministic research-opportunity score, not an
empirical return claim, rank, target, recommendation, portfolio instruction,
or prediction. Phase 4D-K adds no AI, calibration, backtest, alert, or provider
selection.
