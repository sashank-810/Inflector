# Production O notification delivery

Production O delivers immutable Production N outbox events through Telegram
without changing their eligibility, factual payload, or K/L lineage. It adds no
research, score, rank, severity, recommendation, watchlist, or portfolio logic.

## Boundary and identity

`production_notification_delivery_v1` binds the accepted N policy checksum,
`telegram_bot_api_v1`, logical target `primary_telegram`, renderer
`research_notification_telegram_v1`, claim semantics, retry schedule, and
per-cycle capacity. A delivery key is canonical JSON SHA-256 over the N outbox
row/key, delivery-policy checksum, transport, logical target, and renderer.
Preparing the same event twice therefore reuses one delivery row; a conflicting
row with the same key fails closed.

The logical target is not a secret. The real bot token and chat ID are read
only at runtime from `INFLECTOR_TELEGRAM_BOT_TOKEN` and
`INFLECTOR_TELEGRAM_CHAT_ID`. They are never stored, rendered, or logged.

## Durable lifecycle

`ResearchNotificationDelivery` has the explicit states `pending`, `claimed`,
`retry_wait`, `delivered`, and `dead_letter`. A transactional claim records its
owner, acquisition time, expiry, increments `attempt_count`, and creates one
append-only `ResearchNotificationDeliveryAttempt`. An active lease excludes
another worker; an expired lease may be recovered. Delivered and dead-letter
rows are terminal.

The network request is outside the database transaction:

1. claim and persist the started attempt, then commit;
2. perform the finite-timeout Telegram request;
3. persist the attempt result and state transition, then commit.

This avoids holding database locks during network I/O. It also creates an
unavoidable crash window: Telegram may accept a message before the local
success transaction commits. Telegram Bot API has no application-supplied
idempotency key, so external delivery is **AT-LEAST-ONCE**, not exactly once.
The short deterministic event reference in each message makes such a duplicate
auditable. Internal delivery identity, claim ownership, and attempt history are
deterministic and retry-safe.

## Telegram rendering and response handling

The plain-text renderer consumes only the immutable N payload. It displays the
current symbol, then baseline symbol, then a deterministic identity label; maps
matched trigger codes to factual phrases; includes score/rank only when present;
and includes a short event reference. Null never becomes zero. The concise
format stays below Telegram's message bound and contains no investment thesis
or recommendation language.

The adapter posts only `chat_id` and `text` to the official Bot API
`sendMessage` endpoint with a 10-second timeout. HTTP 200 is insufficient:
success requires JSON `ok=true` and a `result.message_id`, which is stored only
as provider audit metadata.

## Retry and dead letter

V1 permits five attempts. Retry delays after attempts 1–4 are respectively
60, 300, 1,800, and 7,200 seconds, with no jitter. Both HTTP statuses and
Telegram response `error_code` values are policy-bound: 400, 401, and 403 are
permanent; 429 and the 5xx class are retryable. Connection failures, timeouts,
and malformed responses without a permanent status/code are retryable. For a
429 response a valid Telegram `retry_after` can only extend the scheduled
delay. Other unclassified provider failures fail closed as permanent.

Classification precedence is deterministic: (1) any explicit permanent HTTP
status or Telegram code; (2) any explicit retryable HTTP status or Telegram
code; (3) network failure; (4) malformed-response fallback; and (5) permanent
fail-closed. Thus malformed payload structure never overrides HTTP 400/401/403.
A permanent failure or exhaustion of attempt five moves the delivery to
`dead_letter`; no evidence or attempt is deleted.

Due `retry_wait` backlog is eligible in later cycles and is ordered neutrally by
due/creation time and delivery key. The per-cycle bound of 100 is operational
capacity control, never a research-merit filter.

## Operations V6

`nse_daily_operations_v6` preserves V5 and appends `notification_delivery`
after `notification_projection`. Provider failures are represented in the
delivery lifecycle and do not invalidate completed research, K, L, or N facts.
Integrity/program failures fail the delivery stage; normal operations resume
only that stage because all predecessors are already complete.

Missing Telegram credentials produces a successful `transport_unavailable`
stage result with pending backlog counts and no network attempt. Doctor reports
only whether each credential is available, never its value. The existing single
Asia/Kolkata 20:00 scheduler remains authoritative.

A real smoke send additionally requires
`INFLECTOR_ALLOW_LIVE_TELEGRAM_SMOKE=1`; credentials alone never authorize a
smoke message. Production O adds no other transport and no public API or UI.
