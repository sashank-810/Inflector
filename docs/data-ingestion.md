# Phase 2A data ingestion spine

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

Listing validity uses half-open intervals `[valid_from, valid_to)`: an old
symbol ending on a date is inactive from that date and the successor begins on
it. Security replacement creates a new immutable ISIN/security and an explicit
predecessor-to-successor relationship; historical prices/facts remain on the
old security.
