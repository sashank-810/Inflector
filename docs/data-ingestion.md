# Phase 2A data ingestion spine

Every provider batch follows one path: provider adapter, typed envelope,
content-addressed raw archive, ingestion run, immutable source record,
deterministic validation, and canonical PostgreSQL records. Providers only
produce provider-neutral records; they never receive a SQLAlchemy session.

Raw bytes are stored outside PostgreSQL at `RAW_ARCHIVE_ROOT` under
`sha256/<prefix>/<digest>`. Repeating identical bytes reuses the same object.
`source_records` are unique by provider dataset, provider external ID, and
record-content SHA-256. An identical rerun creates an audit run but no new
source observation or economic price fact.

Changed content for the same external ID creates a new immutable source record.
A corrected price bar is appended with its own availability and revision times;
the original is never overwritten. Point-in-time selection is deferred.

Malformed bars and unknown securities are retained as source records marked
`quarantined`, with `data_quality_issues`, and never become `price_bars`.
Rules cover missing identity/date/values, negative values, impossible OHLC
bounds, unsupported intervals, and unknown securities.

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
