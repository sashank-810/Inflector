# Low Market Attention feature primitives

Phase 6D-B converts already PIT-selected Phase 6D-A observations into neutral,
non-persisted facts. It does not decide that attention is low or favorable and
does not assign points. The bundle version is `attention_feature_bundle_v1`.

## Explicit input contract

`AttentionFeaturePrimitives.features_as_of(...)` receives the company/security
mode, aware research cutoff, exact news window, optional analyst economic-date
bound, and one caller-selected `AttentionSeriesIdentity` for each metric. A
series identity fixes provider dataset, provider scope, methodology version,
and lowercase measurement-definition SHA-256. The builder never discovers,
ranks, or falls back across providers or series.

Company-level requests require `company_level_only=True` and no security ID.
Security-level requests require `company_level_only=False` and one explicit
security ID. The two evidence levels are never blended.

The news lookup requires the exact requested start and end timestamps. There
is no default, nearest, latest, or preferred news window. The analyst lookup
uses the reader's latest eligible economic snapshot, optionally bounded by
`analyst_observation_on_or_before`; it never chooses an older observation for
its count or coverage status.

## Feature values and coverage

The five stable primitives are:

| Feature | Algorithm version | Unit | Meaning |
| --- | --- | --- | --- |
| `news_mentions_count` | `news_mentions_count_feature_v1` | `count` | Provider-reported count for the exact window |
| `news_window_duration_days` | `news_window_duration_days_v1` | `days` | Exact selected-window duration |
| `news_window_age_days` | `news_window_age_days_v1` | `days` | Exact elapsed time from window end to `as_of` |
| `analyst_coverage_count` | `analyst_coverage_count_feature_v1` | `count` | Provider-reported latest snapshot count |
| `analyst_snapshot_age_days` | `analyst_snapshot_age_days_v1` | `days` | Calendar-date age at `as_of` |

Only `complete` coverage makes a count value available. Complete zero is the
valid value `Decimal("0")`; missing is `None`. Partial and unknown counts are
also `None`, with distinct stable warnings, while the selected observation,
its availability, and its factual news-window or analyst-date metadata remain
auditable. A later partial or unknown correction therefore blocks the count;
the feature layer never resurrects an older complete revision.

News duration and age use integer timedelta components and exact `Decimal`
division, including microseconds. Analyst snapshot age is the integer
calendar-date difference between `as_of.date()` and `observation_date`; no
midnight timestamp is invented. Phase 6D-B applies no window-length or
freshness threshold.

Every available or coverage-blocked feature retains the complete
`PointInTimeAttentionObservation`, including its accepted `SourceRecordView`
and raw archive provenance. Feature `available_at` is exactly the selected
observation's source availability. It is never the cutoff, retrieval time,
ingestion time, window end, or observation date.

## Deliberate boundaries

The bundle is an immutable in-memory derivation and is not persisted. There is
no market-data normalization, BusinessEvent or announcement-count
normalization, peer ranking, cross-metric arithmetic, sentiment, AI, policy,
curve, or score. No migration or snapshot version is introduced.
`score_snapshot_v4` remains the latest persisted contract at maximum coverage
`0.95`; Low Market Attention remains missing and `final_score` remains null.
