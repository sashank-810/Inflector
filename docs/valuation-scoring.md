# Valuation component scoring

Phase 4D-E is a pure 0–100 component scorer over the approved Phase 3H-A
valuation feature bundle. Phase 3H-A owns PIT selection and deterministic
financial arithmetic. Phase 4D-E validates that evidence contract, negates
valid multiples into lower-is-better scoring signals, applies configured
curves, and handles coverage. It never recomputes a valuation multiple.

## Factors and transforms

The canonical factor order is:

1. `market_cap_to_ttm_pat` — weight `0.30`;
2. `market_cap_to_total_equity` — weight `0.15`;
3. `market_cap_to_ttm_revenue` — weight `0.10`;
4. `simplified_ev_to_ttm_ebitda` — weight `0.30`;
5. `simplified_ev_to_ttm_revenue` — weight `0.15`.

Each available feature is a positive ratio. Its explicit scoring signal is the
negative of that ratio, with a factor-specific `negate_*` transform code. This
allows the common non-decreasing piecewise-linear curve implementation to give
lower multiples higher scores without supporting descending curves.

The absolute curves and weights are development placeholders. They are not
calibrated production thresholds, historical evidence of predictive power, or
claims about intrinsic value. Absolute curves are likely to exhibit sector
bias and require calibration/backtesting before production interpretation.

## Availability and coverage

A factor participates only when its Phase 3H-A feature has the expected code
and version, an exact positive `Decimal` value, `ratio` unit, no warnings, a
timezone-aware availability timestamp no later than the shared cutoff, and a
positive configured weight. Unavailable positive-weight factors retain their
original feature object and warning tuple for audit; they are never zero-filled.

The minimum coverage is `0.70`. Available weights are renormalized only after
that gate passes. Missing PAT alone or EV/EBITDA alone leaves exactly `0.70`
coverage and can score. Missing both leaves `0.40` and cannot score. Zero-weight
factors are excluded from output, coverage, score, missing audit, and component
availability.

Non-positive PAT, equity, revenue, or EBITDA semantics remain owned by Phase
3H-A and arrive as unavailable features. A non-positive simplified enterprise
value is retained by Phase 3H-A, but both simplified-EV multiples are
unavailable; the scorer never rewards negative EV as cheap.

## Accounting and orchestration boundaries

The conservative names remain unchanged: this is not a canonical P/E or P/B
claim, and `simplified_enterprise_value` is not complete enterprise value. The
scorer has no peer/sector percentile, own-history percentile, quality/growth
overlay, confidence adjustment, recommendation prose, or top-level 10%
application.

Results are in-memory only. Phase 4D-E does not write score snapshots,
components, or explanations. `score_snapshot_v2` remains the four-financial-
component contract with maximum standard persisted coverage `0.60`; Valuation
needs an explicit market-provider context and is not integrated. Existing
snapshot `final_score` and component final contributions remain `NULL`.
