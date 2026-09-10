# Point-in-time period normalization

## Scope

Phase 3B derives only individual fiscal-quarter values that are mathematically
supported by point-in-time reported facts. It is a pure read layer: it never
updates `FinancialFact` rows or stores derived quarters. TTM, growth, ratios,
and financial features remain out of scope.

## Eligibility

Subtraction is permitted only when controlled metric metadata proves both:

```text
semantic_type == duration
unit_category == monetary
```

Both selected components must have a non-null `normalized_value` and the same
`normalized_unit` (currently exact `INR`). This excludes instant balance-sheet
metrics, ratios, percentages, shares, and `INR/share` EPS. No foreign-exchange
conversion or binary-float arithmetic is used.

An explicitly reported `quarter` is returned first, including a directly
reported per-share quarter. It is never replaced by a conflicting cumulative
difference.

## Approved derivations

Only these operations are allowed in version `period_normalization_v1`:

```text
Q2 = H1 YTD - Q1
Q3 = 9M YTD - H1 YTD
Q4 = annual - 9M YTD
```

The two inputs must share provider dataset, company, filing scope, metric,
fiscal year, fiscal-year `period_start`, normalized unit, and monotonically
increasing period ends. H1 and 9M must carry their expected fiscal-quarter
identities and `is_ytd = true`; annual is represented by `period_kind = annual`.
The derived period starts on the day after the predecessor end and ends on the
cumulative period end. No calendar convention is hardcoded.

For example, Q1 revenue of 100 crore and H1 revenue of 230 crore become a Q2
value of 1,300,000,000 normalized INR. A negative difference remains a valid
reported arithmetic result; it is not a quality judgement.

## Point-in-time and revisions

All components come from `PointInTimeFinancialReader` for the supplied
timezone-aware `as_of`. A derived value is therefore impossible until both
components are eligible. Its `available_at` is the maximum component
availability. A later H1 restatement changes derived Q2 only at and after the
restatement availability; historical cutoffs continue to select the old inputs.

Provider datasets and filing scopes are explicit on every request and are never
mixed. Missing or incompatible components return no value; the service never
zero-fills quarters or applies alternate algebra such as `9M - Q1 - Q2`.

## Lineage and interfaces

`FiscalQuarterNormalizer.quarter_as_of(...)` requires provider dataset, company,
scope, metric, fiscal year/quarter, and `as_of`. `quarter_series_as_of(...)`
uses the same explicit context and returns available values ordered by period
end, fiscal year, and fiscal quarter.

Each `QuarterizedFinancialValue` has `derivation_kind`, a structured
`operation`, algorithm version, normalized value/unit, source availability,
and structured lineage. Operation is one of `reported`, `h1_minus_q1`,
`nine_month_minus_h1`, or `annual_minus_nine_month`; callers therefore do not
have to infer the arithmetic from component periods. A derived result contains
`minuend` and `subtrahend` entries; a reported result contains a `reported`
entry. These lineage roles remain distinct from the operation. Each entry
retains the selected financial fact, filing, source-record identity, raw-object
key, and raw-payload locator.
