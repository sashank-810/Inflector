# Market Structure component scoring

Phase 4D-F is a pure, development-only 0–100 scorer over the immutable
`market_structure_feature_bundle_v1` produced by Phase 3G-C. Phase 3G-C owns
point-in-time selection and all feature mathematics. Phase 4D-F only validates
the bundle and nested lineage, applies an explicit identity or negation,
evaluates versioned piecewise-linear curves, gates coverage, and renormalizes
available positive weights. It does not recompute returns, moving averages,
volatility, range, activity, delivery, or relative strength.

This uncalibrated score is confirmation evidence for research prioritization.
It is not a trading signal, entry timing, recommendation, alpha claim, momentum
forecast, or prediction. Historical calibration and backtesting remain future
work.

## Scored primitives and development policy

| Primitive | Weight | Transform | Development curve `(signal, score)` |
|---|---:|---|---|
| `relative_strength_60_to_benchmark` | 0.30 | identity | `(-0.30,0), (-0.15,20), (0,50), (0.10,65), (0.20,80), (0.40,100)` |
| `close_to_sma20` | 0.10 | identity | `(-0.20,0), (-0.10,20), (0,50), (0.05,70), (0.10,85), (0.20,100)` |
| `sma20_to_sma60` | 0.15 | identity | `(-0.15,0), (-0.05,20), (0,50), (0.05,70), (0.10,85), (0.20,100)` |
| `volatility_ratio_20_to_60` | 0.15 | negate | `(-2,0), (-1.5,20), (-1,50), (-0.8,70), (-0.6,85), (-0.4,100), (0,100)` |
| `consolidation_range_20` | 0.10 | negate | `(-0.50,0), (-0.30,20), (-0.20,50), (-0.12,70), (-0.08,85), (-0.04,100), (0,100)` |
| `close_times_volume_ratio_20_to_60` | 0.10 | identity | `(0,0), (0.5,10), (0.8,30), (1,50), (1.2,70), (1.5,85), (2,100)` |
| `average_delivery_percentage_20` | 0.10 | identity | `(0,0), (0.2,20), (0.4,50), (0.6,75), (0.8,90), (1,100)` |

The lower-is-better transforms are exactly
`negate_volatility_ratio_20_to_60` and
`negate_consolidation_range_20`; the other five use `identity`. The common
piecewise-linear algorithm clamps at curve endpoints. Close/SMA20 intentionally
has no overextension penalty in this version.

The minimum configured-weight coverage is `0.70`. Missing positive-weight
factors are not zero-filled. When coverage passes, each available factor gets
`configured_weight / available_weight`; below the floor the component score is
`None` and its audit remains visible. Missing benchmark relative strength alone
leaves `0.70` and can score. Missing relative strength and delivery leaves
`0.60` and cannot. Zero-weight factors are excluded from score, missing codes,
coverage, and component availability.

## Intentionally unscored evidence

`return_volatility_20`, `return_volatility_60`, and
`average_close_times_volume_20_inr` remain supporting evidence. Absolute
volatility is regime- and sector-dependent; only its ratio is scored. Absolute
close-times-volume is size-dependent and is not a cross-company reward. These
three affect neither coverage, missing codes, availability, nor score. The
activity proxy is not wired to eligibility.

Delivery is only a development participation proxy. High delivery does not
imply accumulation, institutional interest, or Low Market Attention. No such
label or prose inference is produced.

## Identity, cutoff, and lineage

The scorer requires exact bundle/feature versions and codes, ratio units,
Decimal values in feature-specific domains, aware cutoff-coherent timestamps,
coherent provider/security/interval identities, and one basis date. Adjusted
evidence must use `adjusted_market_price_v1`, the bundle basis, and declared
corporate-action provider. Relative strength retains the bundle's exact
benchmark provider and code; no sector benchmark or `Company.sector` inference
is allowed. Accepted market, action, and benchmark source lineage remains
recursively reachable through retained feature objects.

Corrections are handled upstream. Market corrections change scores only where
feature evidence changes; action corrections can change adjusted-price factors
without mechanically changing raw activity or delivery; benchmark endpoint
corrections change only relative strength; and delivery-only revisions change
only delivery values when price and volume are unchanged. Earlier cutoffs are
reproducible. The scorer performs no PIT selection.

## Separation from orchestration

There is no confidence input, freshness penalty, or multiplication by the
reserved top-level 5%. The scorer creates no database record or migration and
is absent from `score_snapshot_v1` and `score_snapshot_v2`. Persisted v2 maximum
standard coverage remains `0.60`, although pure component scorers exist for
top-level weight `0.75`. `final_score` remains `NULL`.

RSI, MACD, breakout, support, resistance, overbought/oversold, sector-relative
strength, Business Catalyst, and Low Market Attention are outside Phase 4D-F.
Cross-domain orchestration for Valuation and Market Structure requires a
separate review after acceptance.
