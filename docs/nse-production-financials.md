# NSE Integrated Filing financial ingestion

Production Data Activation B adds a narrow archive-first adapter for the
current NSE **Integrated Filing - Financials** generation. It ends at accepted
`FinancialFact` rows. It does not run features, scoring, snapshots, or the
Opportunity Score.

## Official source and observed format

The official discovery surface is the NSE Integrated Filing page and its
structured response:

- page: `https://www.nseindia.com/companies-listing/corporate-integrated-filing`;
- endpoint: `https://www.nseindia.com/api/integrated-filing-results`;
- fixed request identity: `index=equities`, `period_ended=all`, and
  `type=Integrated Filing- Financials`;
- explicit selectors: `symbol`, bounded `page`/`size`, and optionally
  `from_date`/`to_date` in `DD-MM-YYYY` form.

Live inspection on 1 October 2026 observed discovery fields `seq_Id`, `symbol`,
`smName`, `qe_Date`, `consolidated`, `type_Sub`, `xbrl`, `xbrlFileSize`,
`ixbrl`, `broadcast_Date`, `creation_Date`, `revised_Date`, and
`revision_Remark`. The adapter uses the response only to locate and bind one
official XBRL. It does not scrape the rendered table. The exact XBRL bytes from
`https://nsearchives.nseindia.com/corporate/xbrl/INTEGRATED_FILING_..._WEB.xml`
are the financial fact source and are archived independently for every filing.

The controlled `INDAS` path supports the current SEBI capital-market namespace
generations observed live:

- `http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt` with
  `in-capmkt-ent-2025-01-31.xsd`;
- `http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt` with
  `in-capmkt-ent-2026-01-31.xsd`.

Ordinary non-financial ITC and TCS standalone and consolidated filings were
inspected. Their facts use context-local duration or instant periods, an
explicit standalone/consolidated fact, `iso4217:INR` units, and an INR-per-share
divide unit. `decimals` values include negative integers and `INF`; the numeric
fact text is already in absolute rupees. Human display rounding such as
"Crores" is not used as an XBRL multiplier. Banking paths were observed as a
materially separate taxonomy and are unsupported, not malformed.

This slice begins with quarter ends on or after 31 March 2025. Older NSE result
formats are not merged into this parser.

## Controlled mapping

`nse_integrated_financial_mapping_v1` uses exact namespace-qualified names.
There is no suffix, label, fuzzy, or presentation-tree matching.

| Exact NSE local concept in either supported namespace | Inflector metric |
|---|---|
| `RevenueFromOperations` | `operating_revenue` |
| `OtherIncome` | `other_income` |
| `FinanceCosts` | `finance_cost` |
| `ExceptionalItemsBeforeTax` | `exceptional_items` |
| `ProfitBeforeTax` | `profit_before_tax` |
| `TaxExpense` | `tax_expense` |
| `ProfitLossForPeriod` | `pat` |
| `BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations` | `eps_basic` |
| `DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations` | `eps_diluted` |
| `Assets` | `total_assets` |
| `Liabilities` | `total_liabilities` |
| `Equity` | `total_equity` |
| `CashAndCashEquivalents` | `cash_and_equivalents` |
| `Inventories` | `inventory` |
| `CashFlowsFromUsedInOperatingActivities` | `cash_flow_from_operations` |
| `CashFlowsFromUsedInInvestingActivities` | `cash_flow_from_investing` |
| `CashFlowsFromUsedInFinancingActivities` | `cash_flow_from_financing` |

The target metrics `revenue`, `operating_profit`, `ebitda_reported`, `ebit`,
`total_debt`, `trade_receivables`, `trade_payables`, and `capex_reported` are
intentionally unavailable in v1. In particular, `Income` is not silently
treated as revenue, borrowings/receivable/payable concepts are not summed,
cash-flow purchase lines are not called capex, and EBITDA is never derived.
Unrecognized facts are ignored without rejecting the filing.

Facts are `Decimal`; no float is involved. INR facts retain `INR`, `ones`, and
`INR`. EPS is accepted only from the explicit INR-per-share unit and remains
`INR/share`; it is not quarterized or summed by the provider.

## Context, period, scope, and duplicate rules

Every fact resolves its own `contextRef` and `unitRef`. External taxonomy
references are never fetched. DTD/entity input is rejected, response size is
bounded, and malformed XML fails closed. Dimensional contexts and contexts
outside the supported standard April-March shapes are ignored rather than
guessed.

Supported source periods are exact Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar quarters;
Apr-Sep half-year YTD; Apr-Dec nine-month YTD; and Apr-Mar annual. The provider
retains these reported windows. Existing period normalization, not the XBRL
adapter, derives individual quarters from cumulative facts. Standalone and
consolidated filings remain separate.

For one controlled metric/context, repeated facts with the same value and unit
select the first deterministic document locator while retaining all duplicate
locators in semantic hashing. Conflicting values quarantine that metric/context
as `conflicting_duplicate_financial_fact`; they are never averaged or selected
by minimum/maximum.

## Identity and immutable provenance

Discovery symbol maps exactly through the current official `EQUITY_L.csv` EQ
row to its ISIN. The financial envelope carries that optional provider-neutral
`security_isin`; ingestion resolves the canonical `Security` and `Company`.
Unknown securities quarantine, and a supplied company/security contradiction
quarantines as `financial_company_security_mismatch`. There is no company-name
fuzzy, substring, or approximate matching.

The official `seq_Id` is the provider-local filing identity. Each source fact
uses a deterministic identity derived from filing identity, qualified fact
name, context ID, and unit ID. Its raw reference is an exact
`xbrl:{QName}:context:{id}:unit:{id}:{locator}` locator. Exact XBRL response
bytes reach `LocalRawObjectStore` before accepted facts exist. No transformed
XML, generated CSV, or metadata ZIP is archived as provider truth.

## Point-in-time limitation

For every XBRL download:

- `retrieved_at` is the actual aware UTC successful retrieval time;
- `available_at` is exactly the same observed time;
- `published_at` and `revision_at` remain null because this activation does not
  archive and bind an independent official timestamp source defensibly.

**Historical bootstrap is not historical PIT reconstruction.** A quarter from
2025 downloaded on 1 October 2026 becomes known on 1 October 2026. Neither its
period end, exchange broadcast date, market close, nor midnight is substituted
for availability. Such data can support a current score after retrieval, but
cannot support an earlier backtest. Changed mapped content appends only under
the existing strictly later availability/revision rules; nothing is
overwritten.

## Production commands and bounds

All commands retain Production A's mandatory production database/raw-root/
licence arguments and synthetic-target preflight. Single-symbol bootstrap:

```powershell
python -m inflector_data.nse_cli ingest-financials --symbol ITC `
  --max-filings 20 `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS
```

Bounded multi-symbol bootstrap uses one symbol per line, deterministic
de-duplication, and explicit caps:

```powershell
python -m inflector_data.nse_cli ingest-financials `
  --symbols-file .\production-symbols.txt --max-symbols 25 --max-filings 100 `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS
```

The currently observed endpoint supports a bounded explicit filing window:

```powershell
python -m inflector_data.nse_cli ingest-financials --symbol TCS `
  --from-date 2026-04-01 --to-date 2026-09-30 --max-filings 20 `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS
```

The date span is capped at 366 days; symbols and filings are each capped at
100; the default inter-filing delay is one second. Machine-readable summaries
retain each issuer/filing failure, retrieval time, run ID, ingestion counters,
recognized source facts, mapped metric codes, and unsupported reason. The
separate PowerShell runner is:

```powershell
.\scripts\ingest_nse_financials.ps1 -Symbol ITC -MaxFilings 20
```

The runner reads only the three established `INFLECTOR_PRODUCTION_*`/NSE
environment variables. It does not install or combine scheduled jobs.

## Deferred boundaries and market capitalization reconnaissance

Banks, NBFCs, life/general insurers, REITs, InvITs, SMEs, legacy financial
results, and other materially different taxonomies are deferred. There is no
BSE, corporate-action, announcement, attention, document, delivery, frontend,
or scoring work in this activation.

Bounded reconnaissance found the official NSE regulatory "Market
Capitalisation All Companies" surface, but verified it only as periodic
tables, not an atomic daily per-security observation suitable for daily
`PriceBar.market_cap`. No daily atomic official source with coherent identity,
observation date, and defensible availability was established. Nothing is
derived from close × shares, turnover, traded value, or index weight, and no
periodic value is forward-filled. A later activation must handle this source
and provenance boundary explicitly.
