# Production market evidence

Production Data Activation F adds one official delivery source without changing
Market Structure scoring and records the separate market-cap qualification
result. It is current-research evidence, not reconstructed historical PIT data.

## Official delivery source

The canonical source is NSE's **Full Bhavcopy and Security Deliverable data**:

`https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`

It is persisted as provider `nse_official`, dataset
`nse_cash_market_delivery_daily`, report family
`full_bhavcopy_security_deliverable_v1`. The verified CSV fields are `SYMBOL`,
`SERIES`, `DATE1`, `TTL_TRD_QNTY`, `DELIV_QTY`, and `DELIV_PER`. Only exact
current canonical NSE `EQ` symbol identities are accepted. Other series and
unknown current identities are counted as unsupported, not coerced to equity.

Exact response bytes are archived before normalized acceptance. Quantities are
integers. `DELIV_PER` is retained exactly as percentage points and normalized
exactly with Decimal division by 100 into the ratio expected by the accepted
`average_delivery_percentage_20_v1` primitive. Blank, `-`, `NA`, `N.A.`, and
`N/A` remain null, never zero. Price bars are not rewritten: immutable delivery
observations are selected independently at the research cutoff and aligned to
the last 20 actual PIT-visible market-bar dates.

`available_at` is the observed UTC retrieval time. A report dated T downloaded
at T+N is invisible before T+N and becomes usable only for a later explicit
cutoff. Range ingestion attempts every requested date, preserves 404 as
`source_not_available`, never infers holidays, and is limited to 150 calendar
days:

```powershell
python -m inflector_data.nse_cli ingest-delivery-range `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS `
  --from-date 2026-06-01 --to-date 2026-09-30
```

## Versioned profiles

V1 assets are unchanged. `nse_current_research_v2` adds only the explicit
delivery dataset binding and a 20-observation delivery window. All financial,
market-price, benchmark, action, announcement, GDELT, analyst-null, eligibility,
confidence, and scoring policy choices remain V1-equivalent.

`nse_daily_operations_v2` adds only a 120-calendar-day delivery acquisition
lookback and the optional `delivery_history` stage after `market_history`.
Expected missing daily artifacts do not fail that stage. Parser, network, and
integrity failures remain distinct. Resume reuses a completed delivery stage;
the V2 profile checksum participates in the existing deterministic run key.

## Market-cap qualification

No market-cap source was approved and no acquisition code was added.

- NSE quote pages visually distinguish Total Market Cap and Free Float Market
  Cap, but the structured per-security quote request returned HTTP 403 through
  the bounded accepted client. Exact definition, effective timestamp, stable
  bounded acquisition, and archiveable source semantics therefore remain
  unproven for the existing valuation input.
- NSE index capitalization/weight reports are constituent-only and fail the
  broad ordinary-EQ coverage gate.
- NSE's periodic all-company regulatory capitalization tables are not a daily
  security observation and are not suitable for the existing exact-date
  `PriceBar.market_cap` valuation input without a separately reviewed temporal
  methodology.

Consequently the qualification gate fails on source definition/as-of proof,
bounded deterministic acquisition, and exact compatibility with the accepted
daily valuation input. Inflector does not derive market cap from shares,
price, turnover, free-float values, or another valuation measure. Production
valuation continues to report `market_cap_missing`. Analyst coverage also
remains unconfigured.
