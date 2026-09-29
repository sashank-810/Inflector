# External attention evidence

Phase 6D-A establishes an append-only, provider-neutral source domain for
external attention measurements. It stops at normalized evidence and
point-in-time reads. It does not decide whether attention is low, favorable,
or scoreable.

## V1 vocabulary and semantics

V1 accepts exactly `news_mentions_count` and `analyst_coverage_count`, both in
unit `count`. A news observation describes one explicit provider window through
`window_start_at` and `window_end_at`; an analyst observation is a snapshot at
one `observation_date`. The metric name never implies a 30-day, 90-day,
monthly, or global window.

Every observation declares `scope_code`, `methodology_version`, and a SHA-256
hash of its canonical measurement definition. The definition covers query
construction, issuer aliases, source universe, languages, geography,
deduplication, and analyst semantics without storing credentials. Provider,
scope, methodology, and definition hash jointly isolate a series;
similar-looking strings from different providers are not made comparable.

Coverage status is exactly `complete`, `partial`, or `unknown`. `complete`
means only that the provider asserts completeness inside its declared scope
and methodology. A reported zero is valid and remains distinguishable as
complete-zero, partial-zero, or unknown-zero. No observation is missing data,
not a fabricated zero, and none of these states is interpreted as low
attention in this phase.

## Archive-first ingestion

`AttentionObservationRecord` is the database-free provider value and
`AttentionDataProvider.fetch_attention_data()` returns the standard immutable
provider batch. Development supports only `MockAttentionDataProvider` and
`CSVAttentionDataProvider`; there is no live HTTP adapter or web scraper. The
CSV contract is:

`external_record_id, source_uri, company_legal_name, security_isin,
metric_code, reported_count, reported_unit, scope_code, methodology_version,
measurement_definition_sha256, coverage_status, observation_date,
window_start_at, window_end_at, reported_at, published_at, available_at,
revision_at`.

The original batch bytes enter the existing content-addressed raw archive
before normalization. Each normalized row points to its immutable
`SourceRecord`. The standard `(provider_dataset_id, external_record_id,
content_sha256)` source identity makes exact reruns idempotent. Providers never
receive database sessions and never write ORM rows directly.

`available_at` is mandatory provider/source knowledge time. It is never
invented from retrieval, ingestion, observation date, window end, publication,
or the current clock. Timestamps must be timezone-aware and normalize to UTC.
`SourceRecord.retrieved_at` and `attention_observations.ingested_at` are
operational audit times only.

## Append-only corrections

Changed content under one external record ID is appended only when its
semantic order `(available_at, revision_at or available_at)` is strictly newer
than every accepted revision. The same rule applies when a provider corrects
one economic measurement under a new external ID. Equal or older changed
positions are quarantined rather than selected by count.

News economic identity consists of company, optional security, metric, scope,
methodology, definition hash, and exact window. Analyst identity replaces the
window with `observation_date`. Count, source ID, operational timestamps, and
availability are not identity fields. A different scope, methodology, or
definition hash is a separate series, and provider datasets are never merged.

Company identity is required; security is optional. When an ISIN is supplied,
it must resolve to the same canonical company. Current security and listing
statuses do not gate historical ingestion or reads.

## Point-in-time reads

`PointInTimeAttentionReader` requires an exact provider, company, metric,
scope, methodology, definition hash, and aware cutoff. Callers explicitly
request company-level evidence or one security; the reader never blends both.
It first chooses the latest eligible accepted revision per provider external
ID, then resolves corrections per metric-specific economic identity. Equal
semantic ordering with changed values fails closed.

The exact-window news API returns one corrected observation or `None`. The
analyst API chooses the greatest eligible economic `observation_date`, never
the smallest count, largest count, coverage status, or retrieval time.
Coverage status is returned unchanged and has no selection priority.

## Deliberate boundaries

Market price, volume, delivery, turnover, and market capitalization are trading
activity, not external attention evidence. Announcement, filing, document,
BusinessEvent, and catalyst counts are also not attention evidence. V1 has no
search, social, ownership, sentiment, AI, feature, curve, score, snapshot, or
final-score behavior. `score_snapshot_v4` therefore remains the latest
persisted contract with maximum coverage `0.95`; Low Market Attention remains
missing and `final_score` remains null.
