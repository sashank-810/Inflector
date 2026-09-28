# Document acquisition and deterministic text

Phase 6B extends the Phase 6A announcement/document source identity into an
immutable evidence chain:

```text
PIT-selected document metadata
  -> exact acquired bytes
  -> deterministic page text
  -> exact page/offset evidence slices
```

This phase represents source material. It does not interpret business meaning,
classify catalysts or risks, calculate materiality or sentiment, call AI, run
OCR, or change any score.

## Three distinct times

`PointInTimeDocument.available_at` remains the public source knowledge time.
`DocumentAsset.retrieved_at` records when bytes were operationally acquired,
and `DocumentTextExtraction.extracted_at` records when deterministic extraction
ran. Later acquisition or extraction never moves the document's PIT knowledge
time and never makes a corrected document visible at an earlier cutoff.

## Immutable byte acquisition

`DocumentFetcher` is database-free and returns the requested/resolved URI,
exact bytes, optional media type, and a timezone-aware retrieval timestamp.
Phase 6B implements only `MockDocumentFetcher` and a local-file fetcher
restricted to one configured root. The local fetcher rejects path traversal,
absolute paths outside that root, unexpected URI schemes, missing files, and
inputs exceeding the explicit byte limit. There is no HTTP client, redirect
policy, authentication, cookie handling, or live exchange adapter.

The development default limit is 25 MiB and is configurable per acquisition.
Partial or truncated content is never accepted. Exact fetched bytes are hashed
without normalization and stored under `sha256/<prefix>/<digest>` outside
PostgreSQL.

Asset status is deterministic:

- `verified`: a Phase 6A provider byte hash exists and exactly matches;
- `accepted_unverified`: the first non-empty content for a Document without a
  provider byte hash;
- `rejected_hash_mismatch`: actual bytes conflict with a provider hash;
- `rejected_content_conflict`: a no-hash Document's mutable URI later returns
  different content;
- `invalid_empty_content`: zero-byte content.

All fetched bytes, including rejected conflicts, are archived for audit. Only
`verified` and `accepted_unverified` assets are extractable. Same Document and
same actual hash reuses the immutable asset even when a later fetch timestamp
differs. Corrected disclosures must arrive as new Phase 6A Document rows rather
than replacing an accepted asset.

Declared media type and byte-detected media type remain separate. `%PDF-` byte
magic identifies PDF. Strictly decodable UTF-8 is identified as plain text only
when declared `text/plain`; everything else is
`application/octet-stream`. A `.pdf` URI is not media evidence, and a declared
PDF without PDF bytes receives `declared_pdf_content_mismatch`.

## Content-addressed object integrity

`RawObjectStore` now supports deterministic `get(object_key)`. The local store
accepts only the canonical SHA-256 key shape, rejects traversal and missing
objects, and verifies stored bytes against the hash encoded in the key.
Acquisition, extraction, and text reads additionally recompute SHA-256 against
their persisted asset/text metadata. Any divergence raises an explicit
integrity error; corrupted content is never processed or returned.

The Phase 6A `SourceRecord.raw_object_key` still identifies the archived
provider announcement metadata batch. A Phase 6B `DocumentAsset.object_key`
identifies the actual attachment bytes. They are separate evidence objects and
are never substituted for one another.

## Deterministic extraction

Supported extractors are:

- `plain_text` / `plain_text_v1`, with runtime identity
  `python-<major.minor.patch>`;
- `pypdf` / `pypdf_text_v1`, with the exact installed `pypdf` package version
  stored separately as the runtime identity.

The implementation uses permissively licensed `pypdf` 6.19.0. Extractor code,
semantic version, and runtime version together form an explicit immutable
selection key. APIs never choose a hidden latest or best parser.

Plain text is strict UTF-8. PDF extraction operates page by page and performs
no OCR, image-to-text conversion, password guessing, DOCX parsing, or HTML
semantic parsing. Encrypted/unreadable PDFs fail with
`encrypted_or_unreadable_pdf`. Unsupported media is recorded explicitly.

The only canonical text normalization is `CRLF -> LF` and `CR -> LF`.
Characters and whitespace are otherwise preserved. Multiple page strings are
joined with exactly `"\n\f\n"`.

Successful canonical full text is UTF-8 encoded and stored in the same
content-addressed object store. PostgreSQL retains only object identity, text
hash, character/page counts, extractor identity, status, warnings, and page
map. PDFs with no page text use `no_extractable_text`, store no fake text
object, and do not trigger OCR. A partially empty PDF may succeed with
`empty_pdf_page`; the empty page remains in the map.

## Page and citation contract

Each page-map entry contains:

- a one-based `page_number`;
- inclusive `start_offset` and exclusive `end_offset` in the canonical Unicode
  string;
- SHA-256 of exactly `page_text.encode("utf-8")`.

The `"\n\f\n"` separator is not attributed to either adjacent page.
`DocumentTextReader` consumes an already selected `PointInTimeDocument` and an
explicit extractor identity. It returns the selected document and SourceRecord
lineage, asset identity/hash/retrieval time, extraction identity/hash/time,
canonical text, and validated page map.

`page_evidence` returns an exact page slice. `excerpt_evidence` accepts only
`0 <= start < end <= character_count`, returns the exact canonical substring,
and identifies intersecting pages. These are in-memory citable primitives;
Phase 6B creates no `event_evidence` table.

## Deferred boundaries

Phase 6B has no arbitrary network fetching, OCR, embeddings, vector search,
LLM, summarizer, prompt, classifier, catalyst taxonomy, risk classification,
management commitment, sentiment/direction label, or materiality arithmetic.
It creates no scoring policy or snapshot version. V1/V2/V3 maximum persisted
coverage remains 0.25/0.60/0.75 and `final_score` remains null.
