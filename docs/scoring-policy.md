# Phase 4 scoring policy and control contracts

## Scope

Phase 4A establishes immutable model/configuration identity and pure policy
contracts. It does not calculate component points, a final opportunity score,
rankings, explanations, or score snapshots.

Phase 4B extends only the Financial Inflection policy with an optional scoring
subsection. Absence is valid for historical Phase 4A configurations and the
canonical serializer omits that key only when absent, preserving their exact
JSON and checksum. New configurations can carry subfactor weights, a minimum
coverage threshold, explicit margin/history metrics, and six normalization
curves. See
[`financial-inflection-scoring.md`](financial-inflection-scoring.md).

Phase 4D-A adds an optional top-level `business_quality` scoring section.
Historical policies remain valid, and canonical serialization omits that key
only when absent so Phase 4A/4B JSON and checksums remain unchanged. The new
section configures current ROCE, ROE, and margin-level weights, minimum
coverage, margin identity, and reusable piecewise-linear curves. See
[`business-quality-scoring.md`](business-quality-scoring.md).

Phase 4D-B similarly adds an optional top-level `cash_flow_quality` section.
The narrowly scoped canonical serializer removes only an absent key, preserving
all Phase 4A, 4B, and 4D-A checksums. The section configures four subfactor
weights, minimum coverage, and shared piecewise-linear curves; interpretation
transforms remain versioned scorer semantics rather than hidden mutations of
accounting data. See
[`cash-flow-quality-scoring.md`](cash-flow-quality-scoring.md).

## Model version and scoring configuration

`model_versions` identifies code/model semantics by unique
`(model_family, semantic_version)`, with a Git SHA and application-validated
`draft`, `active`, or `retired` status. A semantic change creates a new row.

`scoring_configurations` identifies one immutable policy version belonging to a
model version. The unique identity is model version, configuration name, and
configuration version. A policy change creates a new row; the repository has no
generic update operation for policy JSON, checksum, model identity, or version.
The JSON contains no secrets or run-specific timestamps/IDs.

Active resolution requires both model and configuration status to be `active`
and uses the half-open interval `[effective_from, effective_to)`. A missing
bound is open. Zero matches returns `None`. More than one match raises
`AmbiguousActiveConfigurationError`; creation time, version strings, UUIDs, and
insertion order never break an overlap.

## Canonical policy and checksum

Persisted JSON is validated as `InflectionScoringPolicy`, whose frozen sections
are financial context, eligibility, confidence, component weights, and
financial-inflection history settings. Arbitrary dictionaries are not exposed
to policy consumers.

Canonical serialization recursively uses sorted JSON object keys, compact JSON
separators, UUID strings, ordered arrays for tuples, and normalized Decimal
strings. Decimal trailing zeros are removed, with every numeric zero encoded as
`"0"`; therefore `Decimal("0.10")` and `Decimal("0.1")` are semantically
identical. No float conversion occurs. SHA-256 is computed over UTF-8 canonical
JSON and stored as lowercase 64-character hexadecimal text. Database IDs and
effective timestamps are outside the checksum.

## Financial context selection

Phase 3 readers remain explicit and never mix contexts. Phase 4A's pure
`FinancialContextPolicyResolver` accepts complete candidates and searches
lexicographically, with provider priority outermost:

```text
for provider in provider_dataset_priority:
    for scope in filing_scope_priority:
        select the first available exact pair
```

For providers `(P1, P2)` and scopes `(consolidated, standalone)`, the order is
P1 consolidated, P1 standalone, P2 consolidated, P2 standalone. Thus provider
priority dominates scope preference. Unconfigured providers and unsupported
scopes are ignored. The result records both priority indices, whether fallback
occurred, and a deterministic reason. It never combines features across pairs.

## Eligibility

Eligibility is a pure rule evaluation over supplied evidence. Hard reasons are
ordered deterministically:

1. `unsupported_security_type`;
2. `inactive_security`;
3. `inactive_listing`;
4. `critical_data_quality_issue`;
5. `insufficient_comparable_history`;
6. `insufficient_financial_core_coverage`;
7. `missing_liquidity_evidence` or `below_liquidity_floor`.

Coverage is `available / required`, using exact Decimal arithmetic. Required
must be positive and available must lie between zero and required. Equality at
the coverage or liquidity minimum passes. When no liquidity floor is
configured, missing liquidity evidence has no effect.

A configured new-listing exception suppresses only the history failure and
adds `new_listing_history_exception`; it does not invent a lower alternate
threshold. A critical quality issue always makes the result ineligible.
Eligibility has no score or numerical penalty.

## Confidence

Confidence is independent of eligibility and is always a Decimal in `[0, 1]`:

```text
confidence = completeness * completeness_weight
           + source_reliability * source_reliability_weight
           + recency * recency_weight
           + history * history_weight
           + evidence * evidence_weight
```

Weights are non-negative and must sum exactly to one. Completeness is available
features divided by required features. History is comparable observations
divided by the configured maximum and capped at one. Source reliability and
evidence confidence are supplied values in `[0, 1]`; Phase 4A does not infer
provider ratings.

Recency uses exact elapsed seconds, including partial days:

```text
age_days = elapsed_seconds / 86400
recency = max(0, 1 - age_days / stale_after_days)
```

Evidence at the cutoff has recency one; evidence exactly at or older than the
stale threshold has recency zero. Future evidence is invalid. Missing recency
or evidence confidence produces a zero subcomponent and an explicit warning,
not ineligibility. Timestamps must be aware and are normalized to UTC.

## Development v1 policy

The committed test fixture represents a development form of proposed
Inflection Model v1.0. It includes the documented component weights and
explicit four-observation consistency/persistence windows. Its provider UUIDs,
liquidity floor, coverage/history requirements, confidence weights, and 10%
persistence threshold are synthetic development values, not universal or
finalized production policy.

The separate Phase 4B development fixture adds scoring mechanics without
rewriting this historical fixture. Its 30/25/20/15/5/5 subfactor split and
piecewise breakpoints are placeholder policy for deterministic validation, not
empirically calibrated or production-ready parameters.

The separate Phase 4D-A fixture retains all prior sections and adds development
Business Quality weights of 0.50/0.30/0.20 plus placeholder ROCE, ROE, and
margin-level curves. These values validate mechanics and are not production
calibration or empirical claims.

The Phase 4D-B fixture retains all earlier sections and adds 0.40/0.30/0.15/0.15
Cash-Flow Quality weights plus four development normalization curves. These are
also uncalibrated mechanics fixtures, not production policy.

Phase 4C accepts no arbitrary policy object. Its orchestrator resolves the
persisted active configuration at the knowledge cutoff and preserves model ID,
configuration ID, checksum, and semantic identity in the snapshot fingerprint.
Half-open effective intervals and overlap ambiguity therefore remain part of
every persisted audit. See [`score-snapshots.md`](score-snapshots.md).
