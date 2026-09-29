# Business-event materiality and recency feature primitives

Phase 6C-C is a non-persisted deterministic feature layer for one already
PIT-selected `BusinessEvent`. It converts explicit Phase 6C-B observations into
auditable ratios and exposes raw event age. It does not score, rank, aggregate,
or interpret events.

## Two distinct time boundaries

Materiality and recency deliberately use different cutoffs:

- materiality selects financial evidence at `event.source_available_at`;
- recency measures `requested as_of - event.source_available_at`.

An event published in March therefore uses only revenue known in March even if
the feature is requested after a June filing or restatement. Later accounting
information never backfills or reinterprets the v1 materiality of an older
event. Retrieval, extraction, event derivation, and quantitative derivation
timestamps are operational and do not affect either cutoff.

The caller supplies an explicit financial provider dataset and filing scope.
There is no provider or scope fallback, and event/market evidence cannot choose
the accounting source. Revenue is the latest valid `ttm_v1` revenue window by
economic `period_end` at the event-time knowledge cutoff. Tied latest endpoints
fail closed as `ambiguous_latest_event_time_ttm_revenue`; missing and
non-positive denominators remain explicitly unavailable.

## Versions and output

The bundle identity is `business_event_feature_bundle_v1`. Feature identities
are:

- `business_event_age_days_v1`
- `order_value_to_ttm_revenue_v1`
- `capex_value_to_ttm_revenue_v1`
- `capacity_change_ratio_v1`
- `acquisition_consideration_to_ttm_revenue_v1`
- `acquisition_stake_fraction_feature_v1`

Repeated quantitative observations are resolved under
`quantitative_observation_resolution_v1`. The immutable in-memory bundle and
feature values are not persisted.

`event_age_days` is the exact elapsed timedelta expressed as `Decimal` days,
including seconds and microseconds. It is not bucketed, clipped, decayed, or
scored. Commercial-commencement and regulatory-approval events expose only
this age primitive in v1.

Order, capex, and acquisition-consideration ratios divide one resolved INR
`currency_major` observation by event-time PIT TTM revenue. USD/EUR observations
remain unavailable because v1 has no PIT FX input. Market cap, enterprise value,
share price, and current FX are never used.

The acquisition stake feature carries one resolved explicit fraction without
transformation. It must be greater than zero and at most one.

## Observation resolution

Phase 6C-B facts remain observations, not totals. Equivalent repeated facts
resolve once by normalized semantics while retaining every supporting fact:

- money: currency + normalized unit + normalized value;
- capacity/fraction: normalized unit + normalized value;
- date: exact date.

Thus `₹250 crore` and `INR 2,500 million` resolve to one INR 2.5 billion value
and are used once, not summed. Different semantic values for the same fact code
produce `conflicting_quantitative_observations`; v1 never chooses, averages, or
sums them.

An absent quantitative derivation yields `quantitative_derivation_unavailable`.
A persisted empty derivation yields the relevant `missing_*` warning. These
states remain distinct.

## Capacity change

Capacity uses only explicit Phase 6C-B observations. V1 supports two ratio
constructions:

1. `capacity_after / capacity_before - 1`;
2. `additional_capacity / capacity_before`.

The baseline must be positive and normalized units must share the same
dimension. Before/after must represent expansion. If both constructions are
available, their exact Decimal results must agree; disagreement produces
`conflicting_capacity_change_calculations`. Absolute capacity is not normalized
against company size, and no before, after, or additional source fact is
inferred through arithmetic.

## Lineage and boundaries

Feature evidence retains the full PIT BusinessEvent, resolved observation and
all underlying quantitative fact views. Revenue ratios also retain the selected
TTM object and its quarter, financial fact, SourceRecord, and archive lineage.
Feature `available_at` is calculated from actual participating evidence.

Phase 6C-C performs no cross-event aggregation, cross-announcement semantic
deduplication, best/newest-event selection, direction, sentiment, confidence,
AI/LLM work, persistence, policy lookup, score curve, recency decay, or snapshot
change. Business Catalyst and Low Market Attention remain absent from
V1/V2/V3, maximum persisted coverage remains 0.25/0.60/0.75, and `final_score`
remains null.

Phase 4D-H consumes these bundles without recomputation. Its pure scorer
validates the event-time materiality cutoff and exact upstream versions,
multiplies approved event strength by a configurable recency score, and uses a
maximum-event aggregation to avoid duplicate-announcement inflation. Only
order awards, capacity expansions, commercial commencement, and regulatory
approval are scoreable in v1; capex and acquisition evidence remains neutral.
The scorer performs no provider selection, financial joins, persistence, or
top-level weighting. See
[`business-catalyst-scoring.md`](business-catalyst-scoring.md).
