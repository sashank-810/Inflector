# Deterministic business-event primitives

Phase 6C-A derives neutral, source-linked `BusinessEvent` records from one
already point-in-time-selected announcement revision. A BusinessEvent records
that a narrow, versioned rule found an explicit factual event category. It is
not a Catalyst score, recommendation, sentiment label, materiality assessment,
or forecast.

```text
PIT announcement revision
  -> immutable headline and explicit Phase 6B document text
  -> page-local versioned rule match
  -> one BusinessEvent per announcement/type/ruleset
  -> one or more immutable BusinessEventEvidence spans
```

## Ruleset v1

The ruleset identity is `business_event_rules` /
`business_event_rules_v1`. Its exact event vocabulary is:

1. `order_award`
2. `capacity_expansion`
3. `commercial_commencement`
4. `capex_announcement`
5. `acquisition_agreement`
6. `regulatory_approval`

Every pattern family has a stable descriptive rule code and the immutable rule
semantic version `business_event_rule_v1`. Changing pattern meaning or the
controlled regulator vocabulary requires a new ruleset semantic version; a
rerun that changes a fingerprint under the same version fails closed.

Rules are case-insensitive regular expressions applied directly to original
headline or canonical Phase 6B page text. They do not stem, fuzzy-match,
rewrite, lowercase into a separate offset space, use provider category, or run
an NLP model. Document matching is page-local, so a phrase split across the
canonical `"\n\f\n"` separator cannot match. Regulatory approval requires an
approval relationship plus one v1 regulator token: USFDA, US FDA, FDA, CDSCO,
DCGI, RBI, SEBI, IRDAI, DGCA, CCI, or NCLT. Generic or board approval is not a
regulatory event.

The rules deliberately reject speculative and incidental wording such as
`may receive an order`, `order book`, `capacity utilisation`, `expects to
commence`, `may acquire`, `exploring acquisition`, `approval may be sought`,
and historical capital expenditure. False negatives are preferable to
silently broadening v1 semantics.

## Events and evidence

Repeated wording, headline matches, or matches across multiple attachments
produce one event for a given immutable announcement revision and event type,
with multiple evidence rows. Distinct announcement rows are never fuzzy-merged,
even when headline text is identical or separate providers reuse an external
ID. Corrected announcement revisions remain separate immutable sources.

Evidence kinds are exactly `announcement_headline` and `document_text`.
Headline evidence has no document/asset/extraction identity and uses the exact
headline string. Document evidence requires the exact Phase 6B Document,
DocumentAsset, and successful DocumentTextExtraction. It carries canonical
Unicode offsets, page number, page-text SHA-256, exact excerpt, excerpt
SHA-256, stable rule identity, and `source_available_at`.

Document context is the literal containing line. A headline uses the complete
headline. When either context exceeds 1000 characters, a deterministic
match-centered window of at most 1000 original characters is retained. Stored
start/end offsets select that context exactly; whitespace and characters are
not rewritten.

Evidence fingerprints canonically include announcement, evidence kind,
document/asset/extraction identities where applicable, rule identity, context
offsets and hash, and page numbers/hashes. Event fingerprints include company,
optional security, announcement, provider dataset, neutral event type, source
date/availability, ruleset identity, ordered rule codes, and ordered evidence
fingerprints. Operational timestamps and generated event/evidence IDs are
excluded.

## Point-in-time correction behavior

`source_available_at` is copied exactly from the selected announcement.
`derived_at` records when deterministic derivation ran; it is operational and
does not gate historical visibility. Document acquisition and extraction times
likewise never change source knowledge time.

Readers first apply Phase 6A revision selection. Events are then read only for
that exact immutable Announcement row. If an original revision says an order
was received and a correction removes that wording, the old stored event is
visible before the correction and absent afterward. If the correction instead
states capacity expansion, only the corrected revision's capacity event is
visible after its availability time.

## Deliberate boundaries

Phase 6C-A extracts no monetary values, order values, capex values, capacities,
stake percentages, target/effective dates, or management guidance. It computes
no materiality, event strength, direction, sentiment, or confidence. It uses no
LLM, embeddings, OCR, summarizer, or classifier and changes no scoring policy or
snapshot. Phase 6C-B will separately review quantitative event facts. Only
after both evidence phases are accepted may Phase 4D-H review Business Catalyst
scoring.

V1/V2/V3 maximum persisted coverage remains 0.25/0.60/0.75, Business Catalyst
and Low Market Attention remain missing, and `final_score` remains null.
