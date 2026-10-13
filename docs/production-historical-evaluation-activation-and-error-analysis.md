# Production S1 — Historical Evaluation Activation and Error Analysis

Production S1 activates the evidence path needed to move Production R beyond
fixture-only validation. It does not change the V5 model, any score, eligibility,
Q label definitions, or the R evaluation policy.

## Official historical membership evidence

Historical NSE membership is sourced from one explicit official CM MII daily
security-master artifact named `NSE_CM_security_DDMMYYYY.csv.gz`. The
`nse_cm_mii_security_daily` adapter accepts one requested date and one artifact,
archives the original compressed bytes before normalized acceptance, binds the
raw SHA-256 and source URI, and validates the required `Sgmt`, `FinInstrmTp`,
`ISIN`, `TckrSymb`, and `SctySrs` columns. Only source rows that are exactly
`CM`/`STK`/`EQ` enter Q's eligible projection; unsupported rows remain audited.

Retrieval time is the point-in-time availability time. A file describing an old
trading date is never backdated when fetched later. The adapter reconstructs Q
evidence from archived bytes and revalidates each canonical semantic row against
its persisted source-record hash. No current listings or survivor set is a
fallback.

S1 does not guess an NSE historical URL. Only bytes fetched by the reviewed
allowlisted HTTPS source are `verified_official_https` and may create Q source
records. Local operator bytes are archived as `operator_supplied_unverified`;
an NSE-looking URI cannot elevate them, and they cannot make readiness ready.
Every acquisition/revision has a separate append-only raw-SHA ledger entry,
while Q's protected semantic source identity remains
`nse-cm-mii-security:<date>:<isin>:EQ`.

## Independent session-calendar verification

The activation policy binds the official session evidence dataset
`nse_cm_official_session_calendar`, the accepted `nse_indices_daily` benchmark
dataset, and `NIFTY 50`. Expected dates are never accepted from a CLI/manifest or
bare `SourceRecord`. Production verification requires an archive-bound official
HTTPS acquisition whose raw bytes and SHA can be reconstructed by a reviewed,
versioned parser. The repository currently has no reviewed official NSE session
artifact format, so production verification fails closed and real readiness
remains `DATA-BLOCKED`. Fixture set-comparison tests cannot create production
verification. Weekdays and observed NIFTY rows never invent the calendar.

This layer does not rewrite Q labels. It determines whether the session basis
underlying real negative-label evaluation is independently supportable.

Any persisted calendar marked `verified` is revalidated before temporal-split
construction or readiness reporting. The calendar, acquisition, official NSE
provider/dataset, completed ingestion, accepted source record, archived-content
identity, benchmark dataset/series, persisted dates, counts, fingerprints,
verification checksum, and run key must agree exactly. A status or provenance
claim stored only on the calendar row is not sufficient.

## Temporal split and untouched holdout

The temporal split uses only completed R cohort cutoffs, one outcome-data cutoff,
independently verified session coverage, and date arithmetic for Q's longest
five-calendar-year horizon. Eligible cutoffs are sorted chronologically and
partitioned using integer floor semantics:

- development: earliest 60%;
- validation: next 20%;
- holdout: remaining latest 20%.

Validation and holdout must each contain at least 12 cutoffs. The exact ordered
cutoff lists and split checksum are append-only. Labels, returns, scores,
confusion classes, and randomization are not split inputs.

Readiness is explicitly bound to the accepted temporal-split policy checksum
and calls the same partition function used by split construction. It cannot be
`READY` unless one coherent R evaluation has a single outcome-data cutoff and
enough fully matured cutoffs to produce at least 12 validation and 12 holdout
cohorts.

S1 knows holdout identities but never reads their outcome or confusion facts.
Only development and validation cohort IDs are placed into the first
outcome-bearing SQL query. The schema additionally rejects holdout analysis rows
and requires every analysis run to record `holdout_outcomes_accessed = false`.
Production T owns holdout outcomes.

## Factual error analysis

The versioned error-analysis policy permits only development and validation FP/FN
facts. Results remain separate by temporal split and preserve outcome contract,
selection contract, evaluation view, year, J score bucket, frozen rankability,
snapshot status, component availability, unranked reason, boundary distance,
threshold timing, peak multiple, drawdown, and endpoint return.

Unrankable positive FNs are separated from rankable FNs below a fixed R boundary.
Rankable FNs retain display/dense rank, boundary score, positions below the
boundary, score gap, and frozen component state. FPs retain the same factual
score/rank/outcome diagnostics. Fixed signatures describe persisted states; they
do not claim causality or select a new threshold.

Component summaries are isolated by split, outcome contract, selection contract,
evaluation view, and component code. They use frozen persisted component facts and exact
Decimal medians. “Associated with” and “observed among” are the correct
interpretation. S1 performs no fitting, search, score change, model promotion, or
holdout report. Those boundaries remain for separately reviewed later phases.

## Operator commands

All commands emit deterministic JSON:

```text
python -m inflector_data.nse_cli ingest-historical-mii ...
python -m inflector_data.backtest_cli activate-historical-universe ...
python -m inflector_data.backtest_cli verify-historical-session-calendar ...
python -m inflector_data.backtest_cli historical-evaluation-readiness ...
python -m inflector_data.backtest_cli build-multibagger-temporal-split ...
python -m inflector_data.backtest_cli build-multibagger-error-analysis ...
python -m inflector_data.backtest_cli summarize-multibagger-error-analysis ...
python -m inflector_data.backtest_cli inspect-multibagger-error-cohort ...
```

Acquisition is explicit and bounded to one artifact/date per invocation. There
is no unbounded historical download loop. Readiness reports `READY`,
`DATA-BLOCKED`, or `ENVIRONMENT-BLOCKED` and never treats fixtures as real data.

Migration `20261006_0025` adds append-only MII/session acquisition evidence,
session verification, temporal split, and error-analysis tables. Operations V6
and all production research/delivery profiles remain unchanged.
