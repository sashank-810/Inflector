# NSE production corporate filings

Production Data Activation C consumes only the current official structured NSE
equity filing surfaces. It does not scrape rendered corporate-filings tables:

- corporate actions: `GET https://www.nseindia.com/api/corporates-corporateActions`
- announcements: `GET https://www.nseindia.com/api/corporate-announcements`

Both requests use `index=equities`, explicit `from_date` and `to_date` values in
`DD-MM-YYYY`, and an optional exact `symbol`. Responses are flat JSON arrays;
the public page paginates rows in the browser rather than exposing a server
cursor. Date ranges are limited to 366 days, and announcement/document
processing has explicit row bounds.

## Observed source shapes

The action response inspected on 2026-10-01 supplies `symbol`, `comp`, `series`,
`isin`, `subject`, `faceVal`, `exDate`, `recDate`, `caBroadcastDate`, and book/no-
delivery date fields. The adapter uses only current `EQ` identities in the
official NSE equity master. The official ISIN must agree with that master.

The announcement response supplies `seq_id`, `symbol`, `sm_isin`, `sm_name`,
`desc`, `attchmntText`, `an_dt`, `exchdisstime`, `attchmntFile`, size display
fields, and ancillary flags. `seq_id` is the source identity, `desc` the provider
category, `attchmntText` the exact descriptive headline when present, and
`an_dt` the event date. Attachments observed in this generation use
`https://nsearchives.nseindia.com/corporate/...`.

Event, broadcast, and economic dates are not knowledge clocks. For both
datasets, `retrieved_at` and `available_at` equal actual successful UTC
retrieval. An old filing downloaded today becomes known today. `revision_at`
remains null because this source generation did not supply a retained,
defensible revision timestamp. This is current-research bootstrap, not
historical PIT reconstruction.

## Corporate actions

`nse_corporate_action_purpose_rules_v1` is anchored and exact. It supports:

- one explicit `Rs`/`Re X Per Share` Dividend, Interim Dividend, Final Dividend,
  or Special Dividend, stored as exact `INR/share`;
- `Bonus A:B`, meaning A bonus shares for B existing shares;
- the observed face-value split phrase, reduced exactly from old/new face value
  (10 to 2 becomes 5:1);
- `Rights A:B @ Rs X/-` as an explicit total price; or
- `Rights A:B @ Premium Rs X/-` only when the same row independently supplies a
  positive face value, making total price exactly face value plus premium.

The source ex-date is stored as `ex_date`; for split, bonus, and rights it is
also `effective_date`, meaning the exchange economic adjustment date. Record
and explicit announcement dates remain independent.

Compound distributions, premium-only rights without face value, demergers,
mergers, meetings, interest/redemption, buybacks, and unmatched purposes are
unsupported rather than coerced. Ingestion does not adjust market prices.

## Announcements, documents, and catalyst evidence

Exact structured response bytes are archived first. Source locators identify
the JSON row and official sequence. Document metadata retains the observed URI,
but no content hash is fabricated. Exact attachment bytes are fetched only from
the explicit NSE HTTPS allowlist through `DocumentAcquisitionService`; redirect,
timeout, and byte bounds remain enforced.

Accepted PDFs use the existing `PyPdfTextExtractor`. There is no OCR, image
interpretation, arbitrary URL fetch, or execution of content. Image-only PDFs
may produce `no_extractable_text`.

The catalyst command reuses the accepted pipeline unchanged:

1. `PointInTimeAnnouncementReader` selects the accepted revision;
2. `DocumentAcquisitionService` archives exact attachment bytes;
3. `DocumentTextExtractionService` persists deterministic page text;
4. `BusinessEventDetectionService` runs `business_event_rules_v1`; and
5. `BusinessEventQuantitativeDerivationService` runs the existing quantitative
   ruleset.

The announcement's `available_at` remains event `source_available_at`; later
asset/extraction time never moves it. Headline detection may continue after a
document failure, but rejected assets never generate synthetic evidence. This
phase does not calculate Business Catalyst or create a score snapshot.

## Production commands

Commands require an explicit production database, separate raw root, and
caller-supplied source-terms classification. Preflight rejects known synthetic
development markers.

```powershell
python -m inflector_data.nse_cli ingest-corporate-actions `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS `
  --from-date 2026-09-01 --to-date 2026-10-01

python -m inflector_data.nse_cli ingest-announcements `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS `
  --from-date 2026-10-01 --to-date 2026-10-01 --max-announcements 100

.\scripts\ingest_nse_catalysts.ps1 `
  -FromDate 2026-10-01 -ToDate 2026-10-01 `
  -MaxAnnouncements 100 -MaxDocuments 100
```

`--symbol` narrows a command to one explicit current NSE symbol. The CLI emits
machine-readable stage counts and exits non-zero on failure. No unbounded crawl
or schedule is installed automatically.

Use remains subject to NSE source terms. Inflector retains official bytes for
private research provenance and does not redistribute provider data.
