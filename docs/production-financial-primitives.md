# Production financial primitives

Production Data Activation G expands the accepted official NSE Integrated
Filing financial path through an explicit semantic qualification policy. It
does not change a scorer, create source facts that NSE did not report, or seek
maximum component coverage.

## Source review

The source remains `nse_official / nse_integrated_financials_xbrl`, discovered
through `https://www.nseindia.com/api/integrated-filing-results` and acquired as
exact XBRL from `nsearchives.nseindia.com/corporate/xbrl/`. A bounded 2 October
2026 review covered TCS standalone/consolidated June 2026 filings and ITC
consolidated March 2026. The supported 2025 and 2026 `in-capmkt` namespaces
exposed exact `RevenueFromOperations`, `Income`, `OtherIncome`, `FinanceCosts`,
`DepreciationDepletionAndAmortisationExpense`, `BorrowingsCurrent`, and
`BorrowingsNoncurrent` facts. The reviewed instances did not expose a stable
structured reported-EBITDA concept.

The exact reviewed archive objects were TCS sequence 173420 consolidated
(`INTEGRATED_FILING_INDAS_1690004_09072026063620_WEB.xml`, 45,281 bytes), its
standalone companion sequence 173419 (18,685 bytes), and ITC consolidated
sequence 1670766
(`INTEGRATED_FILING_INDAS_1670766_21052026063229_WEB.xml`, 101,253 bytes).
These live-inspection bytes were not committed or persisted into a production
database.

`Income` equals revenue from operations plus other income in the representative
filings. It is therefore not accepted as the generic revenue denominator.
Textual EBITDA references are not numerical evidence.

## Versioned policy and qualification matrix

The authoritative asset is
`config/financial/production_financial_primitives_v1.json`:

- policy code: `nse_indas_financial_primitives_v1`;
- checksum: `d41513bf624f24f11c5a54a3979b4865a0f524514a77cf5849693f57ace95d92`;
- scopes: standalone and consolidated;
- namespaces: the already accepted 2025 and 2026 capital-market taxonomies.

| Normalized metric | Status | Exact source/decision |
|---|---|---|
| `revenue` | `APPROVED_DERIVED` | Identity normalization from persisted `operating_revenue` / `RevenueFromOperations`; `Income` rejected |
| `ebitda_reported` | `NOT_APPROVED` | No stable structured reported-EBITDA QName observed; no construction allowed |
| `borrowings_current` | `APPROVED_DIRECT` | `BorrowingsCurrent`, instant INR |
| `borrowings_non_current` | `APPROVED_DIRECT` | `BorrowingsNoncurrent`, instant INR |
| `total_debt` | `NOT_APPROVED` | Accepted debt boundary does not specify leases, maturities, debt securities and other interest-bearing items completely enough |
| `finance_cost` | `APPROVED_DIRECT` | `FinanceCosts`, duration INR |
| `depreciation_amortisation` | `APPROVED_DIRECT` | `DepreciationDepletionAndAmortisationExpense`, duration INR |

`APPROVED_DERIVED` for revenue is a non-arithmetic semantic identity, not a
second source fact. `FinancialFact` continues to store `operating_revenue`.
The policy-aware quarter value is labelled `revenue`, while its lineage retains
the source fact, filing, context, dataset, availability, policy code/checksum,
and the exact identity operation. Without the policy, `revenue` remains
unavailable.

## Missingness and debt boundary

Missing current or non-current borrowings never mean zero. Only an explicit
source zero is zero. The two borrowings facts are not summed. The structured
debt/equity ratio is not multiplied by equity. Lease liabilities, current
maturities, debt securities, preference shares, accrued interest, and other
interest-bearing liabilities are not silently included or excluded.

Finance cost and depreciation/amortisation remain useful direct primitives,
but they do not authorize EBITDA derivation. Consequently the accepted
`cfo_to_ebitda` and net-debt/EBITDA evidence remains unavailable without a true
`ebitda_reported` fact. `total_debt`-dependent balance-sheet evidence likewise
remains unavailable.

## Profile and operation

Research V1 and V2 are immutable. `nse_current_research_v3` preserves V2 and
adds only the policy asset/checksum binding. Its checksum is
`a1510f311babe3edcab242b228f79fc6353e34a399bc8145aec6764909eb4676`.
Operations V2 remains sufficient because the same bounded financial stage
loads the policy selected by the research profile; no Operations V3 exists.

For a direct financial bootstrap, make the policy explicit:

```powershell
python -m inflector_data.nse_cli ingest-financials `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --raw-root $env:INFLECTOR_PRODUCTION_RAW_ROOT `
  --license-class $env:INFLECTOR_NSE_LICENSE_CLASS `
  --symbol ITC --max-filings 20 `
  --financial-primitive-policy config/financial/production_financial_primitives_v1.json
```

Operational cycles using Research V3 pass that exact asset to the existing
financial ingestion stage. Raw XBRL archival, observed retrieval availability,
revision ordering, context priority, fiscal period normalization and explicit
cutoffs are unchanged. Historical filings retrieved today do not become
historically PIT-visible.

There is no migration. The existing financial fact/metric/source lineage model
stores the direct primitives. There is also no market-cap, valuation, analyst,
delivery, latest-quarter, backtest, frontend, recommendation, or scorer change.
