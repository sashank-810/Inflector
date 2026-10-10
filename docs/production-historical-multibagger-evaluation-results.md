# Production R — Historical Prediction Evaluation

Production R evaluates frozen historical model state; it does not train, tune,
or promote a model. It joins Production J's immutable `BacktestObservation` and
referenced V5 snapshot with Production Q's immutable multibagger labels.

Each cohort is bound to one completed Q historical-universe run and must exactly
recompose all of its resolved eligible securities from one or more J shards.
Missing, duplicate, extra, mixed-cutoff, or mixed-model shard input fails closed.
Q unresolved, ambiguous, and unsupported members are reported as coverage facts
but are not silently ranked.

R validates the actual persisted Q V1 contract projection (code, exact Decimal
threshold text, and calendar horizon) in its declared order. Before a cohort is
scoped to one cutoff, each supplied Q label run must exactly cover every frozen
observation in its source J run for all three contracts. A manifest cannot
self-certify calendar integrity; without independently persisted session
evidence, operational readiness remains `not_independently_verified`.

Pre-ranking Q validation uses a column-only immutable label-run metadata
projection, so the ORM's label relationship cannot preload outcome rows. Ranking,
all six selection flags, and their checksum are frozen and prediction rows are
flushed inside the evaluation transaction before any outcome-label query occurs.
A later label-integrity failure rolls the transaction back atomically.

Ranking is frozen before labels are read: V5 final score descending, then
historical symbol and canonical security ID ascending. Dense score rank is
descriptive; deterministic display order controls the fixed diagnostic budgets:
`TOP_5`, `TOP_10`, `TOP_20`, `TOP_1_PERCENT`, `TOP_5_PERCENT`, and
`TOP_10_PERCENT`. Percentage budgets use exact Decimal ceiling, minimum one
rankable member, and a cap at rankable count. These are evaluation contracts,
not production selection thresholds.

Only Q `positive` and `negative` labels enter TP/FP/FN/TN. `unmatured` and
`unavailable` remain explicit exclusions. R publishes both `end_to_end` (where
legitimately unrankable positives are FN and negatives TN) and `rankable_only`
(where unrankable facts are explicitly excluded). It persists factual FP/FN
cohorts but does not infer causes.

Metrics use exact Decimal calculations and separate total selected predictions
from selected mature-label rows. They include observation-weighted counts,
precision, recall, F1, prevalence, coverage, rank-of-positive diagnostics,
macro/cohort-weighted precision/recall/F1 where defined, cohort-hit reporting,
fixed J score buckets, and fixed calendar/cutoff/snapshot/rankability/component
availability groupings. Repeated security observations at nearby
cutoffs are temporally correlated; observation-weighted and cohort-level facts
are labeled accordingly and are not statistical-significance claims.

Every `rankable_only` in-view count is calculated after rankability filtering;
excluded unrankable rows are reported separately. Cohort hit rate uses only
cohorts with at least one mature in-view ground-truth row. Grouped diagnostics
remain separately keyed by outcome contract, selection contract, and evaluation
view before applying the fixed grouping dimensions, so contracts are never
mixed.

The `calendar_integrity_status` in every bundle is a real-data readiness gate.
If the historical benchmark/session archive cannot independently establish
coverage for the evaluated windows, real production evaluation is
`DATA-BLOCKED`; fixture verification does not certify real data.

Commands extend `backtest_cli` and emit deterministic JSON:

```text
python -m inflector_data.backtest_cli build-multibagger-evaluation ...
python -m inflector_data.backtest_cli summarize-multibagger-evaluation ...
python -m inflector_data.backtest_cli inspect-multibagger-errors ...
```

The cohort manifest carries IDs only. Every database object and policy binding
is revalidated before evaluation. Production R has no current-state lookup,
opportunity-discovery invocation, score recomputation, or label-driven ranking.
