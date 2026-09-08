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
each economic item. Financial features use consolidated values by default and
declare a fallback to standalone. A later restatement cannot revise a past
score. Scores are not produced for records failing material quality or
eligibility checks.

Minimum eligibility for v1.0: an active common-equity listing, a configurable
liquidity floor, 8 comparable quarters (or a declared new-listing exception),
and enough reported inputs to calculate a defined subset of the financial core.
Insufficient history, suspended/delisted status, invalid units, and unresolved
critical quality issues cause exclusion or reduced confidence, never a zero
risk score.

## Component model

The final score is a weighted mean of available, confidence-adjusted component
scores, on a 0–100 scale. The denominator is the sum of weights for eligible
components; missing components are disclosed and lower final confidence. A hard
risk gate can cap or exclude a score independently of the weighted sum.

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
in `scoring_configuration`; weights are never embedded in application code.

## Deterministic feature definitions

All period metrics use normalized INR figures and source lineage. Let `R_t`
mean revenue for the current quarter and `P_t` PAT.

| Feature | Formula / policy |
|---|---|
| Revenue YoY | `(R_t / R_t-4) - 1`, only for comparable quarterly periods |
| Revenue QoQ | `(R_t / R_t-1) - 1`; displayed but seasonality-aware and lower weight |
| PAT YoY / QoQ | Corresponding formula on PAT; losses and near-zero denominators use signed absolute-change rules and a warning |
| Growth acceleration | current YoY growth minus trailing median of the prior 3 comparable YoY growth observations |
| Growth consistency | share of last 4 comparable periods with positive growth, adjusted for data quality |
| Persistence | number of consecutive periods above threshold, capped; one quarter cannot create a high score |
| Margin expansion | current operating/EBITDA margin minus comparable prior-year margin, in basis points |
| ROE | TTM PAT / average beginning-and-ending equity |
| ROCE | TTM EBIT / average capital employed, where capital employed = equity + interest-bearing debt − cash; definition/version is stored |
| ROIC | NOPAT / average invested capital, only when tax and invested-capital inputs are adequate |
| Net debt | interest-bearing debt − cash and cash equivalents |
| Debt/equity | interest-bearing debt / equity; flagged if equity is non-positive |
| Interest coverage | EBIT / interest expense; undefined values are flagged, not forced high |
| CFO conversion | TTM CFO / TTM PAT, with loss-aware interpretation |
| FCF | CFO − capex; capex sign is normalized by metric definition |
| Receivable days | average receivables / TTM revenue × 365, where inputs permit |
| Cash conversion cycle | receivable days + inventory days − payable days |
| Enterprise value | market cap + debt + preferred/minority interests where available − cash |

Trailing periods are calculated only from observations available at the cutoff.
Winsorisation, sector comparisons, minimum denominators, and score breakpoints
are configuration values and carry their own version.

## Component construction

### Financial inflection (25)

Sub-factors are revenue acceleration (30%), profit acceleration (25%), margin
expansion (20%), return-on-capital improvement (15%), and persistence/
consistency (10%). Each score combines magnitude, trend slope, acceleration,
and persistence over 4–8 quarters. A single extreme period is downweighted
through robust median/MAD outlier detection and cannot contribute more than a
configured fraction of the component.

### Business catalyst (20)

Only source-linked catalyst records are used. Direction, evidence confidence,
recency decay, and materiality determine the score. When amount is known,
materiality includes `order_or_capex_value / latest available annual revenue`;
otherwise the event is visible but capped. Duplicate announcements and
unreviewed AI extraction cannot be counted twice.

### Quality, cash flow, and balance sheet (35 combined)

Business quality evaluates sustainable margins, ROCE/ROE trend, and asset
turnover. Cash-flow quality evaluates CFO/PAT, CFO/EBITDA, FCF trajectory, and
accrual/working-capital consistency. Balance sheet evaluates net-debt trend,
debt/equity, interest coverage, current/quick ratios, and working-capital
movement. Risk evidence can reduce these components and the final cap applies
independently.

### Valuation (10)

PE, PB, EV/EBITDA, EV/sales, market-cap/sales, and historical/sector percentiles
are conditional features. Valuation is judged jointly with growth, profitability
and capital efficiency; low PE alone cannot score highly. Negative or undefined
ratios are shown with a reason and excluded from percentile ranking rather than
misclassified as cheap.

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
