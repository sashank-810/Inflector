# Phase 4D-A Business Quality scoring

## Scope

Business Quality measures current quality **levels** from three existing,
PIT-clean Phase 3 results: ROCE, ROE, and one explicitly configured quarterly
margin. It is separate from Financial Inflection, which measures change through
ROCE improvement, margin expansion, acceleration, consistency, and persistence.

Phase 4D-A is a pure in-memory scorer. It does not query providers, apply
confidence, apply the top-level Business Quality weight, persist a component,
change Phase 4C orchestration, or calculate an Opportunity Score.

## Policy and normalization

The optional `business_quality` policy contains exact-Decimal subfactor
weights, minimum available-weight coverage, an explicit `operating_margin` or
`ebitda_margin` choice, and a piecewise-linear curve for each level. It reuses
the Phase 4B interpolation algorithm: values between breakpoints interpolate
exactly and values outside the curve clamp to its endpoint scores.

The development fixture uses weights of 0.50 for ROCE, 0.30 for ROE, and 0.20
for margin. Its shared example curve from -0.10 through 0.30 is a development
normalization curve for validating mechanics. It is not calibrated, optimal,
production-ready, or evidence of predictive validity.

Legacy Phase 4A and 4B configurations need not contain this section. Canonical
serialization omits only an absent `business_quality` key, preserving their
historical JSON and checksums.

## Inputs and units

- `roce_level` uses the current `ReturnOnCapitalEmployedValue.value` ratio.
- `roe_level` uses the current `ReturnOnEquityValue.value` ratio.
- `margin_level` uses the current `QuarterMarginValue.value` ratio and must
  match the configured margin code.

Negative values are valid when Phase 3 produced them from a valid positive
denominator; they are passed unchanged through the configured curve. An ROCE or
ROE object whose value is undefined because its denominator was non-positive is
unavailable, not a zero score. The scorer does not take absolute values or
convert ratios such as `0.20` into percentage points.

Every supplied feature must match the requested company, provider dataset,
filing scope, fiscal-year/quarter endpoint, and knowledge cutoff. Aware
timestamps are normalized to UTC; incoherent or naive inputs fail explicitly.

## Missing factors and availability

Available weight is the sum of positive configured weights with usable
evidence. Below the configured minimum, the score is undefined and carries
`insufficient_subfactor_coverage`. At or above the minimum, participating
weights are renormalized:

```text
effective_weight = configured_weight / available_weight
contribution = normalized_score * effective_weight
component_score = sum(contributions)
```

Missing evidence is disclosed and never manufactured as a normalized zero.
Zero-weight factors are excluded from output, coverage, missing-factor lists,
the score, and `available_at`. Component availability is the maximum PIT
availability of participating evidence.

## Deferred quality signals

Asset turnover, ROIC, NOPAT/tax inference, multi-period ROCE or ROE trends,
margin trends, peer percentiles, and sector normalization are not implemented.
They require separately approved deterministic primitives and policy.
Phase 4D-A also adds no Business Quality snapshot persistence or orchestration;
Phase 4C continues to persist Financial Inflection only with a null final score.
