# Company research context, history, and audit API

Phase 5B extends the read-only V5 product boundary needed by a future company
research page. It reads immutable persisted snapshots only: it does not run
scoring, choose a default context, consult current time, or replace historical
evidence with live database rows.

## Explicit research contexts

`GET /api/v1/companies/{company_id}/research-contexts` requires an explicit
`model_family`. It returns every persisted V5 context identified by the exact
company, selected security, and scoring-configuration ID. Multiple securities
and configurations remain separate; the backend does not choose a primary,
best, or highest-scoring context.

Each context exposes current canonical security/listing identity, immutable
model/configuration identity and checksum, and its bounded latest snapshot.
Current identity is display metadata only and does not rewrite historical
eligibility. Listings retain current and historical statuses and are not used
as gates.

The context-latest order is knowledge cutoff descending, fiscal year
descending, fiscal quarter descending, then snapshot fingerprint ascending.
Score, confidence, coverage, status, and operational `created_at` never affect
selection. Thus, a newer partial snapshot replaces an older final snapshot.
An existing company with no requested-family V5 evidence returns an empty
page; an unknown company returns `404`.

## Exact-context history and display change

`GET /api/v1/companies/{company_id}/opportunity-score-history` requires
`model_family`, `security_id`, and `scoring_configuration_id`. It returns only
V5 snapshots for that exact context in the same semantic order as context
discovery. It does not substitute a similarly named configuration or another
security.

When at least two snapshots exist, `latest_change` always compares the first
two snapshots in the full unpaginated semantic history. It therefore remains
the same for every page. The comparison is display-only persisted-value
subtraction and state comparison:

- score deltas exist only when both final scores exist;
- confidence and coverage deltas are always exact Decimal differences;
- component score deltas exist only when both component scores exist;
- added/removed availability is based on component presence, not truthiness;
- exact zero remains available, while missing remains null.

All deltas use the Phase 5A canonical Decimal-string contract, including
negative values. They carry no positive/negative, bullish/bearish, or
improvement/worsening interpretation.

If multiple immutable snapshots share cutoff, fiscal year, and fiscal quarter,
fingerprint order is the deterministic display order. The API deliberately
does not use `created_at` to infer which was persisted later; this is a stated
limitation of same-semantic-time history.

## Bounded deep audit

`GET /api/v1/opportunity-scores/{snapshot_id}/audit` returns a bounded index of
the selected V5 snapshot's stored union manifest. It includes historical
component, Phase-3, curve, and Opportunity Score aggregation algorithm
identities plus counts for exactly these categories:

- `financial_facts`
- `market_bars`
- `benchmark_bars`
- `corporate_actions`
- `announcements`
- `documents`
- `document_assets`
- `text_extractions`
- `business_events`
- `business_event_evidence`
- `quantitative_derivations`
- `quantitative_facts`
- `attention_observations`
- `attention_source_records`

`GET /api/v1/opportunity-scores/{snapshot_id}/audit/{category}` pages one typed
category in its persisted order. Items are the stored canonical semantic JSON
objects. The read path does not query live facts/sources, resort or deduplicate
evidence, open archive objects, fetch source URIs, or return raw document or
provider payload bytes.

Every context, history, and audit snapshot passes the existing V5 immutable
integrity checks. Audit reads additionally require the complete bounded union
shape and historical algorithm fields. Corrupt snapshots or malformed audit
manifests fail closed with the same stable HTTP 500 error as Phase 5A. V1–V4
IDs remain outside this product resource and return `404`.

Phase 5B adds no database writes, migration, scoring, provider selection,
current-status gating, frontend, alerts, backtesting, AI, or recommendation.
