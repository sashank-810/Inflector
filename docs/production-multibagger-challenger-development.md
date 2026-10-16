# Production S2: governed multibagger challenger development

Production S2 adds infrastructure for explicit, hypothesis-bound weight-only
challengers. It does not change or replace the accepted V5 champion. Real
challenger selection remains blocked until Production S1 can report a genuinely
authoritative historical evaluation chain; fixtures validate mechanics only.

## Immutable champion and narrow challenger class

The champion is `config/scoring/production_opportunity_v5.json` at checksum
`702fb4d4e934542091f09b832777498fcc59c920acd87645f31dab8fd7d61569`.
S2 never writes that asset, its scoring configuration, its snapshots, its
components, or a `ModelVersion`.

`counterfactual_v5_top_level_weight_recomposition_v1` may change only the eight
top-level weights, in this canonical order:

1. `balance_sheet`
2. `business_catalyst`
3. `business_quality`
4. `cash_flow_quality`
5. `financial_inflection`
6. `low_market_attention`
7. `market_structure`
8. `valuation`

Weights are exact non-negative Decimal strings, each no greater than one, and
must sum exactly to one. Candidate score is the exact sum of candidate weight
times the frozen persisted V5 component score. There is no confidence or
coverage multiplier, component recomputation, missing-component
renormalization, eligibility change, or rankability change.

## Predeclared objective and candidate governance

The primary objective is frozen before outcome access:

- outcome: `MB_3X_3Y`
- selection: `TOP_20`
- view: `end_to_end`
- metric: F1

All three Q contracts, all six R selection contracts, and both R views remain
reported as diagnostics. An experiment allows at most eight explicitly
registered candidates. Each candidate contains canonical weights, a factual
hypothesis, and structured development-only S1 evidence references. Each
reference resolves to an immutable row in the exact bound S1 error-analysis
run; its split, contracts, view, signature, and row fingerprint are verified.
The evidence checksum and normalized hypothesis are part of the candidate key.
S2 has no grid, random, Bayesian, gradient, genetic, or other automatic search
path.

Development may iterate. Once the candidate set is sealed, its ordered checksum
is immutable, and development evaluation cannot reopen it. The complete
persisted candidate definitions are revalidated before validation. Validation
is one-way: before the first validation outcome query, all sealed candidates'
validation score and rank rows are persisted and flushed. No candidate
definition can be added, changed, or removed after validation opens. Idempotent
reuse of score, evaluation, and metric rows requires exact immutable projection
equality.

## Leakage barriers

Candidate score rows contain only frozen prediction and snapshot identities,
component-state checksums, counterfactual scores, rankability, and ranks. They
contain no future label or return field. Outcome-bearing evaluation rows accept
only `development` or `validation`; a database check constraint rejects
`holdout`. Queries use the cohort IDs persisted by the S1 split. Holdout cutoff
metadata can be known, but holdout labels, confusion facts, returns, threshold
dates, peak multiples, and drawdowns are owned by Production T and are not read
in S2.

## Validation gate and freeze meaning

A validation candidate is eligible only when its `MB_3X_3Y` / `TOP_20` /
`end_to_end` F1 is strictly greater than the champion, precision and recall do
not decline, both secondary contract F1 values do not decline at `TOP_20`, and
median positive rank does not worsen. Rankability and coverage deltas must be
exactly zero. Missing metric support is a rejection, not a fabricated zero.

All sealed candidates are retained. If several pass, deterministic ordering is
F1, precision, recall, median positive rank, then candidate code. At most one
row may be frozen as `candidate_for_production_t`. This is not production
promotion and does not activate a model. If none passes, the valid terminal
state is `no_challenger_selected`.

A production experiment and freeze additionally require database-backed,
authoritative S1 readiness for one exact R evaluation, session calendar,
temporal split, error-analysis run, and champion scoring configuration. A
caller-provided status or evidence-class string cannot grant authority. A
fixture-backed experiment cannot create a production freeze. At present, the
reviewed official NSE session-calendar parser/source is unavailable, so a
runtime-equipped environment remains data-blocked. A local environment without
the production database is environment-blocked.

## Operator commands

The historical CLI exposes deterministic JSON commands for readiness,
experiment creation, explicit candidate registration, development evaluation,
candidate-set sealing, validation evaluation, summary/inspection, and a
readiness-gated freeze for T. There is intentionally no holdout evaluation
command in S2.

Migration `20261007_0026` adds append-only experiment, candidate, score,
evaluation, metric, and T-freeze tables. Operations V6 and all production
research, ranking, notification, delivery, Telegram, and scheduler semantics
remain unchanged.
