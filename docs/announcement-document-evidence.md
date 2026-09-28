# Announcement and document evidence

Phase 6A adds a provider-neutral, append-only evidence spine for public company
announcement metadata and related document metadata. It is source evidence
only. An announcement is not a catalyst, sentiment, recommendation,
materiality assessment, management-guidance claim, risk classification, or AI
interpretation.

## Boundary

`AnnouncementProvider` returns immutable `ProviderBatch[AnnouncementRecord]`
values through the established ingestion envelope. `AnnouncementRecord`
contains provider-neutral issuer/security hints, raw category and headline,
economic announcement date, exchange, and zero or more
`AnnouncementDocumentRecord` values. Adapters never receive a database
session.

Phase 6A supplies synthetic-only CSV and mock adapters. The flat CSV represents
multiple attachments as repeated rows with the same `external_id` and the same
announcement-level fields. Rows are grouped deterministically in input order;
they are never overwritten. No real NSE/BSE adapter is included.

The accepted document roles are `primary`, `attachment`, and `supporting`.
The initial structural document types are `announcement_attachment`,
`exchange_filing`, `press_release`, `investor_presentation`, and `other`.
Neither vocabulary is a catalyst taxonomy.

## Persistence and revision identity

Migration `20260928_0011` creates only:

- `announcements`, one immutable normalized observation per accepted source
  revision;
- `documents`, metadata and provenance only, with no large document bytes;
- `announcement_documents`, a many-to-many relation with a structural role and
  unique `(announcement_id, document_id)` membership.

Each accepted announcement and document references an immutable
`SourceRecord`. Raw provider batches are archived before normalization through
the existing content-addressed archive. Source idempotency remains
`provider_dataset_id + external_record_id + content_sha256`.

Within one provider dataset, revisions sharing an announcement
`external_record_id` are revisions of the same provider identity. Changed
content is accepted only when `(available_at, revision_at or available_at)` is
strictly later than the newest accepted revision. Otherwise it is quarantined
as `ambiguous_announcement_revision`. Distinct provider datasets remain
independent, and distinct external IDs are not deduplicated merely because
their company, date, or headline matches.

A corrected announcement owns a new immutable relation set. Attachments from
an older revision are not carried forward. A document URI is mutable metadata,
not announcement identity.

## Identity and validation

The ingestion service resolves a supplied legal name against canonical company
identity. If an ISIN is supplied, it resolves the canonical security and
requires that security's `company_id` to match the resolved announcement
company. A security is optional for legitimate issuer-level disclosures.
Mutable company, security, and listing status is not used to reject historical
evidence.

Normalized persistence requires the envelope's `available_at`; an
`announcement_date` is never substituted. Stable quarantine rules include:

- `missing_company_identity`, `unknown_company`, `unknown_security`, and
  `security_company_mismatch`;
- `missing_headline`, `missing_available_at`, and
  `invalid_announcement_date`;
- `invalid_document_role`, `invalid_document_sha256`, and
  `missing_document_uri`;
- `ambiguous_announcement_revision`.

Headlines and provider categories retain provider meaning and are not rewritten
or classified. A supplied `document_content_sha256` must be exactly a
hexadecimal SHA-256 digest. Null is valid, and no hash is computed from a URL.
Document availability and revision timestamps inherit the announcement
envelope in Phase 6A because the contract has no independent document
publication timestamp.

## Point-in-time reads

`PointInTimeAnnouncementReader` requires an explicit provider dataset and a
timezone-aware cutoff, normalized to UTC. Its APIs are:

- `announcement_as_of(provider_dataset_id, external_record_id, as_of)`;
- `company_announcements_as_of(provider_dataset_id, company_id, as_of,
  start_date=None, end_date=None)`;
- `security_announcements_as_of(provider_dataset_id, security_id, as_of,
  start_date=None, end_date=None)`.

Eligible rows have an accepted source and `announcement.available_at <= as_of`.
For each provider announcement identity the reader selects exactly one row by
`available_at DESC`, `coalesce(revision_at, available_at) DESC`,
`ingested_at DESC`, then immutable announcement ID descending. Ingestion time
is only a deterministic tie-breaker after knowledge-time eligibility; it does
not repair ambiguous revision chronology.

Economic date bounds apply to `announcement_date`, not knowledge time. When a
date bound is supplied, announcements without an announcement date are
excluded rather than guessed. The selected revision returns only documents
linked to that immutable row. Both announcement and document views retain
their `SourceRecordView`, including archive provenance and timestamps.

## Explicitly deferred

Phase 6A does not fetch document bytes, parse PDFs, extract text, run OCR,
create embeddings, call an LLM, produce summaries, or create document
interpretations. It creates no catalyst, event-evidence, risk, or management
commitment rows and performs no direction, sentiment, materiality, importance,
confidence, or recency-decay classification.

Scoring policies and `score_snapshot_v1`, `score_snapshot_v2`, and
`score_snapshot_v3` are unchanged. V3 maximum standard component coverage
remains `0.75`; Business Catalyst and Low Market Attention remain missing, and
`final_score` remains null.
