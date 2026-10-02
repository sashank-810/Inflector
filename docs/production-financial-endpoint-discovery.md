# Production financial endpoint discovery

Production Data Activation H adds an explicit automatic current-research mode.
It continues to use the official NSE Integrated Filing discovery endpoint and
the accepted archive-first XBRL ingestion path. It introduces neither another
financial provider nor another scoring path.

## Policy

The immutable policy asset is
`config/financial/production_financial_endpoint_policy_v1.json`:

- code: `nse_latest_pit_financial_endpoint_v1`;
- checksum: `8d101bbec5c58c6939cb0650aa7ed428d9aa2b34ea01913aa48a2b5ce84661d5`;
- source: `nse_official / nse_integrated_financials_xbrl`;
- scope order: the bound research profile's consolidated-then-standalone order;
- endpoint order: stored period end, fiscal year, then fiscal quarter;
- completeness: ignored;
- revisions: selected later by the existing PIT financial reader;
- ambiguity: fail closed;
- future periods: excluded;
- no endpoint: issuer-level unavailable state.

Latest means the latest supported stored fiscal endpoint whose accepted source
fact has `available_at <= knowledge_cutoff`. It does not mean the most recently
retrieved older period. It never compares metric count, component coverage,
confidence, or score. A partial Q2 therefore remains Q2 even if Q1 contains
more metrics.

The selected scope is resolved before assembly. Financial facts are never
filled across standalone/consolidated boundaries. A later restatement changes
the PIT-visible facts for the same endpoint without changing endpoint
chronology.

## Profiles and commands

`nse_current_research_v4` preserves Research V3 and binds the endpoint policy.
`nse_daily_operations_v3` preserves Operations V2 and selects automatic mode.
The existing explicit command remains available:

```powershell
python -m inflector_data.research_cli run-current `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --model-family MODEL --research-profile config/research/production_research_v3.json `
  --symbol TCS --fiscal-year 2026 --fiscal-quarter 1 `
  --knowledge-cutoff 2026-10-03T00:00:00+05:30
```

Automatic research is explicit rather than inferred from missing arguments:

```powershell
python -m inflector_data.research_cli run-current-auto `
  --database-url $env:INFLECTOR_PRODUCTION_DATABASE_URL `
  --model-family MODEL --research-profile config/research/production_research_v4.json `
  --symbols-file .\symbols.txt --max-symbols 100 `
  --knowledge-cutoff 2026-10-03T00:00:00+05:30
```

Unattended operation uses `ops_cli run-cycle-auto` with Operations V3 and does
not accept a global fiscal endpoint. Its existing bounded financial stage
refreshes up to `maximum_financial_filings` per explicit symbol before each
symbol resolves its own endpoint. No date-to-quarter inference occurs.

The existing operational ledger is reused without a migration. Its legacy
non-null fiscal columns contain an internal zero sentinel for automatic runs;
machine-readable inputs and status expose fiscal year/quarter as null and
`financial_endpoint_mode` as `automatic`. The immutable run key additionally
binds endpoint/primitive policy checksums and the canonical scoring-policy
asset identity.

No accepted endpoint is not a cycle failure. Ambiguous endpoint metadata is an
issuer-scoped failure, later symbols continue, and no mutable latest-period
pointer is stored.
