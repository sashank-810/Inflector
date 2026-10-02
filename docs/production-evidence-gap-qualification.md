# Production evidence-gap qualification

Production Data Activation I closes the source-qualification review for the
remaining valuation and analyst-attention gaps. It does not make an unavailable
observation available merely to improve score coverage. Both tracks are
independent and both conclude `NOT_APPROVED` in this version.

## Market cap — NOT_APPROVED

The accepted valuation contract requires a full security-level market
capitalization in INR on the same atomic daily market observation selected by
the PIT market reader. It requires an exact economic date, observed knowledge
time, stable source identity, raw archival, and no stale forward-fill.

The bounded review considered these official NSE surfaces:

- The equity quote page and its backing `api/quote-equity` request display
  **Total Market Cap** separately from **Free Float Market Cap**, in INR crore,
  alongside a quote-page “as on” timestamp. A bounded request for TCS on
  2026-10-03 returned HTTP 403 through the accepted cookie-warmed NSE client.
  More importantly, the surface does not supply an approved downloadable daily
  artifact or a documented field definition/reference-time contract that can
  be joined atomically to the immutable UDiFF close bar. Stable bounded raw
  acquisition and exact `PriceBar.market_cap` compatibility therefore remain
  unproven.
- NSE's **Eligibility based on Market Capitalisation — All Companies** files
  are periodic regulatory ranking observations (for example December 31 or
  March 31), not a daily security-level market-bar series. They cannot be
  copied or forward-filled across daily bars.
- **Market Capitalisation, Weightage, Beta** and other index reports are
  constituent-specific and may use free-float/index methodology. They fail the
  broad ordinary-EQ and full-market-cap requirements.
- The accepted equity master, UDiFF bhavcopy, and delivery artifacts do not
  report market capitalization.

The gate fails on documented definition/reference-time semantics, reproducible
archiveable daily acquisition, broad daily coverage, and atomic compatibility
with the accepted valuation input. No provider, dataset, parser, or policy
asset is activated. `PriceBar.market_cap` stays null, production research keeps
`market_cap_missing`, and Valuation remains fail-closed.

No derived alternative is approved. In particular, Inflector does not combine
close price with issued, subscribed, paid-up, treasury-adjusted, or free-float
shares. No reviewed source establishes all required share-class, effective-date,
corporate-action, and PIT semantics.

## Analyst attention — NOT_APPROVED

The accepted Low Market Attention contract expects one complete, non-negative
integer `analyst_coverage_count` for a company-level dated snapshot, under an
explicit measurement definition. The observation must identify what counts as
an analyst, be PIT-visible, and be no more than the configured 180 days old.
The scorer interprets fewer covering analysts as lower attention. A meeting,
participant, brokerage, estimate, recommendation, or registered-intermediary
count is not automatically equivalent.

The bounded first-party review considered:

- NSE/BSE analyst and institutional-investor meeting announcements. These are
  event disclosures and may mix analysts, investors, institutions, organizers,
  transcripts, recordings, and repeated updates. They do not report a complete
  company coverage population.
- The SEBI Research Analyst registry. It identifies registered intermediaries
  and their registration validity, not which listed companies they actively
  cover at a dated observation.
- Issuer meeting schedules, participant lists, presentations, and conference
  call transcripts. Coverage is inconsistent, unstructured, non-comprehensive,
  and cannot distinguish ongoing coverage from attendance. Text extraction or
  participant-name counting is explicitly not an approved methodology.

No reviewed source proves a stable, broad, reproducible company-level active
coverage count. No analyst provider/dataset or acquisition stage is activated.
Research V4 retains `analyst_coverage_source = null`; missing analyst evidence
stays unavailable rather than zero, and Low Market Attention remains below its
unchanged full subfactor-coverage requirement.

## Production consequences

Because neither source passed qualification, Research V1–V4 and Operations
V1–V3 remain immutable. There is no Research V5, Operations V4, source-policy
asset, provider binding, new operational stage, or migration 0018. Existing
automatic financial endpoints, financial primitives, delivery, leases, resume,
PIT selection, and V5 orchestration remain unchanged.

Partial V5 snapshots remain valid. Missing Valuation or Low Market Attention
does not trigger weight renormalization, an older-source fallback, a fabricated
zero, or a final score. Historical PIT backtesting and opportunity discovery
remain outside this activation.
