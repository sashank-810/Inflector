# Low Market Attention pure component scoring

Phase 4D-J adds `low_market_attention_component_v1`, a pure, non-persisted
component scorer over one explicit `attention_feature_bundle_v1`. It performs
no reads, provider selection, feature recomputation, persistence, top-level
weighting, or final-score calculation.

## Policy-bound evidence

`LowMarketAttentionScoringPolicy` binds separate exact provider dataset,
scope, methodology version, and measurement-definition hash identities for
news and analyst evidence. It also fixes company-level or security-level
evidence. A bundle mismatch fails closed; the scorer cannot shop among
providers, scopes, methodologies, definitions, or evidence levels.

The reference policy requires company-level evidence and the latest PIT
analyst snapshot. A non-null analyst economic-date bound is rejected so a
caller cannot select an older, lower coverage count.

The reference news window is deterministically aligned with
`utc_day_start_v1`: its end is UTC midnight at the start of the cutoff's UTC
date and its start is exactly 30 Decimal days earlier. A different historical
window is rejected. The required duration must resolve exactly to an integer
number of microseconds.

## Subfactor scoring

Only `news_mentions_count` and `analyst_coverage_count` produce scores, each
with reference weight `0.50`. News duration/age and analyst snapshot age are
comparability and freshness gates, never independent sources of points.

Both count transforms are explicit negations because the shared piecewise
curve requires non-decreasing scores:

| News count | 80 | 40 | 20 | 10 | 5 | 2 | 0 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Score | 0 | 10 | 30 | 55 | 75 | 90 | 100 |

| Analyst count | 30 | 15 | 8 | 4 | 2 | 1 | 0 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Score | 0 | 10 | 35 | 60 | 80 | 90 | 100 |

The reference freshness limits are one Decimal day after the news-window end
and 180 calendar days for the analyst snapshot. The news duration must be
exactly 30 days. Partial, unknown, missing, stale, or duration-mismatched
evidence is unavailable rather than scored as zero. Complete zero remains
valid and scores 100 for its subfactor. A valid high-attention component score
of zero also remains available.

Available subfactor weights are renormalized only when they meet the configured
minimum coverage. The reference minimum is `1.00`, so both subfactors are
required. Missing evidence is never weakness. The reference counts of five
news mentions and two analysts yield `77.5`.

Every scored or unavailable subfactor retains its exact Phase 6D-B feature,
and the component retains the full feature bundle. Component `available_at` is
the latest source availability among all present selected observations,
including partial, unknown, stale, or otherwise blocked evidence.

## Development-policy and system boundaries

The curves and freshness thresholds are deterministic development policy, not
empirically calibrated claims about future returns. Retuning requires a new
policy/config checksum rather than silent v1 changes.

Counts are not normalized by market data, company size, events, announcements,
or peers. There is no percentile, rank, sentiment, AI, persistence, migration,
or provider selection in the pure scorer. Pure deterministic scorer coverage
reaches `1.00`.

Phase 4D-K separately consumes this unchanged pure result in
`score_snapshot_v5`. Low Market Attention still cannot select a financial
context. Its policy-bound series identity and full bundle lineage are retained;
scored-subfactor explanations remain internal to the component. The top-level
`0.05` contribution exists only when every positive-weight component is
available and the final score is activated. There is no renormalization, and
V1-V4 remain immutable. See
[`opportunity-score-activation.md`](opportunity-score-activation.md).
