# NSE production ingestion

Production Data Activation A adds database-free adapters for three publicly
reachable official NSE archive artifacts:

- listed-equity master: `content/equities/EQUITY_L.csv`;
- CM UDiFF Common Bhavcopy Final:
  `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip`;
- daily index snapshot: `content/indices/ind_close_all_DDMMYYYY.csv`.

The adapters use only `https://www.nseindia.com` and
`https://nsearchives.nseindia.com`. They do not scrape HTML, bypass access
controls, use proxies, or depend on an unofficial market-data package. The
bounded standard-library client retains normal cookies, warms the NSE homepage
only for `www.nseindia.com` resources, retries a small number of transient
429/5xx/network failures, rejects redirects outside the allowlist, and caps
response bytes. A 404 is `source_not_available`, never a successful empty
trading day. Exact requested dates are mandatory; there is no latest-session,
holiday, or previous-day inference.

## Production separation and source terms

Every command requires `--database-url`, `--raw-root`, and `--license-class`.
There are no synthetic-development defaults. The preflight refuses known
fictional seed identities, the `synthetic_csv` provider, and the
`synthetic-development-only` licence marker. No mixed-database override exists.
Use a separately migrated production database and a separate raw archive root.

`--license-class` is an operator-supplied classification stored on the
production provider dataset. Inflector does not make a legal determination and
sets redistribution false. Use remains subject to NSE source terms; the system
does not redistribute provider data.

## Parsing and raw provenance

`EQUITY_L.csv` is header-checked and restricted to source `SERIES == EQ`.
Symbol, company name, listing date, and ISIN map directly to the canonical
company/security/listing contract. Sector and industry remain empty because
the source does not supply them. The active statuses describe the current
listed-equity master and do not create historical eligibility.

The market adapter archives the original ZIP bytes, validates safe members and
decompressed size, and requires exactly one date-specific UDiFF Final CSV.
Only `CM` / `STK` / `EQ` rows with native ISINs become daily OHLCV envelopes.
All numeric values use `Decimal` or integer parsing. `market_cap`,
`delivery_quantity`, and `delivery_percentage` remain null; traded value and
volume are not substitutes. Unsupported instruments are counted as skipped,
not rewritten as equities.

The index adapter retains every structurally valid row with the exact source
index name as provider-local identity, INR daily OHLC, and no default benchmark
selection. Downstream policies must name their benchmark explicitly.

For every successful live response, `retrieved_at` is the actual aware UTC
completion time. Market and benchmark `available_at` equal that same observed
time. Trading date, market close, midnight, and guessed publication time never
move availability backward. Fetching an old artifact today therefore creates
knowledge available today and is not claimed as historical PIT backfill.
`revision_at` remains null because these artifacts expose no defensible source
revision field; changed content can still append under existing strictly later
availability ordering.

Exact response bytes reach `LocalRawObjectStore` before accepted normalized
records. Universe and index CSV bytes remain unchanged; market raw payloads are
the original ZIP, not an extracted or transformed CSV. Source references retain
the archive member and deterministic row number where applicable.

## Commands

```powershell
python -m inflector_data.nse_cli ingest-universe `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS

python -m inflector_data.nse_cli ingest-market --date 2026-09-30 `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS

python -m inflector_data.nse_cli ingest-benchmarks --date 2026-09-30 `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS

python -m inflector_data.nse_cli ingest-daily --date 2026-09-30 `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS
```

`ingest-daily` runs universe, market, then benchmarks. Its JSON output reports
source URI, observed retrieval, run ID, durable counters, and provider-level
skips per stage. It stops with a non-zero exit code on a failed stage; earlier
committed stages remain auditable.

When live acquisition is blocked, pass an explicitly downloaded official file
and its original official URI. Individual commands use `--file` and
`--source-uri`; daily uses `--universe-file/--universe-source-uri`,
`--market-file/--market-source-uri`, and
`--benchmark-file/--benchmark-source-uri`. Local ingestion still receives a
new observed retrieval time and is not labelled synthetic.

## PowerShell runner and Task Scheduler

The runner requires an explicit date and three environment variables:

```powershell
.\scripts\ingest_nse_daily.ps1 -Date 2026-09-30
```

After acceptance, an operator may install a one-date task manually, changing
both date and time deliberately for each requested session:

```powershell
schtasks.exe /Create /TN "Inflector NSE 2026-09-30" /SC ONCE /ST 20:00 /SD 30/09/2026 /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Inflector\scripts\ingest_nse_daily.ps1 -Date 2026-09-30"
```

No task is installed automatically. This slice adds no range backfill, BSE,
financial statements, market capitalization, delivery enrichment, corporate
actions, announcements, attention, scoring, backtesting, or frontend changes.
