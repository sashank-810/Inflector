# Deterministic valuation features

Phase 3H-A combines an explicit provider-reported market-cap observation with
an explicit PIT financial context. It produces evidence primitives, not a
valuation opinion, score, ranking, or persisted feature.

## Declared identity and time

Every bundle declares the market provider dataset, financial provider dataset,
security, company, filing scope, ending fiscal year and quarter, interval, and
timezone-aware knowledge cutoff. Market and financial providers may differ;
neither has fallback or reconciliation. The security must belong to the stated
company, but mutable security/listing status and ticker are not historical
eligibility gates.

The market observation is the latest PIT-selected atomic bar, optionally on or
before a stated economic date. `market_on_or_before` does not replace `as_of`:
the former bounds trading date and the latter controls knowledge. The newest
selected bar is never skipped merely because `market_cap` is null. Market cap
is the raw provider-reported INR value; it is not rebuilt from adjusted price
or inferred shares and is not modified for splits or bonuses.

Financial evidence uses the exact requested FY/Q. TTM revenue, PAT, and
reported EBITDA are constructed independently. Instant equity and debt/cash
snapshots are also independent and must each come from one unambiguous stored
`FiscalPeriod.id`. Multiple matching period identities fail closed. Evidence
used together must share the same economic period end. Corrections and
restatements become visible only at their inclusive `available_at <= as_of`
boundary.

## Implemented primitives

All arithmetic is exact `Decimal`, with no internal rounding:

- `market_cap_to_ttm_pat = market_cap / TTM PAT`, only for positive PAT;
- `market_cap_to_total_equity = market_cap / total_equity`, only for positive equity;
- `market_cap_to_ttm_revenue = market_cap / TTM revenue`, only for positive revenue;
- `simplified_enterprise_value = market_cap + total_debt - cash_and_equivalents`;
- `simplified_ev_to_ttm_ebitda`, only for positive simplified EV and EBITDA;
- `simplified_ev_to_ttm_revenue`, only for positive simplified EV and revenue.

The first two names are intentionally not canonical P/E or P/B. Controlled PAT
does not yet guarantee profit attributable only to ordinary holders, and total
equity is broader than an explicitly attributable ordinary-shareholder book
value. Enterprise value is explicitly **simplified** because the controlled
bridge does not separately model all minority interest, preferred equity,
pension, lease, or associate/investment adjustments.

Missing and invalid denominators remain unavailable with explicit warnings;
they never become zero and are never repaired with `abs()`. A missing PAT does
not suppress valid revenue, equity, or EV features. Missing debt or cash makes
the simplified EV and its multiples unavailable without suppressing the equity
multiple. Net cash is valid. A zero or negative simplified EV is retained as
raw evidence, but its multiples are unavailable rather than interpreted as
cheap. Market cap must be positive.

## Audit and availability

Each multiple retains only its supporting evidence. TTM objects preserve all
quarter/fact/source lineage; instant snapshots preserve their selected facts;
the market bar preserves its raw source record. Feature `available_at` is the
maximum availability of known participating evidence, including partial
evidence when a feature is unavailable. It is never based on `ingested_at`.

Phase 3H-A has no freshness heuristic, peer/sector comparison, historical
percentile, cheap/expensive label, scoring curve, confidence adjustment,
top-level valuation weight, persistence, or adjusted-price dependency. A
separate Phase 3H-B policy review is required before valuation scoring.
