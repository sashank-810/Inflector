# Point-in-time instant financial snapshots

## Purpose

Phase 3E-A provides a read-only foundation for balance-sheet-derived features.
It answers: for an explicit provider dataset, company, filing scope, and
knowledge cutoff, which requested instant monetary facts coexist in one exact
stored fiscal period? It does not calculate leverage, returns, working-capital
ratios, cash-flow quality, or scores.

The implementation is `InstantFinancialSnapshotReader` in `packages/data`. It
uses `PointInTimeFinancialReader` for every component, creates no database
tables, and does not persist its results. The algorithm version is
`instant_snapshot_v1`.

## Instant versus duration semantics

A snapshot component is eligible only when its controlled metric definition
has `semantic_type == "instant"` and `unit_category == "monetary"`. Its
normalized value must be a non-null `Decimal` and its normalized unit must be
exactly `INR`.

This admits balance-sheet amounts such as debt, cash, equity, inventory,
receivables, and payables. It excludes revenue, profit, cash-flow duration
facts, EPS/per-share facts, ratios, foreign or unnormalized values, and any
fact whose metadata does not prove eligibility. Phase 3E-A performs no FX
conversion.

## Same-period identity

Every component of a snapshot must share the same stored `FiscalPeriod.id`.
Matching only fiscal year, fiscal quarter, or period end is insufficient
because different stored periods may encode different reporting semantics.

`snapshot_for_period_as_of(...)` is the exact-period primitive. It returns a
snapshot only when every requested metric resolves to an eligible fact for the
requested period ID and context. Missing or ineligible components make the
whole request unavailable; values from another period are never substituted.

Metric requests must be non-empty and contain no duplicates. An aware `as_of`
timestamp is required and normalized to UTC.

## Latest complete common period

`latest_common_snapshot_as_of(...)` obtains the PIT-visible eligible period
IDs for each requested metric, then intersects those sets. It selects the
common period with the greatest economic `period_end`.

It never selects each metric independently. If Q2 debt is visible but cash is
available only for Q1, a debt-and-cash request returns the complete Q1
snapshot. If no period contains every requested component, it returns `None`.

Economic recency is not knowledge recency. A late filing or restatement for an
old period cannot displace a newer complete economic period merely because it
has a later `available_at`, `revision_at`, or `ingested_at`.

If multiple distinct common `FiscalPeriod.id` values share the maximum
`period_end`, the method returns `None`. Phase 3E-A has no rule preferring a
quarter, annual period, availability time, or ingestion order, so ambiguous
stored-period semantics fail closed.

## Point-in-time and restatement behavior

Only facts with `available_at <= as_of` can participate. Revision selection is
delegated entirely to `PointInTimeFinancialReader`:

- before a restatement is available, the original fact is selected;
- exactly at the restatement's availability, the restated fact is selected;
- re-querying the earlier cutoff still returns the original immutable fact.

`ingested_at` is never used as the knowledge boundary. A snapshot's
`available_at` is the maximum availability time of its selected component
facts: the first instant when the complete snapshot was knowable.

## Isolation and lineage

Provider dataset and filing scope are explicit on every call. Components must
match the requested provider dataset, company, and standalone/consolidated
scope. There is no provider reconciliation, provider fallback, scope fallback,
or consolidated-first policy.

Each `InstantSnapshotComponent` retains the selected
`PointInTimeFinancialFact`. That object carries the filing, fiscal period,
source record, raw object key, raw payload locator, and immutable fact ID. The
snapshot adds no reconstructed or flattened provenance.

## Future consumers

Later Phase 3E work can safely request exact same-period pairs such as
`(total_debt, cash_and_equivalents)` for net debt or
`(total_debt, total_equity)` for debt/equity. Beginning/ending-period capital
efficiency calculations can request exact period IDs independently. Those
calculations and all selection preferences remain outside Phase 3E-A.
