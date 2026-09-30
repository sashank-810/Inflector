# Implementation roadmap

## Phase 0 — architecture and design (complete)

- Inspected the clean `Inflector` repository and removed its setup-only prompt.
- Added repository engineering rules and the architecture, data model, scoring,
  module-dependency, provider-interface, licensing, and risk decisions.
- Chosen architecture: modular monolith, FastAPI + Next.js, PostgreSQL source
  of truth, DuckDB/Parquet research mart, APScheduler worker, no Redis yet.

## Phase 1 — foundation

1. Create only used modules: `apps/api`, `apps/web`, `packages/core`,
   `packages/database`, migrations, tests, and scripts.
2. Pin Python 3.12/Node toolchains; add `.env.example`, `.gitignore`, Makefile,
   PostgreSQL-only Compose service, health check, and persistent volume.
3. Implement settings, error contracts, and API/web shells. Authentication and
   a worker are explicitly deferred because this slice has no background work.
4. Add SQLAlchemy/Alembic with the initial Company/Security/Listing schema,
   migration test, core repositories, and idempotent fictional seed data.
5. Add Ruff, mypy/pyright choice, pytest, frontend ESLint/typecheck/test setup,
   GitHub Actions CI, and development/deployment/backup documentation.

**Exit gate:** clean PostgreSQL Compose boot; migration upgrade/downgrade; API
health and company-read smoke test; web shell; lint/type/test commands pass.

## Phase 2 — data layer

### Phase 2A — ingestion spine

Build provider-neutral universe and daily-market CSV/mock adapters, immutable
content-addressed raw archival, provenance tables, deterministic quarantine,
and append-only revised price observations. This subphase deliberately excludes
real providers, financial statements, provider health UI, and analytical marts.

### Phase 2B — financial reporting spine

Add provider-neutral CSV/mock financial filings, multiple reported fiscal
periods per filing, controlled reported-metric definitions, exact INR-scale
normalization, and append-only restatement facts. Keep TTM, ratios, PIT read
selection, and all feature calculations out of this subphase.

### Phase 2C — corporate actions and historical identity

Add provider-neutral action ingestion, exact split/bonus/rights/dividend terms,
dated symbol transitions, and explicit security replacement lineage. Do not
derive adjusted prices, returns, or factors until a later deterministic phase.

### Phase 2D-A — market-data enrichment foundation (complete)

Add nullable provider-reported INR market cap and delivery quantity/fraction to
raw price bars, plus provider-dataset-local benchmark identities and append-only
daily benchmark bars. Reuse archive-first ingestion, quarantine, source
idempotency, provenance timestamps, and rollback semantics. Do not add PIT
market readers, adjusted prices, returns, valuation, structure features, or
scoring.

1. Define provider ports and shared ingestion envelope; build mock, CSV, and
   manual-import adapters first.
2. Implement raw archival, idempotent ingestion runs, normalization, company
   match/review workflow, corporate actions, price and financial fact storage.
3. Implement unit/currency normalization, quality rules, quarantine, provider
   health, freshness, and sample datasets. Enable official/licensed adapters
   only after licence and access review.

**Exit gate:** re-running a fixture is idempotent; bad rows quarantine with an
explanation; a filing revision and symbol rename survive normalization.

## Phase 3 — feature engine

### Phase 3A — point-in-time financial read layer

Implement explicit-provider, explicit-scope financial PIT selection before any
formula. Require UTC-aware `as_of`, use `available_at <= as_of`, preserve
reported fiscal-period semantics, and prove late filings/restatements cannot
leak future knowledge. No derived feature table, calculation, or provider
reconciliation belongs here.

### Phase 3B — period normalization and individual-quarter derivation

Derive only PIT-visible individual quarters from compatible additive monetary
duration facts: Q2 from H1/Q1, Q3 from 9M/H1, and Q4 from annual/9M. Preserve
reported-quarter precedence and complete component lineage. Do not persist
derived values or compute TTM, growth, or ratios.

### Phase 3C — point-in-time TTM construction

Add only TTM as the exact sum of four continuous PIT-normalized additive
monetary quarters. Retain complete nested lineage and missing-data safeguards;
defer all other fundamentals, growth, and margins.

### Phase 3D — growth, acceleration, and margin features

Add tested PIT-clean YoY/QoQ growth, loss-aware absolute transitions,
median-based acceleration, and comparable-quarter margins. Persistence and
scoring semantics remain deferred.

### Phase 3E-A — PIT instant financial snapshot foundation

Select complete same-`FiscalPeriod.id` sets of PIT-visible instant monetary INR
facts. Define latest by economic period end, preserve complete fact lineage,
and fail closed on ambiguous maximum periods. Add no ratios or persistence.

### Phase 3E-B — leverage and capital-efficiency primitives

Add PIT-clean net debt, debt/equity, TTM interest coverage, ROE, and ROCE over
the approved snapshot and TTM layers. Require exact ROE/ROCE balance-sheet
boundaries and retain complete input lineage without persistence or scoring.

### Phase 3E-C — cash-flow quality and trade working-capital primitives

Add PIT-clean CFO/PAT and CFO/reported-EBITDA conversion, receivable days, and
trade working-capital change over exact TTM and instant-snapshot boundaries.
Retain full lineage, explicit denominator warnings, and provider/scope
isolation without persistence or scoring. FCF, inventory days, payable days,
and CCC remain deferred until controlled capex-sign, COGS, and purchases
semantics exist.

### Phase 3F — PIT growth history, consistency, and persistence (complete)

Add complete fixed windows over public Phase 3D percentage-mode YoY series,
exact positive-share consistency, and explicit-threshold consecutive
persistence. Preserve nested lineage and refuse partial, gapped, or
absolute-change windows. This is the final deterministic Phase 3 financial feature slice;
scoring policy, confidence adjustment, configured thresholds, and persistence
caps begin only in Phase 4.

Unsupported liquidity and accounting formulas remain deferred until their
controlled source semantics and a separately approved phase exist.

### Phase 3G-A — PIT market and benchmark reads (complete)

Select raw price, enrichment, and benchmark revisions using explicit provider
context and knowledge cutoffs. Preserve accepted source lineage, atomic row
semantics, provider isolation, and one deterministic revision per economic
date. Do not derive adjustments or returns.

### Phase 3G-B — adjusted-price and return primitives (complete)

Apply canonical split/bonus adjustment semantics and construct adjacent PIT-safe
simple security and benchmark price returns. Cash-dividend total returns,
rights adjustment, relative strength, cumulative returns, persistence, and
scoring remain deferred.

### Phase 3G-C — Market Structure deterministic feature primitives (complete)

Compose approved PIT market, benchmark, adjustment, and adjacent-return evidence
into exact benchmark-relative strength, adjusted trend/volatility/consolidation,
raw close-times-volume activity, and delivery primitives. Provider contexts and
the benchmark are explicit; observation windows never fill calendar gaps. No
feature is persisted or scored. Phase 4D-F now adds the separately reviewed
pure Market Structure component scorer without changing this feature boundary.

### Phase 3H-A — Valuation deterministic feature foundation (complete)

Combine provider-reported PIT market capitalization with an explicit financial
provider/scope/FY/Q to produce market-cap/TTM-PAT, market-cap/total-equity,
market-cap/TTM-revenue, simplified EV, simplified-EV/TTM-EBITDA, and
simplified-EV/TTM-revenue evidence. The conservative names do not claim
canonical P/E, P/B, or complete EV accounting semantics. Features are
non-persisted and unscored.

Valuation scoring was deliberately moved out of the Phase 3 data layer and into
Phase 4 to preserve the feature/scoring boundary.

**Exit gate:** formula and edge-case tests pass; late-report/restatement tests
prove no future facts appear at a historical cutoff.

## Phase 4 — scoring

### Phase 4A — policy and control contracts (complete)

Persist immutable model versions and typed scoring configurations with
canonical checksums. Add provider-first financial-context selection, hard
eligibility rules, and independent exact-Decimal confidence. Do not calculate
component or final scores.

### Phase 4B — Financial Inflection component scoring (complete)

Map the six approved Financial Inflection subfactors to versioned development
curves with exact interpolation, clamping, coherent context/cutoff/endpoint
validation, a minimum coverage gate, and available-weight renormalization. No
other component, final score, or persistence is included.

### Phase 4C — partial score persistence and orchestration (complete)

Add immutable fingerprinted snapshots, the selected Financial Inflection
component audit, structured explanations, PIT lineage, and idempotent history.
Eligibility and confidence are retained independently; the final score remains
null.

### Phase 4D-A — Business Quality component scoring (complete)

Score current ROCE, ROE, and configured margin levels through versioned
development curves with exact Decimal arithmetic, minimum coverage, and
available-weight renormalization. No persistence, orchestration, confidence
adjustment, top-level weighting, or final score is included.

### Phase 4D-B — Cash-Flow Quality component scoring (complete)

Score CFO/PAT, CFO/EBITDA, negated receivable days, and negated
revenue-normalized trade-working-capital change through explicit auditable
transforms and versioned development curves. Phase 3 accounting signs remain
unchanged. No persistence, orchestration, confidence adjustment, top-level
weighting, or final score is included.

### Phase 4D-C — Balance Sheet component scoring (complete)

Score net debt/TTM reported EBITDA, debt/equity, and interest coverage through
explicit size-neutral transforms and versioned development curves. Validate
stored stock-period identity as well as context, cutoff, endpoint, and economic
period end. No persistence, orchestration, confidence adjustment, top-level
weighting, or final score is included.

### Phase 4D-D — coherent financial-component orchestration (complete)

Add a `score_snapshot_v2` path that persists any scoreable subset of Financial
Inflection, Business Quality, Cash-Flow Quality, and Balance Sheet from one
provider/scope context. Provider-first policy wins over score and coverage;
missing coverage remains visible. Under current standard weights the maximum
implemented top-level coverage is 0.60, while `final_score`, final
contributions, and confidence multiplication remain absent. Preserve the v1
Financial-Inflection-only path and fingerprint semantics.

### Phase 4D-E — Valuation component scoring (complete)

Score the five approved Phase 3H-A valuation ratios through explicit negation,
development-only absolute curves, exact Decimal coverage, and available-weight
renormalization. Preserve conservative accounting names and unavailable-feature
warnings. Do not persist or integrate Valuation into v2; its explicit market
provider requires separately reviewed cross-domain orchestration.

### Phase 4D-F — Market Structure component scoring (complete)

Score only the seven approved Phase 3G-C primitives through explicit
identity/negation transforms and uncalibrated development curves. Preserve the
0.70 coverage floor, warning/evidence lineage, and available-weight
renormalization. Absolute volatility and absolute close-times-volume remain
unscored. This pure scorer is not added to `score_snapshot_v2`.

### Phase 4D-G — cross-domain partial score orchestration (complete)

Add the separate `score_snapshot_v3` persistence path for the four financial
components, Valuation, and Market Structure. Financial context selection stays
provider-first; Valuation participates in financial-context scoreability while
Market Structure never selects that context. Standard maximum coverage is
`0.75`, with no top-level renormalization, confidence multiplication, final
contribution, or final score. Business Catalyst and Low Market Attention remain
separate missing top-level domains. Acceptance does not automatically activate
a final score.

### Later approved slices — remaining top-level component scorers

Implement only separately approved Business Catalyst and Low Attention
component policies. Neither v1 nor v2 fabricates them.

### Phase 4E — final Opportunity Score activation (fulfilled by Phase 4D-K)

All eight deterministic top-level scorers now exist with complete configured
weight coverage of `1.00`. Phase 4D-K implemented and accepted
`score_snapshot_v5`, which activates the final Opportunity Score only for
complete positive-weight coverage, without top-level renormalization. A missing
required component leaves `final_score` null, so this roadmap goal is fulfilled.

**Exit gate:** scores are deterministic, auditable, versioned, and explainable.

## Phase 5 — research dashboard

### Phase 5A — Opportunity Score Read API & Research Queue Foundation (complete)

Expose a read-only, V5-only research queue and snapshot-detail API. Require an
explicit model family, preserve configuration and security contexts, select
the latest semantic cutoff without score shopping, serialize exact Decimal
values as strings, and retain persisted partial-state and explanation audit
semantics. Add no scoring, mutation, migration, current-state reinterpretation,
or frontend implementation.

Planned only after separate review: Phase 5B company research-page backend and
deeper audit reads.

1. Build dark desktop-first dashboard, explorer filters, company research page,
   score explanations, inflection radar, and watchlist/notes.
2. Include loading, empty, error, freshness, confidence, source, and exclusion
   states from the start.

**Exit gate:** a user can trace any displayed score to inputs and evidence.

## Phase 6 — announcement and catalyst evidence

### Phase 6A — Announcement & Document Evidence Foundation (complete)

Persist provider-neutral, append-only announcement and document metadata with
raw archive provenance, deterministic validation, revision ordering, and PIT
reads. This phase is source evidence only: it adds no document text, AI,
catalyst/risk interpretation, or scoring.

### Phase 6B — Document acquisition/text/evidence extraction foundation (complete)

Acquire bounded offline document bytes into immutable content-addressed
storage, extract deterministic PDF/plain text with explicit runtime identity,
and expose page/offset citation primitives. Source knowledge time remains the
Phase 6A availability timestamp. No OCR, AI, interpretation, or scoring enters
this phase.

### Phase 6C-A — Deterministic Business Event Detection & Evidence Persistence (complete)

Persist the exact six-type neutral `business_event_rules_v1` output and its
headline or document/asset/extraction citation lineage. Detection is
page-local, corrected revisions remain distinct, and one announcement/type has
one event with multiple evidence spans. No quantities, materiality, direction,
confidence, AI, scoring policy, or snapshot changes are included.

### Phase 6C-B — Deterministic Quantitative Event Facts (complete)

Extract controlled explicit event amounts, capacities, stakes, and commercial
commencement dates only from accepted Phase 6C-A evidence spans. Persist an
immutable derivation even when it contains zero facts. Values are source
observations, not summed totals or materiality; there is no FX, financial join,
AI, direction, score, or snapshot change.

### Phase 6C-C — Business Event Materiality & Recency Feature Primitives (complete)

Produce a non-persisted bundle for one PIT-selected event. Resolve equivalent
quantitative observations without summing, fail closed on conflicts, anchor
revenue denominators to event source availability, and calculate raw event age
at the requested cutoff. No market cap, FX, provider selection, cross-event
aggregation, AI, scoring policy, persistence, or snapshot change is included.

### Phase 4D-H — Business Catalyst Component Scoring (complete)

Add an optional versioned policy and pure max-event scorer over explicit
Phase 6C-C bundles. Orders and capacity expansions require quantified strength;
commercial commencement and regulatory approval use configurable base
strength. Capex and acquisitions remain neutral and unscoreable. This phase
does not itself persist the component, apply its 0.20 top-level weight, or
activate a final score.

### Phase 4D-I — Business Catalyst Cross-Domain Snapshot Orchestration (complete)

Add the immutable `score_snapshot_v4` path without changing V1/V2/V3. Reuse
V3 financial-context selection exactly, then evaluate Business Catalyst only
for the selected provider/scope and an explicit coherent event provider/set.
Persist the exact component score, one selected-event explanation, and complete
V4-only evidence lineage. Maximum standard coverage is `0.95`; Low Market
Attention, final contributions, top-level renormalization, and `final_score`
remain absent.

### Phase 6D-A — External Attention Evidence Foundation (complete)

Add provider-neutral, append-only news-mention and analyst-coverage count
observations with archive-first provenance, explicit provider scope and
methodology identity, mandatory source availability, correction-safe economic
identities, and explicit PIT readers. Market activity and BusinessEvents are
not attention evidence. This phase adds no attention feature, policy, score,
snapshot version, final contribution, or final score.

### Phase 6D-B — Low Market Attention Feature Primitives (complete)

Derive non-persisted complete counts and raw temporal metadata from exact
caller-selected attention series. Preserve complete-zero, missing,
partial/unknown coverage, correction selection, provider/methodology/security
isolation, and full SourceRecord/archive lineage. Add no freshness policy,
market or event normalization, peer ranking, AI, score, migration, snapshot,
or final score.

### Phase 4D-J — Low Market Attention Pure Component Scoring (complete)

Add an optional, series-bound pure scorer over Phase 6D-B features. Require a
deterministic aligned news window and latest analyst snapshot; treat temporal
features only as comparability/freshness gates. Low complete counts score
higher, while missing, partial, unknown, stale, or mismatched evidence stays
unavailable. Pure scorer coverage reaches `1.00`, but there is no persistence,
top-level contribution, snapshot v5, or final score.

### Phase 4D-K — Final Snapshot / Opportunity Score Activation (complete)

Add immutable `score_snapshot_v5`, persist policy-bound Low Market Attention
after unchanged financial-context selection, and activate exact Decimal
top-level contributions only for complete weight coverage. Missing components
remain null without renormalization; valid zero scores remain available. V1-V4
stay immutable, and the existing schema requires no migration.

**Core deterministic scoring milestone:** the eight-component Opportunity
Score engine is complete, with exact Decimal aggregation, immutable PIT/audit
lineage, `score_snapshot_v5`, and final-score activation. This does not mean the
whole product is complete; research/dashboard UX, production providers,
empirical calibration/backtesting, alerts, and hardening remain later work.

### Planned after separate review

Alerts, empirical calibration/backtesting, and production attention-provider
work remain separate later phases.

**Exit gate:** unstructured outputs cannot alter financial facts or silently
enter a production score.

## Phase 7 — alerts

1. Detect meaningful score/catalyst/risk changes with deduplication and
   quiet-period rules.
2. Deliver in-app alerts and retain a channel interface for email/Telegram.

**Exit gate:** no duplicate alert on an idempotent rerun; every alert explains
what changed, why, evidence, confidence, and score movement.

## Phase 8 — point-in-time backtesting

1. Export named, immutable DuckDB/Parquet manifests and historical universe
   memberships.
2. Implement ranked portfolio construction, rebalance schedules, holding
   periods, costs, corporate actions, benchmarks, and results.
3. Report return distribution, hit rate, drawdown, volatility, Sharpe, Sortino,
   and decile comparison, with all assumptions visible.

**Exit gate:** fixture backtests demonstrate no look-ahead and preserve
delisted/survivorship-sensitive constituents where data permits.

## Phase 9 — historical research

Build winner/false-positive cohorts (3x/3-year, 5x/5-year definitions),
pre-outcome feature windows, and cohort comparison tools. Treat findings as
research, not automatic model weight changes.

## Phase 10 — hardening

Tune queries and exports, add data-quality/operations dashboards, restore-test
backups, review security, complete documentation, and run end-to-end tests.

## Decision log and assumptions

- Initial active universe is liquid listed common equities; ETFs, REITs, SME,
  preference shares and suspended issues are configured separately.
- Provider and consolidated/standalone preference are later research-policy
  choices; Phase 3A requires both provider dataset and filing scope explicitly.
- Daily bars are first; intraday is a later provider and storage extension.
- No real data is fabricated. Mock/CSV datasets unblock development.
- Exchange and vendor permissions must be approved before production feeds or
  any use outside personal research; Phase 2 can proceed without them.

## Immediate Phase 1 plan

Create the foundation vertically: Compose + settings + database migration + one
Company API read path + web dashboard shell + health/logging + CI tests. This
proves the delivery spine before broad schema or UI work.
