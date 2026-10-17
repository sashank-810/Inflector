# Production T: untouched temporal-holdout evaluation

Production T is the final evaluation-only consumer of the holdout cutoffs frozen
by Production S1. It evaluates the accepted V5 champion and, only when Production
S2 produced one authoritative `candidate_for_production_t` freeze, that exact
frozen challenger. It does not create or modify candidates, tune weights, train,
activate a model, or promote a result.

## Immutable inputs and readiness

The T policy binds the accepted Research V4, champion V5, J, Q, R, S1, and S2
checksums. Readiness is database-backed: it traverses the official historical
evidence chain through R, the verified S1 calendar and temporal split, the S1
error-analysis run, the accepted champion configuration, and the terminal S2
experiment. Caller-supplied readiness or authority strings are not inputs.
Every T run stores a constrained immutable `evidence_class`; it participates in
the deterministic run key. The fixture helper always writes `fixture_only`, even
when its supplied S2 graph is production-authoritative. `summary_json` is display
metadata and never grants authority. Production score freezing and holdout opening
rerun the complete database-backed readiness chain.

Two S2 terminal modes are accepted. `candidate_frozen_for_t` requires exactly
one revalidated authoritative freeze and immutable candidate definition.
`no_challenger_selected` requires completed one-way validation and no freeze;
T then evaluates the champion alone. Fixture-only experiments cannot create a
production T run.

## Exact holdout and one-way opening

T reconstructs development, validation, and holdout membership from persisted
S1 cutoff assignments, verifies the S1 split checksum, and requires the three
sets to be disjoint. The T target is exactly the persisted holdout set—never a
date-based reconstruction, subset, superset, or foreign R cohort.

Before the first outcome-bearing query, T validates and persists every champion
score/rank/selection row and every optional challenger row, including all six
fixed selection flags and boundary-tie state. It commits the frozen score checksum,
then commits `holdout_opened` and its immutable timestamp in a separate transaction.
Only after that durable commit does it query R confusion facts using an explicit
holdout-cohort filter. A post-open failure can safely resume from the exact frozen
state, but can never erase or rewrite the opening. Completed reuse revalidates all
scores, source R fingerprints, evaluation rows, metrics, and the decision derived
from those metrics; T evidence remains append-only and immutable.

Champion scores are the frozen R/V5 scores and are never recomputed with current
scoring code. A challenger score is the exact Decimal sum of its frozen S2
top-level weights times the same eight frozen V5 component scores. Challenger
rankability and coverage must equal the champion.

## Metrics and decision

For champion and optional challenger, T persists each combination of three Q
outcomes, six R selection contracts, and both R evaluation views. Metrics include
TP/FP/FN/TN, Decimal precision/recall/F1, selected and rankable counts, coverage,
positive count, median positive rank, and the selected-membership checksum.
Unmatured and unavailable facts remain excluded rather than becoming negatives.

The primary cell remains `MB_3X_3Y / TOP_20 / end_to_end / F1`. A challenger can
pass only when primary F1 strictly improves, primary precision and recall do not
decline, both secondary TOP_20 F1 guardrails do not decline, median positive rank
does not worsen, rankability and coverage are unchanged, and every required
metric is defined. With no challenger the decision is
`CHAMPION_RETAINED_NO_CHALLENGER`. These are factual holdout decisions, not model
promotion.

## Operator commands and current limitation

The historical/backtest CLI exposes readiness, run creation, score freezing,
evaluation, summary, and inspection commands. There is no T tuning, search,
training, promotion, or S2-holdout command.

Real execution remains data-blocked when runtime exists because S1 intentionally
has no reviewed official NSE session-calendar parser/source. On a workstation
without production runtime configuration it is environment-blocked. Fixtures
validate mechanics only and must never be reported as real holdout performance.
