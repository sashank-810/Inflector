# Point-in-time financial-inflection primitives

Phase 3D is a read-only layer over `FiscalQuarterNormalizer`; it never reads raw
YTD facts or persists features. Every request names provider dataset, company,
scope, and a timezone-aware `as_of` cutoff. Providers and scopes are never
mixed or selected by fallback policy.

Growth compares only continuous stored quarters. QoQ uses one adjacent quarter;
YoY uses four exact adjacent transitions. Both require additive monetary INR
quarters. Positive comparison bases use `current / comparison - 1` as an exact
Decimal fraction. A zero or negative base yields exact `absolute_change` with a
transition (`loss_to_profit`, `loss_narrowing`, `loss_widening`,
`loss_unchanged`, `zero_base_positive`, `zero_base_negative`, or
`zero_base_zero`) rather than a misleading percentage. No near-zero materiality
threshold is hardcoded; that is a later model-policy decision.

Acceleration is `current YoY - median(previous three YoY values)`, using only
four consecutive percentage-mode YoY observations. The middle of three sorted
Decimal values is the median. `growth_v1` and `growth_acceleration_v1` retain
the selected quarters/growth observations and maximum source availability.

Operating margin is `operating_profit / revenue`; EBITDA margin is
`ebitda_reported / revenue`. Both operands must be same-period additive INR
quarters and revenue must be positive. YoY margin expansion is current margin
minus the continuously comparable prior-year margin; basis points are the exact
fraction delta times 10,000. Negative profit margins and contractions remain
valid arithmetic. `margin_v1` and `margin_expansion_v1` retain nested quarter
and raw-source lineage. Scores, confidence, quality ratios, and persistence
policies remain out of scope.
