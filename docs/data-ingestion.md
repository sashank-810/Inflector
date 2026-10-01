# Data ingestion spine

Every provider batch follows one path: provider adapter, typed envelope,
content-addressed raw archive, ingestion run, immutable source record,
deterministic validation, and canonical PostgreSQL records. Providers only
produce provider-neutral records; they never receive a SQLAlchemy session. Raw
bytes are durably archived before an ingestion run is created. After that run
is committed, a processing error rolls back its active normalization transaction
and commits a terminal `failed` run with its finish time, error, and known
counters before the original error is re-raised.
For failed runs, `records_accepted` and `records_quarantined` describe only
durable outcomes and are therefore zero after the rolled-back normalization
transaction; received and already-known duplicate counts remain auditable.

Raw bytes are stored outside PostgreSQL at `RAW_ARCHIVE_ROOT` under
`sha256/<prefix>/<digest>`. Repeating identical bytes reuses the same object.
`source_records` are unique by provider dataset, provider external ID, and
record-content SHA-256. An identical rerun creates an audit run but no new
source observation or economic price fact.

`raw_object_key` locates the immutable complete archived object.
`raw_payload_reference` independently locates the observation inside it (for
example `row-3`, an XBRL fact locator, or a document page/section). Legacy
records can retain `NULL` where this detail is genuinely unavailable.

Changed content for the same external ID creates a new immutable source record.
A corrected price bar is appended with its own availability and revision times;
the original is never overwritten. Point-in-time selection is deferred.

For one provider dataset, the economic identity of a market bar is
`security + trading_date + interval`. A different external ID with equivalent
OHLCV values is stored as a provenance-only `duplicate_economic` source, not a
second normalized bar. Changed values append only if their `(available_at,
revision_at)` ordering is strictly later than all prior revisions (a missing
revision time compares as that observation's available time). Changed values
without that defensible ordering are quarantined as `ambiguous_price_revision`.

Malformed bars and unknown securities are retained as source records marked
`quarantined`, with `data_quality_issues`, and never become `price_bars`.
Rules cover missing identity/date/values, negative values, impossible OHLC
bounds, unsupported intervals, unknown securities, malformed universe listing
dates, and listing end dates before their start dates.

## Development fixtures

Committed CSVs in `tests/fixtures` use `INF0...` synthetic ISINs and the
`synthetic-development-only` licence class. Ingest them only into a dedicated
development database and `var/raw/synthetic`. Before a real provider, create a
separate database/archive root and reset synthetic data rather than mixing it
with licensed data. No permanent `is_fake` fact field is needed.

## CLI

```powershell
python -m inflector_data.cli ingest-universe tests/fixtures/universe_synthetic.csv --database-url "postgresql+psycopg://inflector:inflector@localhost:5432/inflector_dev_synthetic" --raw-root var/raw/synthetic
python -m inflector_data.cli ingest-market tests/fixtures/market_valid_synthetic.csv --database-url "postgresql+psycopg://inflector:inflector@localhost:5432/inflector_dev_synthetic" --raw-root var/raw/synthetic
```

Each command reports its run UUID and received, accepted, quarantined, and
duplicate counts.

## Financial reporting (Phase 2B)

Financial CSV rows carry a filing identity and one reported fact. A single
immutable `financial_filing` header can contain any number of fiscal-period
facts; it does not have a one-period relationship. Periods support quarter,
half-year/YTD, nine-month/YTD, and annual reported windows. A metric's
controlled `semantic_type` distinguishes duration facts from balance-sheet
instant facts; TTM is deliberately absent.

Monetary INR values preserve their reported value, unit, scale, and currency.
Only explicit INR scales are normalized: ones ×1, thousand ×1,000, lakh
×100,000, million ×1,000,000, and crore ×10,000,000. `INR/share`, shares,
percentage, and ratio remain non-monetary/explicit values; foreign-currency
conversion is unsupported and quarantined.

The financial economic identity is provider dataset + company + fiscal period + filing scope +
metric. Equivalent values with another source ID retain provenance but do not
create another fact. Changed values append only under a strictly later
availability/revision ordering; otherwise they quarantine as
`ambiguous_financial_revision`. Different provider datasets are independent
evidence streams and are never auto-revised against one another. No PIT selector is implemented yet.

## Corporate actions and identity (Phase 2C)

Corporate actions are append-only, provider-dataset-scoped observations on the
actual security, never merely its company. Splits and bonuses use exact
`ratio_numerator / ratio_denominator` conventions: 2/1 means two resulting
shares for one existing share; a bonus 1/2 means one bonus share per two held.
Cash dividends retain Decimal amount, `INR`, and `INR/share`; rights retain the
same exact ratio plus an INR subscription price. No adjusted price, total-return
or rights factor is calculated in this phase.

Corporate-action revisions are scoped to one provider dataset and one economic
event. A cash dividend anchors on `ex_date`, falling back to `effective_date`
only when its ex-date is unknown; splits, bonuses, rights, symbol changes, and
replacements anchor on `effective_date`. The same shared rule matches incoming
and stored observations. Distinct anchors are independent events and may arrive
in any order. Same-external-ID content with a changed anchor is treated as a
correction only when its availability/revision ordering is strictly later;
otherwise it is quarantined. Equivalence compares each action's complete typed
terms, including exchange, old/new symbol, and successor ISIN. Provider datasets
remain independent evidence streams.

Listing validity uses half-open intervals `[valid_from, valid_to)`: an old
symbol ending on a date is inactive from that date and the successor begins on
it. Security replacement creates a new immutable ISIN/security and an explicit
predecessor-to-successor relationship; historical prices/facts remain on the
old security.
Dates, not `status`, determine point-in-time listing activity; `status` is
descriptive lifecycle metadata. Universe and symbol-change normalization both
reject an overlap for the same security and exchange, while adjacent intervals
are valid. Security succession rejects self edges and any edge that would close
an existing directed replacement cycle.

## Announcement and document evidence (Phase 6A)

Announcement ingestion reuses this same archive-first transaction boundary.
`AnnouncementProvider` returns provider-neutral announcement envelopes; the
synthetic CSV adapter represents multiple attachments as repeated rows under
one `external_id`, groups them deterministically, and never overwrites a row.
Mock and CSV are the only adapters in this phase; no live exchange scraper is
introduced.

The service resolves the canonical company and, when supplied, the ISIN. A
security/company contradiction quarantines the source, while a company-level
announcement may keep `security_id` null. Accepted rows append one immutable
announcement plus zero or more document metadata rows and revision-specific
relations. Each retains SourceRecord and raw-archive provenance. Documents
inherit the announcement envelope's availability/revision timestamps because
Phase 6A has no independent document availability field.

Identical provider-dataset/external-ID/content-hash reruns are source-level
duplicates. Changed content for the same provider announcement identity is a
new revision only when `(available_at, revision_at or available_at)` is
strictly later than the newest accepted revision; otherwise it is quarantined
as `ambiguous_announcement_revision`. Different provider datasets and distinct
external IDs remain independent even when headlines match.

Document bytes are not downloaded or stored in PostgreSQL. A supplied content
hash must be a byte-content SHA-256 digest; null is valid, and the URI is never
hashed as a substitute. Phase 6A performs no PDF extraction, OCR, text storage,
AI interpretation, or catalyst/risk classification. PIT behavior and the
complete validation contract are documented in
[`announcement-document-evidence.md`](announcement-document-evidence.md).

## Document acquisition and text (Phase 6B)

Phase 6B stores actual attachment bytes separately from the Phase 6A provider
metadata archive. Acquisition is bounded by an explicit maximum size and uses
only mock or configured-root local-file fetchers; arbitrary HTTP(S) fetching is
absent. Exact fetched bytes are hashed and stored content-addressably. A
provider hash match is `verified`; the first no-hash asset is
`accepted_unverified`; hash mismatches, changed content behind an immutable
Document URI, and empty content remain archived but cannot be extracted.

Deterministic extraction supports strict UTF-8 plain text and page-by-page PDF
text through `pypdf`. It records extractor code, semantic version, and runtime
package version. CRLF and CR normalize to LF; no other semantic rewriting is
allowed. Page strings join with `"\n\f\n"`, and page maps retain exact Unicode
offsets plus UTF-8 page hashes. Full text is another immutable object-store
object, never a PostgreSQL blob.

Source `available_at`, asset `retrieved_at`, and extraction `extracted_at` are
distinct. PIT eligibility continues to use only the Phase 6A source time. No
OCR, AI, catalyst inference, event-evidence persistence, or scoring occurs.
See [`document-acquisition-and-text.md`](document-acquisition-and-text.md).

## External attention evidence (Phase 6D-A)

Attention ingestion reuses the archive-first provider batch and SourceRecord
transaction. Its database-free record supports exactly news-mention counts
over explicit windows and analyst-coverage counts on explicit observation
dates. Mock and CSV adapters are available for fictional fixtures; no live HTTP
provider or web scraper exists.

Accepted records require a non-negative integer count in unit `count`, an
explicit provider scope, methodology, definition hash, coverage status, and
provider/source `available_at`. Retrieval, ingestion, window end, and analyst
observation dates never supply a missing knowledge time. Changed source content
is appended only at a strictly later semantic revision position, including
corrections delivered under a new external ID for the same economic identity.
Provider, methodology, scope, and definition-hash series remain isolated.
See [`attention-evidence.md`](attention-evidence.md).

## Official NSE production sources

Production Data Activation A adds separate adapters and CLI orchestration for
the current official NSE listed-equity master, CM UDiFF Final daily ZIP, and
daily index snapshot. Provider code `nse_official` and the three explicit
dataset codes are distinct from `synthetic_csv`. Production commands require a
caller-supplied database URL, raw root, and licence classification, and refuse
known synthetic database markers before mutation.

The same archive-first service persists exact CSV or ZIP response bytes before
accepted rows. Live retrieval completion is the observed UTC knowledge time;
market and benchmark availability is never inferred from trading date or close.
Historical downloads therefore do not claim historical PIT usability. UDiFF
supplies daily OHLCV only: market cap and delivery stay null. Index rows remain
separate provider-local identities, with no preferred benchmark. Local-file
recovery requires an explicit official source URI. Full usage and safety
details are in [`nse-production-ingestion.md`](nse-production-ingestion.md).

## Official NSE Integrated Filing financials

Production Data Activation B adds `nse_integrated_financials_xbrl` as a fourth
official NSE dataset without changing the archive-first transaction boundary.
The structured NSE Integrated Filing endpoint locates individual official XBRL
documents; each exact XBRL response is its own raw `ProviderBatch`. A controlled
QName mapping emits existing `FinancialRecord` envelopes, and optional
`security_isin` binds the fact to canonical company identity without fuzzy name
matching. Existing development CSV providers remain compatible.

Financial availability equals observed successful XBRL retrieval in UTC.
Discovery broadcast dates and source period ends do not backdate knowledge.
Historical bootstrap is therefore useful for current research after retrieval,
not for historical PIT reconstruction. Reported quarter/H1/9M/annual and
standalone/consolidated semantics pass unchanged into existing period/PIT
logic. Full mappings, commands, unsupported taxonomies, and safeguards are in
[`nse-production-financials.md`](nse-production-financials.md).

## Official NSE corporate filings

Production Data Activation C uses the current structured NSE equity corporate-
actions and announcements APIs. Exact JSON response bytes are archived first;
current EQ identity is reconciled through the official equity master without
fuzzy names. `nse_corporate_action_purpose_rules_v1` maps only explicit cash
dividend, bonus, split, and fully defensible rights terms. Unsupported or
compound purposes are counted, not coerced.

Announcement metadata retains the official sequence, native ISIN, event date,
category, headline text, and attachment URI. Event dates never replace source
availability: live `available_at` equals observed UTC retrieval. Official
attachment bytes then use the existing immutable document acquisition and
deterministic PDF extraction services. The accepted event and quantitative
rulesets run unchanged, preserving announcement source time. There is no OCR,
AI, scoring, or historical PIT reconstruction. Commands and exact source fields
are documented in
[`nse-production-corporate-filings.md`](nse-production-corporate-filings.md).

## Current research bootstrap and GDELT attention

Production Data Activation D adds a bounded `ingest-market-range` operation.
It requests both official daily artifacts for each explicit calendar date and
reports 404s separately from acquisition/format failures. It never substitutes
a nearby session. Every historical response keeps observed retrieval UTC as
availability, so the resulting history is current-research bootstrap data, not
historical PIT backtest data.

GDELT DOC 2.0 news attention is a separate `gdelt` provider dataset. The
database-free adapter performs one exact quoted legal-name query per company,
uses `TimelineVolRaw`, archives exact aggregate JSON, and emits only a complete
raw article count when the response shape is coherent. Failure and ambiguity
remain unavailable rather than zero. No article bodies or analyst counts are
acquired. The research profile and commands are documented in
[`production-current-research.md`](production-current-research.md).
