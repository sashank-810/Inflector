# Immutable partial score snapshots

## Scope

The Phase 4C `score_snapshot_v1` path persists the reproducible audit of one scoring attempt. It resolves an
active persisted policy, evaluates eligibility and confidence, selects one
coherent financial context, calculates the approved Financial Inflection
component, and stores structured component and subfactor records.

Only one of eight proposed top-level components is supported by the v1
orchestration contract. Consequently every v1 `final_score` is SQL
`NULL`. The Financial Inflection score is not
multiplied by its 25% top-level weight, divided by that weight, renormalized to
stand in for missing components, or adjusted by confidence.

Phase 4D-D adds a separate `score_snapshot_v2` method on the orchestrator. It
can persist Financial Inflection, Business Quality, Cash-Flow Quality, and
Balance Sheet from one selected provider/scope context. It does not change the
v1 method, algorithm identity, statuses, component set, or exact-rerun
fingerprints. See
[`financial-component-orchestration.md`](financial-component-orchestration.md).

## Statuses

The v1 statuses are:

- `ineligible`: eligibility failed; confidence and reasons are stored but no
  component or explanation exists;
- `financial_inflection_unavailable`: eligible, but no configured context met
  the Financial Inflection subfactor-coverage gate;
- `partial_component_set`: Financial Inflection is available and persisted,
  while `final_score` remains `NULL`.

There is no complete status in this phase.

V2 retains `ineligible` and `partial_component_set`, and replaces the
v1-specific unavailable status with `financial_components_unavailable`. This
means no configured supplied context produced any scoreable positive-weight
implemented financial component. Both versions require `final_score = NULL`;
repository validation rejects cross-version statuses and component codes.

## Context selection and evaluation order

The orchestrator resolves the active configuration at the UTC knowledge cutoff,
then evaluates eligibility and confidence. Ineligible attempts never invoke the
component scorer. Eligible candidates are scored independently within one
provider/scope pair. Only scoreable candidate keys are passed to the Phase 4A
provider-first lexicographic resolver.

Selection never uses score magnitude or greater evidence coverage once a
preferred context meets minimum coverage. Lightweight attempt metadata records
coverage, scoreability, warnings, and the selected fallback reason. Evidence is
never combined across contexts.

## Partial component coverage

The canonical top-level component order is Financial Inflection, Business
Catalyst, Business Quality, Cash-flow Quality, Balance Sheet, Valuation, Market
Structure, and Low Market Attention. When Financial Inflection is available,
top-level coverage equals its configured top-level weight (normally `0.25`),
not one. Positive-weight unavailable components are listed as missing in that
order. Zero-weight components are omitted from the missing list.

`score_components.score` retains the Phase 4B 0–100 score.
`configured_top_level_weight` is audit metadata and `final_contribution` is
`NULL`. Snapshot confidence is stored independently and changes neither the
component nor any subfactor.

## Immutability and idempotency

Snapshots, components, and explanations have create/read/history repository
operations and no generic update operation. An exact rerun returns the existing
snapshot by fingerprint without inserting children. A recalculation with new
historically available evidence produces a new fingerprint and new immutable
snapshot—even for the same company, configuration, endpoint, and cutoff. Both
calculations remain in history; insertion order does not declare one uniquely
correct.

History is ordered by knowledge cutoff descending, creation time descending,
then UUID as a deterministic tie-break.

## Fingerprint and exact serialization

The SHA-256 fingerprint covers snapshot/model/configuration identity,
configuration checksum, cutoff and endpoint, eligibility inputs/results,
confidence inputs/results, context attempts/selection, component coverage,
component/subfactor summaries, and the input lineage manifest. Generated IDs
and creation timestamps are excluded.

Canonical audit JSON sorts mapping keys; normalizes UUIDs, dates, and aware UTC
datetimes; uses semantic Decimal strings; retains sequence order; and rejects
binary floats and naive timestamps. Repository reads recompute the fingerprint
and raise `ScoreSnapshotIntegrityError` on corruption. SQLite stores audit
Decimals as exact text; PostgreSQL uses wide `NUMERIC(50,28)`.

## Components and explanations

V1 persists at most one `financial_inflection` component. Its detail JSON
contains canonical subfactor summaries in Phase 4B order. One explanation row
is written for each available subfactor—never for missing evidence. Rows retain
raw value/unit, normalized score, configured/effective weights, component
contribution, knowledge time, evidence type, and curve version.

Explanations rank by contribution descending, with Phase 4B subfactor order as
the tie-break. Template code is `financial_inflection_subfactor_v1`; direction
is deliberately `NULL`. These rows explain mathematics and do not generate
investment prose or recommendations.

## Lineage and PIT reproducibility

The orchestrator recursively traverses the immutable Phase 3 evidence retained
by Phase 4B. Each subfactor manifest contains only its supporting facts. The
snapshot manifest is their deduplicated union, sorted by financial-fact UUID.
Each fact records financial fact/source record IDs, external record ID, raw
object key, raw payload locator, metric code, and `available_at`, together with
component, curve, and Phase 3 algorithm versions.

Restatements affect snapshots only at their PIT availability boundary. Earlier
snapshots remain unchanged, and a historical rerun reproduces the earlier
fingerprint when its cutoff and evidence remain identical.

V2 generalizes the same persistence tables and recursive lineage contract to
four financial components. It persists selected components in canonical
financial order, ranks explanations independently within each component, and
stores Cash-Flow/Balance-Sheet transforms in each explanation evidence
manifest. The snapshot manifest includes only selected-context component
lineage; non-selected contexts contribute lightweight scoreability audit data,
not full facts or score magnitudes. Standard all-four top-level coverage is
`0.60`, with no renormalization, final contribution, final score, or confidence
multiplication.
