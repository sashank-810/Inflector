# Production U model release governance

Operational activation of the accepted champion follows the
[`controlled production activation runbook`](production-activation-runbook.md).
That deployment tooling does not alter this governance policy or admit a
challenger.

Production U is the release boundary between validated model evidence and live
scoring. It does not tune, train, calibrate, or promote a model automatically.
The accepted V5 champion remains operational even while real historical
Production T evaluation is data-blocked.

## Champion bootstrap and readiness separation

`bootstrap-active-champion` resolves the one accepted
`production_opportunity_v5` scoring configuration by checksum and creates an
immutable champion release candidate and deployment. The singleton
`active_model_release` pointer is then set atomically. Repeating the same
bootstrap reuses the exact deployment; it neither creates nor activates a
`ModelVersion` and it does not claim that Production T completed.

Release readiness reports two independent facts:

- live operational readiness, including schema, raw roots, runtime-presence
  checks, accepted assets, active-pointer/deployment integrity, rollback target,
  cycle binding, and release-scoped scoring-path integrity;
- historical scientific validation, which remains `data_blocked` until the
  official NSE session-calendar evidence required by S1 exists. A completed-T
  status row is never sufficient: U revalidates the complete immutable T
  terminal graph before reporting scientific completion.

## Challenger entry and immutable identity

A challenger release candidate can be resolved only from a completed,
`production_authoritative` T run whose fully revalidated terminal decision is
`CHALLENGER_PASSES_HOLDOUT`. U binds the exact T run and decision fingerprint,
S2 experiment, freeze, candidate definition/evidence checksums, ordered weights,
accepted V5 champion configuration, and U policy. Fixture evidence and the
other two T decisions cannot enter this path.

## Shadow and operational burn-in

The champion remains active throughout shadow. Each shadow event validates one
persisted V5 snapshot and recomposes the frozen challenger score from the same
eight component values using exact `Decimal` arithmetic. The event binds the
same security, evidence cutoff, source snapshot, and component-state checksum.
Candidate rankability and coverage must equal champion rankability and coverage.

Shadow records are isolated from opportunity ranking and notification delivery;
the database rejects notification contamination and non-zero rankability or
coverage deltas. Burn-in applies the policy's predeclared cycle count, wall-clock,
latency, integrity, drift, component, scheduler, and health gates. It reads no
future multibagger outcomes and performs no model tuning.

## Approval, activation, and rollback

Challenger activation consumes an immutable manual approval that binds the
candidate, passed burn-in, current champion deployment, policy, exact challenger
definition, operator provenance, and timestamp. Activation locks the singleton
pointer, revalidates authoritative T and every release binding, inserts an
immutable deployment transition, and changes the pointer in one transaction.
Failed transactions leave the champion active.

Rollback is another atomic pointer transition. It writes a new immutable
deployment plus a rollback record containing the failed deployment, restored
deployment, reason, factual health evidence, timestamp, and policy checksum.
Prior deployment history is never deleted.

## Runtime model resolution

When release governance is bootstrapped, the production cycle resolves the
active deployment once and persists a unique `model_release_cycle_bindings`
row. Retries load and validate that persisted binding before consulting the
global pointer, so later activation or rollback affects only new, unbound
cycles.

The normal V5 pipeline still produces the immutable point-in-time
`ScoreSnapshot` and component state. U then persists one
`model_release_production_scores` projection per cycle and source snapshot. For
the champion it mirrors the V5 final score. For a challenger it uses exact
Decimal recomposition of the same eight frozen V5 components and leaves the V5
snapshot untouched. Opportunity discovery ranks this bound production score;
change detection and notification provenance retain the same binding and
deployment identity. Shadow rows are structurally separate and cannot enter
this production chain.

## Commands

The `inflector_data.model_release_cli` module provides:

- `model-release-readiness`
- `bootstrap-active-champion`
- `create-release-candidate`
- `start-shadow-run`
- `record-shadow-cycle`
- `complete-burn-in`
- `create-activation-approval`
- `activate-release`
- `rollback-release`
- `show-active-release`
- `release-history`

There is no tuning, search, training, automatic-promotion, or holdout-reopening
command.

## Current blocker

On a configured runtime, the V5 champion may be live-operationally ready while
challenger activation remains data-blocked. No real challenger can reach U until
S1 obtains a reviewed authoritative NSE session-calendar source/parser and the
resulting S1/S2/T chain completes successfully.
