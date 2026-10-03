# Production research notification outbox

Production N projects immutable Production L facts into durable,
transport-neutral notification candidates. It does not send messages, generate
recommendations, or reinterpret research evidence.

```text
completed monitored OpportunityChangeRun
-> versioned trigger-code intersection
-> one immutable pending outbox row per eligible OpportunityChangeItem
```

## Versioned trigger policy

`production_research_alert_policy_v1` binds the accepted Production L policy
`production_opportunity_change_v1` and checksum
`2189bdcf6243545524fa8cca8721cd4c5301b775a8c9f89f83c481c55bd646a9`.
The policy checksum is derived from canonical JSON and is bound into every
notification identity.

V1 triggers are factual high-level transitions: compared-universe entry/exit,
symbol change, rankability gain/loss, score increase/decrease, dense-rank
movement, coverage completion/regression, component gain/loss, eligibility
gain/loss, stale/recovered state, and unranked-reason change.

Snapshot changed/unchanged, remained states, unchanged score/rank/eligibility,
confidence-only changes, generic coverage changes, and internal component
score/contribution changes do not trigger an event by themselves. They remain
available in L and may be retained as context when another configured trigger
matches.

There are no score, rank, confidence, or materiality thresholds and no severity,
priority, importance, urgency, or alert score. Every eligible event is a peer.

## Eligibility and identity

For each immutable L item, matched triggers are the intersection of its
persisted change codes and the policy trigger list, in policy order. An empty
intersection creates no row. A non-empty intersection creates exactly one row,
even when several trigger codes match.

The deterministic notification key binds the N policy checksum, source L run
ID/key, source L item ID, ordered matched triggers, and payload schema version.
An identical rerun reuses the row; the same key with different immutable
content fails closed. A future policy checksum naturally produces a distinct
identity and never overwrites the old event.

## Payload and null semantics

The canonical structured payload contains source L/K lineage, company/security
identifiers where present, baseline/current symbols, all and matched change
codes, rankability and unranked reasons, exact score/rank/confidence facts,
component gains/losses and persisted L details, plus N/L policy identities.
Exact Decimal values are represented as decimal strings. Missing score, rank,
confidence, and delta values remain JSON null; no zero or synthetic transition
is introduced.

No LLM or investment-language generator is used. Universe entry remains a
universe-membership fact, symbol change remains one event for one L item, and
rank-only or score-only movements retain their exact factual distinction.

## Durable transport-neutral outbox

Migration `20261003_0021` adds only `research_notification_outbox`. Rows refer
to the exact L run/item, store policy and payload identities, and are created
with `delivery_status = pending`. Production N has no channel selection,
delivery attempts, claims, provider message IDs, sent timestamps, errors, or
transport credentials. Delivery bookkeeping belongs to a later phase.

Projection participates in the existing operational stage transaction. It
does not commit internally. A failure rolls back the incomplete stage work;
retry recreates or reuses deterministic rows without duplication. L, K,
ScoreSnapshot, evidence, and backtest records are never mutated.

## Operations V5

`nse_daily_operations_v5` preserves V4's Asia/Kolkata 20:00 cycle and appends
`notification_projection` after `opportunity_change`. The stage reads only the
exact L run ID persisted by the current operational stage; it never searches
for the newest or a manual L run.

When the first monitored cycle has no compatible baseline, the completed L
stage carries no change-run ID. N records `no_change_run`, creates zero rows,
and the operational cycle succeeds. A completed L run with no configured
triggers similarly succeeds with zero eligible notifications.

Doctor validates the actual N policy asset/checksum, its L binding, migration
head 0021, and projection enablement. No email, Telegram, Slack, Discord, SMS,
push, or other transport credentials are required. The existing single
scheduler, DB lease, retry, stale recovery, and completed-stage reuse remain
authoritative.
