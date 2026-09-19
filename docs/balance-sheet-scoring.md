# Phase 4D-C Balance Sheet scoring

## Scope and evidence

Balance Sheet is a pure component scorer over three controlled signals:

- net debt divided by TTM reported EBITDA;
- debt/equity;
- interest coverage.

It consumes the existing Phase 3E-B result objects and a PIT-clean
`ebitda_reported` TTM denominator. It does not change net debt, debt/equity, or
interest-coverage accounting semantics. The scorer performs no database or
provider reads, persistence, confidence adjustment, top-level weighting,
Phase 4C integration, or final Opportunity Score calculation.

## Scoring transformations

Absolute net debt in INR is retained but never scored directly. When TTM
reported EBITDA is positive:

```text
normalized_raw_value = net_debt / TTM EBITDA
scoring_value = -normalized_raw_value
transform_code = negate_net_debt_over_ttm_ebitda
```

The transform is size-neutral and makes higher leverage score lower on the
shared monotonic curve. Negative net debt is valid net cash and creates a
positive signal. Non-positive EBITDA makes only this subfactor unavailable;
the scorer does not divide, take an absolute value, substitute another
denominator, or score the INR balance.

Debt/equity uses `scoring_value = -value` with transform
`negate_debt_to_equity`. Lower leverage therefore scores higher. Undefined
ratios caused by non-positive equity remain unavailable. A supposedly defined
ratio with negative debt, non-positive equity, or a negative ratio is rejected
as structurally inconsistent rather than rewarded as exceptional leverage.

Interest coverage uses an identity transform. Negative coverage remains valid
when EBIT is negative and finance cost is valid positive. Coverage undefined
because finance cost is non-positive remains unavailable; `abs(finance_cost)`
is never used.

## Coherence and lineage

Every supplied object must match company, provider dataset, filing scope,
requested fiscal-year/quarter endpoint, and normalized UTC cutoff. All known
economic period ends must match. Net debt and debt/equity must additionally
share the exact stored `FiscalPeriod.id`, preventing stock measures from being
combined across distinct stored period semantics. The EBITDA denominator must
be `ebitda_reported`, INR, Decimal, and endpoint-coherent.

The net-debt subfactor retains an immutable pair of the full `NetDebtValue` and
TTM EBITDA objects. Its availability is the later of those inputs. Component
availability is the latest participating subfactor timestamp.

## Policy, coverage, and deferred work

The development weights are 0.40 for net debt/EBITDA, 0.30 for debt/equity,
and 0.30 for interest coverage. Versioned development curves reuse the Phase 4B
exact-Decimal interpolation and endpoint clamping implementation. They are
placeholders for mechanics validation, not calibrated thresholds or evidence
of predictive validity.

Missing factors are unavailable, never score zero. At or above minimum
coverage, available weights renormalize; below it, the score is undefined with
`insufficient_subfactor_coverage`. Zero-weight factors are excluded from
output, missing-factor reporting, coverage, score, and availability.

Net-debt/debt/coverage trends, current and quick ratios, maturity schedules,
covenants, broader liquidity reserves, sector normalization, and peer
percentiles remain deferred. Phase 4D-C does not apply confidence or the
top-level 10% Balance Sheet weight.

Phase 4D-D's v2 orchestration can persist the score from one coherently selected
provider/scope. Component detail and explanation manifests retain raw INR,
normalized leverage, transformed signal, and source lineage separately. The v1
path remains Financial-Inflection-only; no final contribution or final score is
produced.
