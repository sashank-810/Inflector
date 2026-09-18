# Phase 4B financial-inflection component scoring

## Scope and validity

Phase 4B is a pure, in-memory transformation of coherent Phase 3 evidence into
one Financial Inflection component score on a 0–100 scale. It is a development
normalization policy for validating scoring mechanics, not an investment
prediction. The committed breakpoints are uncalibrated placeholders and
require historical calibration and backtesting before activation.

Phase 4B does not calculate the final opportunity score, apply the top-level
25% Financial Inflection weight, multiply by confidence, rank companies, or
persist feature/score/explanation rows.

## Configurable normalization

Every raw subfactor is normalized by a versioned piecewise-linear curve. Each
curve has at least two strictly increasing Decimal raw values and non-decreasing
Decimal scores in `[0, 100]`. Values below or above the curve clamp to its first
or last score. Between `(x0, y0)` and `(x1, y1)`:

```text
score = y0 + (raw - x0) / (x1 - x0) * (y1 - y0)
```

Arithmetic is exact Decimal arithmetic without internal rounding or float
conversion. Flat score segments are valid. The curve algorithm is identified
as `piecewise_linear_v1`.

## Six subfactors

The canonical order and raw evidence are:

1. `revenue_acceleration`: Phase 3D revenue acceleration, ratio;
2. `pat_acceleration`: Phase 3D PAT acceleration, ratio;
3. `margin_expansion`: configured operating/EBITDA expansion, basis points;
4. `roce_improvement`: current ROCE minus prior-year same-quarter ROCE, ratio delta;
5. `growth_consistency`: configured-metric positive share, ratio;
6. `growth_persistence`: streak count divided by configured window size, ratio.

The configured margin code is never substituted. The configured growth-history
metric, consistency window, persistence window, and persistence threshold must
exactly match the supplied Phase 3F results. Persistence saturation remains
lineage metadata and receives no hidden bonus. Undefined ROCE and absent Phase
3 acceleration are unavailable rather than zero.

## Coherence and lineage

All supplied evidence must match the selected provider dataset, filing scope,
company, normalized UTC knowledge cutoff, and requested ending fiscal quarter.
Prior ROCE must end at the same fiscal quarter exactly one fiscal year earlier.
Conflicts raise `ValueError`; the scorer does not perform fallback or repair.

Every subfactor retains the original immutable Phase 3 result. ROCE improvement
retains both current and prior-year ROCE objects. `available_at` is the maximum
knowledge time of the available subfactor evidence; ROCE uses the maximum of
its two inputs.

## Missing evidence and weight coverage

Missing evidence never becomes a zero score. Positive configured weights for
available subfactors are summed as `weight_coverage`. Zero-weight subfactors do
not affect coverage and do not require evidence. Below configured minimum
coverage, the component score is `None` with
`insufficient_subfactor_coverage`.

At or above the minimum, each available weight is renormalized:

```text
effective_weight = configured_weight / available_weight
contribution = normalized_score * effective_weight
component_score = sum(contribution)
```

Contributions therefore sum exactly to the 0–100 component score. Missing
subfactors and outputs use canonical deterministic order.

## Policy compatibility

The Phase 4B scoring subsection is optional under the existing
`financial_inflection` policy. When absent, canonical serialization omits only
that new key. Historical Phase 4A JSON, including legitimate `null` values in
other sections, retains its exact canonical representation and SHA-256.
Policies without the subsection remain valid but cannot be used by the Phase
4B scorer.

