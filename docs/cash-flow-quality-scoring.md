# Phase 4D-B Cash-Flow Quality scoring

## Scope and accounting boundary

Cash-Flow Quality is a pure component scorer over four approved Phase 3E-C
results: CFO/PAT conversion, CFO/EBITDA conversion, receivable days, and trade
working-capital change. Phase 3 retains accounting facts and defines trade-WC
change as ending trade working capital minus beginning trade working capital.
Phase 4D-B does not modify that sign; it retains the accounting value and adds
an explicit scoring signal for interpretation.

The scorer performs no provider queries, persistence, confidence adjustment,
top-level weighting, Phase 4C orchestration, or final Opportunity Score.

## Factors and explicit transforms

| Factor | Accounting raw value | Scoring signal | Transform |
|---|---|---|---|
| CFO/PAT | conversion ratio | same ratio | `identity` |
| CFO/EBITDA | conversion ratio | same ratio | `identity` |
| Receivable days | days | negative days | `negate_receivable_days` |
| Trade-WC burden | INR change and change/TTM revenue | negative change/TTM revenue | `negate_twc_change_over_ttm_revenue` |

Receivable days are negated because the approved generic curve is monotonic:
30 days becomes -30 and therefore scores above 120 days at -120. The scorer
does not use a reciprocal or divide days by 365 again.

Absolute INR trade-WC change is not scored because it is size-dependent. The
scorer first calculates `change / TTM revenue`, then negates it. A positive
accounting change is a build that ties up more operating capital and produces a
negative scoring signal. A negative change is a release and produces a positive
signal. The original INR value and normalized accounting ratio remain visible
beside the transformed signal. Non-positive TTM revenue makes this factor
unavailable; the scorer does not divide, take an absolute value, substitute a
denominator, or assign zero.

Negative CFO ratios remain valid when Phase 3 had a valid positive denominator.
Undefined CFO ratios, undefined receivable days, and invalid-component trade-WC
results remain unavailable rather than becoming zero. Negative receivable days
are structurally inconsistent with the approved Phase 3 formula and are
rejected. Negative trade-WC change is valid.

## Curves, weights, and missing evidence

The development policy uses weights 0.40, 0.30, 0.15, and 0.15 in canonical
factor order. Every normalization curve is versioned policy and uses the shared
exact-Decimal Phase 4B interpolation and endpoint clamping implementation. The
fixture values are development placeholders for mechanics validation, not
calibrated production thresholds or evidence of predictive power.

Available weight is the sum of positive-weight factors with usable evidence.
Below the configured minimum, the component score is undefined with
`insufficient_subfactor_coverage`. Otherwise:

```text
effective_weight = configured_weight / available_weight
contribution = normalized_score * effective_weight
component_score = sum(contributions)
```

Missing factors are disclosed and never represented as normalized zero.
Zero-weight factors are excluded from output, missing-factor reporting,
coverage, score, and `available_at`. Availability is the maximum PIT timestamp
among participating factors. Context, endpoint, and normalized UTC cutoff must
match exactly across all supplied evidence.

## Deferred work

FCF and capex-sign normalization, inventory days, payable days, cash conversion
cycle, cash-flow and working-capital trends, additional accrual ratios, peer or
sector normalization, and curve calibration remain deferred. Phase 4D-B does
not persist Cash-Flow Quality in score snapshots and does not apply confidence
or the top-level 10% component weight.
