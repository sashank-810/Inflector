# Phase 2A data ingestion spine

Every provider batch follows one path: provider adapter, typed envelope,
content-addressed raw archive, ingestion run, immutable source record,
deterministic validation, and canonical PostgreSQL records. Providers only
produce provider-neutral records; they never receive a SQLAlchemy session. Raw
bytes are durably archived before an ingestion run is created. After that run
is committed, a processing error rolls back its active normalization transaction
and commits a terminal `failed` run with its finish time, error, and known
counters before the original error is re-raised.

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
