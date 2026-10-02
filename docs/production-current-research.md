# Current production research activation

Production Data Activation D connects accepted production observations to the
existing deterministic `score_snapshot_v5` orchestrator. It adds no scoring
formula, provider-selection heuristic, score renormalization, or frontend
feature. A persisted partial or ineligible V5 snapshot is a successful research
result when source evidence is incomplete.

## Current-only PIT boundary

Historical NSE and GDELT artifacts retain the UTC time at which Inflector
actually retrieved them as `available_at`. An old artifact fetched today is
known today. This bootstrap supports current research after acquisition; it is
not historical PIT reconstruction or backtest evidence.

`ingest-market-range` attempts the exact UDiFF and index artifact for every
requested calendar date. A 404 is `source_not_available`; the command does not
infer holidays, skip weekends, walk backwards, or substitute another session.
One invocation is limited to 150 calendar days.

## Explicit profile and policy

[`production_research_v1.json`](../config/research/production_research_v1.json)
defines `nse_current_research_v1` with exact provider/dataset codes, `NIFTY 50`,
consolidated-then-standalone scope priority, named eligibility/confidence slots,
and explicit liquidity, catalyst, market, listing, and attention windows.

Eligibility core availability counts the exact PIT-visible source metrics
`operating_revenue`, `pat`, `cash_flow_from_operations`, and `total_equity`.
This does not alias `operating_revenue` to scoring metric `revenue`. Comparable
history comes from actual PIT YoY features. Liquidity is exact Decimal
`close_price * volume` over the configured complete window; it is not market
capitalization.

Official-NSE source reliability input `1` is operational configuration, not
empirical calibration. The v1 critical-quality rule list is explicitly empty;
that does not mean data-quality issues do not exist. Current identity metadata
is used only for the explicit current run and does not rewrite history.

[`production_opportunity_v5.json`](../config/scoring/production_opportunity_v5.json)
promotes the accepted V5 weights, curves, floors, Business Catalyst, and Low
Market Attention policy. Initialization replaces only database-specific UUIDs
after exact provider-code plus dataset-code resolution. It never chooses a
provider or context by score. Immutable conflicts fail closed.

The assembler calls existing PIT readers and feature primitives. It does not
derive EBITDA, sum debt, derive market cap, manufacture delivery, or redetect
business events. V1 continues to report missing delivery. V2 may consume only
PIT-visible official delivery observations from the explicit delivery dataset;
the exact accepted 20-bar primitive remains unchanged. Valuation still reports
`market_cap_missing`, because no official market-cap source passed Production F
qualification. Delivery features can remain unavailable, and a correct partial
snapshot is expected.
The existing financial context resolver remains authoritative.

Research V3 preserves V2 and explicitly binds
`nse_indas_financial_primitives_v1`. Its policy-aware period normalizer may
expose `RevenueFromOperations`/`operating_revenue` as generic `revenue` without
creating a second source fact; complete nested lineage retains the source fact
and policy checksum. Direct current/non-current borrowings, finance cost and
depreciation/amortisation are available as their own exact metrics. Reported
EBITDA and total debt remain unapproved, so their dependent evidence remains
missing. See
[`production-financial-primitives.md`](production-financial-primitives.md).

## GDELT news methodology

GDELT DOC 2.0 is called only at `api.gdeltproject.org` with
`mode=TimelineVolRaw`, `format=json`, and explicit UTC start/end timestamps.
Methodology `gdelt_exact_company_name_news_v1` sends one exact quoted canonical
legal company name. It adds no ticker, brand, subsidiary, executive, language,
country, domain, tone, or article-body expansion. This can undercount by design.

Exact successful response bytes are archived before a company-level
`news_mentions_count` is accepted. Counts sum the raw `Article Count` series,
not normalized TimelineVol percentages. A structurally complete zero is valid;
an empty, malformed, throttled, failed, or ambiguous response is unavailable,
not zero. Availability is observed successful retrieval UTC. At most one count
request is made per company per run and no article bodies are downloaded.

No approved analyst-coverage source exists. Inflector does not scrape brokers,
Yahoo, Trendlyne, Moneycontrol, or search-result counts. The profile records the
analyst source as null and never manufactures a count. News-only evidence may
remain an incomplete Low Market Attention bundle, retaining lineage while the
component stays unavailable under the accepted coverage rule.

## Commands

```powershell
python -m inflector_data.nse_cli ingest-market-range `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS `
  --from-date 2026-06-01 --to-date 2026-09-30

python -m inflector_data.research_cli ingest-gdelt-news `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --research-profile config/research/production_research_v1.json `
  --symbol TCS --knowledge-cutoff 2026-10-01T23:59:59+05:30 `
  --gdelt-raw-root $env:INFLECTOR_GDELT_RAW_ROOT `
  --gdelt-license-class $env:INFLECTOR_GDELT_LICENSE_CLASS

python -m inflector_data.research_cli init-model `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --model-family inflector_v1 --model-semantic-version 1.0.0 `
  --git-sha <accepted-git-sha> --effective-from 2026-10-01T00:00:00+05:30 `
  --research-profile config/research/production_research_v1.json

python -m inflector_data.research_cli run-current `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --model-family inflector_v1 `
  --research-profile config/research/production_research_v1.json `
  --symbol TCS --fiscal-year 2025 --fiscal-quarter 4 `
  --knowledge-cutoff 2026-10-01T23:59:59+05:30
```

`--symbols-file` preserves deterministic input order, de-duplicates symbols,
and is capped at 100. Per-company failures remain visible. Partial snapshots are
success; operational and integrity failures are not.

[`run_inflector_current.ps1`](../scripts/run_inflector_current.ps1) executes
universe, bounded market/index, financials, actions, catalyst evidence, optional
GDELT, model initialization, and V5 current research in that order. It requires
separate NSE/GDELT raw roots and caller-supplied source classifications.

The result reports exact company/security/cutoff/fiscal identity,
model/configuration/checksum, attempted and selected contexts, component
availability, coverage, confidence, snapshot ID/fingerprint, final score or
null, and explicit unavailability reasons. Exact reruns reuse immutable
identity. Corrections follow existing append-only source and fingerprint rules.

No frontend change is needed. Open
`/opportunities?model_family=<explicit-family>` after starting the accepted API
and web app. Missing positive-weight components leave `final_score` null and all
final contributions null; no top-level renormalization occurs.

This activation adds no migration, backtest, alert, ranking, recommendation,
analyst fabrication, market-cap fabrication, or frontend scoring.

Production Data Activation E wraps these accepted commands with an explicit
cycle clock, persistent run/stage/symbol ledger, expiring database lease,
explicit resume, status inspection, and Windows scheduled runner. It does not
change this research profile or V5 semantics. See
[`production-operations.md`](production-operations.md).

## Automatic financial endpoints

Production H adds explicit `run-current-auto` mode under immutable Research V4.
For each symbol it resolves the latest accepted fiscal endpoint whose source
evidence is visible at the caller-supplied knowledge cutoff, using
`nse_latest_pit_financial_endpoint_v1`. Scope priority remains consolidated,
then standalone. The resolver orders authoritative persisted period metadata;
it never examines metric completeness, component coverage, confidence, or
score. Consequently, a newer partial filing is selected over an older complete
filing. No endpoint is an issuer-level unavailable result, while ambiguous
period metadata fails closed without stopping later symbols.

The manual `run-current --fiscal-year ... --fiscal-quarter ...` path remains
available. Once AUTO selects an endpoint, it delegates to that same accepted
research assembler and V5 orchestrator with the selected FY, quarter, and
scope. See
[`production-financial-endpoint-discovery.md`](production-financial-endpoint-discovery.md).
