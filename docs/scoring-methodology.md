# Scoring methodology

## Research purpose and guardrails

The Early Opportunity Score ranks companies for further research. It is not an
investment recommendation, price target, probability of return, or prediction
of multibaggers. It favours an evidence-backed combination of improving
business fundamentals, catalysts, quality, and a still-manageable market
attention profile. It must not disguise missing data as strength.

`Inflection Model v1.0` is the first proposed configuration. It will be
implemented only after Phase 2/3 produces validated point-in-time inputs. Each
run persists the model version, configuration checksum, source cutoff, feature
lineage, components, contributions, confidence, and explanations.

## Input admissibility

Every feature calculation receives `as_of_date` and `knowledge_cutoff`. It can
read only facts, disclosures, holdings, and prices with
`available_at <= knowledge_cutoff`; it selects the latest eligible revision for
each economic item. Financial context selection follows the active policy's
provider-first lexicographic `provider_dataset_priority` and then
`filing_scope_priority`; there is no hidden consolidated-first default. A later
restatement cannot revise a past score. Scores are not produced for records failing material quality or
eligibility checks.

Minimum eligibility for v1.0: an active common-equity listing, a configurable
liquidity floor, 8 comparable quarters (or a declared new-listing exception),
and enough reported inputs to calculate a defined subset of the financial core.
Insufficient history, suspended/delisted status, invalid units, and unresolved
critical quality issues cause exclusion or reduced confidence, never a zero
risk score.

## Component model

No final Opportunity Score aggregation rule is approved yet. Current component
scores stay on independent 0–100 scales; configured top-level weights are audit
metadata until a later approved aggregation phase. Missing components are
disclosed. Confidence is an independent audit/display value and is not
multiplied into component or subfactor scores. Any future aggregation or risk
gate requires explicit review.

| Component | Initial weight | Intent |
|---|---:|---|
| Financial inflection | 25% | Acceleration, persistence, and margin/capital improvement |
| Business catalyst | 20% | Material, evidence-backed change in business trajectory |
| Business quality | 15% | Durable profitability and capital efficiency |
| Cash-flow quality | 10% | Profit conversion and cash-generative economics |
| Balance sheet | 10% | Leverage, coverage, liquidity, and working capital |
| Valuation | 10% | Price relative to quality, growth, history, and sector |
| Market structure | 5% | Constructive, liquid price/volume behavior—not RSI screening |
| Low market attention | 5% | Less-recognised improvement, not obscurity for its own sake |

The configuration, including thresholds, caps, universes, and weights, lives
in `scoring_configurations`; weights are never embedded in application code.

Low-level deterministic feature readers never select a preferred provider or
filing scope. Provider and scope preference is an explicit scoring
configuration, never an implicit consolidated-first calculation rule.

Phase 4A implements that context choice as provider-first lexicographic policy:
for each configured provider in order, it tries configured filing scopes in
order. One coherent candidate context is selected; features are never borrowed
across provider/scope pairs.

## Deterministic feature definitions

All period metrics use normalized INR figures and source lineage. Let `R_t`
mean revenue for the current quarter and `P_t` PAT.

| Feature | Formula / policy |
|---|---|
| Revenue YoY | `(R_t / R_t-4) - 1`, only for comparable quarterly periods |
| Revenue QoQ | `(R_t / R_t-1) - 1`; displayed but seasonality-aware and lower weight |
| PAT YoY / QoQ | Corresponding formula on PAT; losses and near-zero denominators use signed absolute-change rules and a warning |
| Growth acceleration | current YoY growth minus trailing median of the prior 3 comparable YoY growth observations |
| Growth consistency | raw positive share over an explicit complete consecutive percentage-mode YoY window; zero is non-positive |
| Persistence | consecutive percentage-mode YoYs strictly above an explicit threshold within an explicit complete lookback window |
| Margin expansion | current operating/EBITDA margin minus comparable prior-year margin, in basis points |
| ROE | TTM PAT / average beginning-and-ending equity |
| ROCE | TTM EBIT / average capital employed, where capital employed = equity + interest-bearing debt − cash; definition/version is stored |
| ROIC | NOPAT / average invested capital, only when tax and invested-capital inputs are adequate |
| Net debt | interest-bearing debt − cash and cash equivalents |
| Debt/equity | interest-bearing debt / equity; flagged if equity is non-positive |
| Interest coverage | TTM EBIT / TTM finance cost; non-positive finance cost is undefined and flagged, never repaired with an absolute value |
| CFO conversion | TTM CFO / TTM PAT when PAT is positive; otherwise retain exact CFO − PAT with an undefined-ratio warning |
| CFO / EBITDA | TTM CFO / TTM reported EBITDA when EBITDA is positive |
| FCF (planned) | Deferred until controlled metric metadata defines the provider capex sign convention |
| Receivable days | average receivables / TTM revenue × 365, where inputs permit |
| Trade working-capital change | ending minus beginning of receivables + inventory − payables, using exact TTM boundaries |
| Cash conversion cycle (planned) | Deferred until controlled COGS/purchases denominator semantics exist |
| Enterprise value | market cap + debt + preferred/minority interests where available − cash |

Trailing periods are calculated only from observations available at the cutoff.
Winsorisation, sector comparisons, minimum denominators, and score breakpoints
are configuration values and carry their own version.

Phases 3E-B and 3E-C provide only the implemented raw versioned calculations
described in their focused documents. They do not apply near-zero thresholds,
sector normalization, weights, persistence rules, or score contributions.
FCF, inventory days, payable days, and CCC remain planned rather than
implemented deterministic primitives.

Phase 3F supplies only the raw growth-history primitives. Phase 4 configuration
will choose consistency/persistence window sizes, persistence thresholds, and
score caps. Data-quality or confidence adjustment, weighting, new-listing
exceptions, and outlier policy are not part of the Phase 3F calculations.

Phase 4A persists these policy inputs and provides eligibility and confidence
contracts only. Eligibility is a hard, reasoned gate rather than a zero score.
Confidence is an independent weighted Decimal blend of completeness, supplied
source reliability, linear recency, comparable-history coverage, and supplied
evidence confidence. Missing recency/evidence lowers confidence to the extent
of its configured weight but does not itself make a company ineligible. No
feature value is multiplied by confidence in Phase 4A.

Phase 4B implements only the Financial Inflection component. Versioned
piecewise-linear development curves normalize revenue/PAT acceleration, the
configured margin expansion, same-quarter prior-year ROCE improvement, growth
consistency, and the persistence streak/window ratio. Endpoint clamping limits
extreme values. Missing evidence is disclosed and available weights are
renormalized only after a configured minimum-coverage gate. Confidence and the
top-level 25% component weight are deliberately not applied. The development
breakpoints validate mechanics and require calibration/backtesting before any
activation; they are not asserted to be correct or predictive.

Phase 4C persists Financial Inflection as a partial component set with exact
eligibility, confidence, selected context, subfactor explanations, and source
lineage. It never treats that component as the overall Opportunity Score:
`final_score` and final component contribution remain null, top-level coverage
is the configured Financial Inflection weight, and missing future components
are neither zero-filled nor renormalized. Confidence remains a separate audit
value rather than a score multiplier.

Phase 4D-A implements a pure Business Quality level score from current ROCE,
current ROE, and an explicitly configured current-quarter operating or EBITDA
margin. Versioned piecewise-linear development curves normalize these ratios;
missing factors are unavailable rather than zero and available weights are
renormalized only after a minimum-coverage gate. Valid negative profitability
is scored unchanged. Confidence and the top-level 15% weight are not applied,
and the result is not persisted by the v1 Phase 4C path.

Phase 4D-B implements a pure Cash-Flow Quality score from CFO/PAT,
CFO/EBITDA, receivable days, and trade-working-capital change divided by TTM
revenue. CFO ratios use identity signals. Receivable days are negated so lower
days rank higher on monotonic curves. Trade-WC change retains the Phase 3
`ending - beginning` accounting sign and exposes both `change / revenue` and
its negation as the scoring signal: builds score below equal-size releases.
Non-positive TTM revenue makes only that factor unavailable. Confidence and the
top-level 10% weight are not applied. The separate Phase 4D-D v2 path may
persist it from the selected coherent financial context.

Phase 4D-C implements a pure Balance Sheet score from net debt divided by TTM
reported EBITDA, debt/equity, and interest coverage. Absolute INR debt is not
scored. Net-debt/EBITDA and debt/equity are explicitly negated so lower
leverage scores higher on monotonic curves; interest coverage uses an identity
signal. Net cash and negative-EBIT coverage retain their approved Phase 3
semantics. Non-positive EBITDA or an undefined denominator makes the relevant
factor unavailable. Confidence and the top-level 10% weight are not applied,
and the v1 Phase 4C path does not persist the result.

Phase 4D-D adds a separate `score_snapshot_v2` path that can persist all four
approved financial components from one coherent provider/scope context. A
context needs only one scoreable positive-weight implemented component to be
selectable; configured provider-first priority then wins over score magnitude,
component count, and coverage. The selected context is never supplemented from
another source. Standard all-four coverage is `0.60`, not a partial score;
Business Catalyst, Valuation, Market Structure, and Low Market Attention remain
missing. `final_score`, final contributions, and confidence multiplication
remain absent.

## Component construction

### Financial inflection (25)

Sub-factors are revenue acceleration (30%), profit acceleration (25%), margin
expansion (20%), return-on-capital improvement (15%), and persistence/
consistency (10%). The Phase 4B development policy splits the last 10% equally
between consistency and persistence. It uses configurable normalization curves
and no confidence adjustment. The split and breakpoints are uncalibrated
development placeholders. Robust median/MAD outlier treatment remains future
work rather than hidden scoring behavior.

### Business catalyst (20)

Only source-linked catalyst records are used. Direction, evidence confidence,
recency decay, and materiality determine the score. When amount is known,
materiality includes `order_or_capex_value / latest available annual revenue`;
otherwise the event is visible but capped. Duplicate announcements and
unreviewed AI extraction cannot be counted twice.

### Quality, cash flow, and balance sheet (35 combined)

Phase 4D-A Business Quality currently evaluates only current ROCE level, ROE
level, and configured margin level. Multi-period ROCE/ROE or margin trends,
asset turnover, ROIC, sector-relative quality, and peer percentiles remain
deferred. Phase 4D-B Cash-Flow Quality currently scores CFO/PAT, CFO/EBITDA,
receivable days, and revenue-normalized trade-WC change. FCF, FCF trajectory,
CCC, inventory/payable days, additional accrual ratios, sector-relative cash
conversion, and multi-period working-capital trends remain deferred. Phase
4D-C Balance Sheet currently scores net debt/TTM reported EBITDA, debt/equity,
and interest coverage. Net-debt/debt/coverage trends, current and quick ratios,
maturity profiles, covenants, broader liquidity reserves, sector-relative
leverage, and peer percentiles remain deferred. Risk evidence can reduce future
components and a future final cap independently.

### Valuation (10)

Phase 3H-A implements only conservative, deterministic precursors:
`market_cap_to_ttm_pat`, `market_cap_to_total_equity`,
`market_cap_to_ttm_revenue`, `simplified_ev_to_ttm_ebitda`, and
`simplified_ev_to_ttm_revenue`. These names deliberately do not claim canonical
P/E or P/B attribution semantics or a complete enterprise-value bridge.
Non-positive denominators and non-positive simplified EV remain unavailable
with reasons rather than being misclassified as cheap.

Valuation scoring, curves, peer/sector and historical percentiles, and the
top-level 10% weight remain unimplemented pending a separate Phase 3H-B policy
review. Phase 3H-A features are not scores and make no cheap/expensive claim.

### Market structure and attention (10 combined)

Market structure blends relative strength versus benchmark and sector, trend
and moving-average structure, consolidation/volatility contraction, volume,
delivery, and liquidity. It is a small confirmation component, never an entry
signal. Attention blends available institutional holdings, coverage/news/search
proxies, and trading activity. Low attention scores only when business evidence
is adequate; it does not reward illiquidity or absent disclosure.

## Risk gates and confidence

Critical unresolved data-quality issues exclude a company. Configurable hard
gates include severe promoter pledge/governance evidence, going-concern style
signals, non-positive equity with high leverage, extreme illiquidity, and
unresolved corporate-action identity issues. Other risks reduce a named
component and create explicit negative explanations.

Confidence is separate from score. It combines data completeness, source
reliability, recency, comparable-history length, and evidence confidence. It is
not a probability of investment success. The UI always shows score, confidence,
data coverage, and active exclusion/cap reason together.

## Explanation contract

Each score component creates ranked explanation rows:

| Field | Example |
|---|---|
| Factor | `revenue_acceleration` |
| Contribution | `+6.2` score points |
| Direction | positive / negative / neutral |
| Confidence | 0.88 |
| Evidence | Q1 revenue YoY 38% versus preceding 3-quarter median 14%; filing link |

The explanation engine compares the current immutable score to the immediately
prior score under the same active configuration where possible. It reports new
catalysts, resolved/new risks, and score movement without claiming causality.

## Validation and evolution

Before activating any model version: unit-test every formula, test missing and
negative-value behavior, test revision/PIT fixtures, check sector-size bias,
and run historical decile, threshold, and sensitivity backtests. Backtests use
realistic rebalance timing, eligible historical universe, delisting handling,
corporate-action adjusted prices, and documented costs. A model change produces
a new `model_version`; old score and backtest outputs remain reproducible.
