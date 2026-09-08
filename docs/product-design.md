# Product design and frontend decisions

## Product character

Inflector is a personal financial research workstation: a quiet place to find,
interrogate, and track evidence. It should feel closer to a simplified
institutional research terminal than a consumer brokerage or generic SaaS app.
The design rewards comparison, provenance, chronological context, and careful
judgement. It never turns a score into a trade instruction.

The visual tone is dark by default, neutral, compact, and deliberate: charcoal
surfaces, restrained borders, a high-contrast reading layer, and a limited
semantic palette. Green/red communicate signed change only; they are never the
sole carrier of meaning. Avoid large hero areas, rounded-card galleries,
celebratory motion, gradients, confetti, and brokerage-style buy/sell language.

## Information hierarchy

The application consistently answers these questions in order:

1. **What deserves attention now?** Ranked opportunities, score movers, fresh
   catalysts/risks, and freshness.
2. **Why?** Component contribution, evidence, confidence, missing inputs, and
   score change.
3. **Is the business improving?** Financial trends, cash conversion, leverage,
   capital efficiency, and event sequence.
4. **What could invalidate it?** Risk flags, quality concerns, weak evidence,
   valuation, attention, and market-structure caveats.
5. **What do I think?** Personal watchlist state, thesis, questions, and
   invalidation conditions.

Company identity, current score/confidence, effective as-of date, and source
freshness remain visible in page context. Raw values are never visually
subordinate to a decorative aggregate score.

## Primary workflows

| Workflow | User path | Completion criterion |
|---|---|---|
| Triage the queue | Dashboard → score movers/new events → Explorer | A company is opened, saved, or deliberately ignored |
| Screen a thesis | Explorer → filters/sort → compare rows → company page | Candidate is placed in a watchlist state with a reason |
| Investigate an inflection | Company overview → timeline → financial/catalyst/risk evidence | User can state what changed, when it was public, and what could fail |
| Challenge the score | Score explanation → component → feature → filing/event evidence | Every contribution is traceable or the score is marked incomplete |
| Maintain research | Watchlist → notes → alerts → revisit timeline | Thesis and invalidation conditions are current |
| Test a hypothesis | Backtests → parameters → run → audit assumptions/results | Result can be reproduced from its data/model manifest |

Keyboard-first navigation is encouraged but never required. A universal company
search is available from the application chrome once real company data exists.

## Dashboard layout philosophy

The dashboard is a decision surface, not a KPI collage. A fixed left rail holds
primary destinations, a compact top bar provides search and global as-of/data
status, and a scrollable main column starts with the research opportunity queue.
The default desktop composition is:

1. **Queue table:** highest-information module; rank, company, score and delta,
   confidence, key driver, risk, freshness, watchlist state.
2. **Change stream:** score movers and new catalysts/risks, ordered by their
   `available_at`, not marketing-style notifications.
3. **Research context:** compact market/universe health and personal watchlist
   activity, shown below rather than competing with the queue.

Cards group a single question; they do not reproduce the same score in several
shapes. Dense table rows expand into an evidence preview or open the company
page. Persistent filters and a visible "as of" control make the research
context explicit.

## Opportunity Explorer workflow

The Explorer is a filterable, URL-addressable research table. It begins with a
saved base universe and displays the active as-of date, universe eligibility,
and row count. A filter bar exposes score, sector, market cap, revenue/profit
growth, ROCE, debt, valuation, attention, risks, and catalysts. Advanced
filters live in a structured popover/drawer; applied filters become editable
chips. URL search parameters store filter, sort, and page/selection state so a
view is shareable and revisitable locally.

The table is the primary work area: sortable columns, compact sparklines only
where they add directional context, pinned company identity, column chooser,
and a detail preview. Missing values use an em dash plus an accessible reason,
not zero. A row click opens the company page; a dedicated control modifies the
watchlist to avoid accidental workflow changes. Bulk selection is deferred
until a real batch workflow exists.

## Company Research workflow

The company page opens with an evidence-oriented header: legal/display name,
listing identity, sector, score, confidence, score change, as-of timestamp,
source freshness, and watchlist state. It then follows an intentional reading
order:

1. **Research brief:** business description, current inflection statement,
   top positive and negative factors, active risks, and data coverage.
2. **Financial evidence:** comparable quarterly/TTM trends for revenue, profit,
   margin, ROCE, debt, and cash flow.
3. **Inflection Timeline:** dated financial, event, score, and personal-note
   markers in one chronological model.
4. **Catalysts, risks, valuation, attention, and market structure:** each with
   current state, change, confidence, and source drill-down.
5. **Personal workspace:** thesis, what must happen, invalidation conditions,
   and notes; private content is clearly distinct from sourced facts.

The initial layout is a conventional scrollable research dossier. Resizable
three-pane layout is a later enhancement for wide desktop screens, only after
the company page proves a sustained need for simultaneous table/chart/evidence
comparison.

## Score explanation workflow

Clicking a score opens a drill-down, not a modal dead end. The first view shows
the score, confidence, model/configuration version, data cutoff, eligibility
state, and a waterfall-like list of weighted contributions. Selecting a
component reveals raw feature values, formula/version, period comparison,
input freshness, and exceptions. Selecting an evidence reference opens the
source document at its relevant excerpt/page where available.

Score movement is framed as a comparison of two immutable snapshots: “what
changed since prior score”, model/configuration differences, new or resolved
facts, and missing/invalid data. Do not show a positive score without the
corresponding counter-evidence and risk state.

## Inflection Timeline

The timeline is the central explanatory artifact, not a decorative feed. Its
horizontal time axis aligns four lanes: reported financial periods, public
announcements/documents, score snapshots, and personal research activity.
Markers use `available_at` for the decision timeline and disclose reported and
published dates in the detail panel. The user can filter lanes and event types,
zoom from quarters to years, and select a marker to inspect evidence.

This makes the sequence testable: business change → disclosure → model reaction
→ market context. It also makes gaps and late ingestion visible. Dense periods
cluster visibly; the interface never silently drops events.

## Freshness and confidence

Freshness is shown close to each decision-bearing value, using plain language
and exact timestamps on demand: “Financials: Q1 FY26, available 14 Aug 2026”;
“Prices: 1 trading day behind”; “Holdings: 47 days old.” A compact status label
uses fresh, aging, stale, unknown, or failed—not a vague green dot. Thresholds
are provider/dataset-configurable.

Confidence is separate from freshness and score. It shows high/moderate/low
with a disclosed driver such as coverage, source quality, evidence quality, or
comparability. Tooltips and the score drill-down reveal the calculation. Color,
label, icon, and text all communicate status.

## Loading, error, and empty states

- **Loading:** preserve layout geometry with restrained skeletons for tables
  and chart frames; retain prior data with an explicit refreshing state where
  safe. Do not use indefinite spinners for a full page.
- **Errors:** show what failed, scope, last successful data, retry action, and
  a correlation/job ID where useful. Keep unrelated sections usable.
- **Empty:** distinguish no matching records, not-yet-ingested data, excluded
  universe, and unavailable provider data. Explain the next useful action.
- **Partial:** render available data, label absent/stale fields, and never
  calculate a polished aggregate from undisclosed missing components.

## Accessibility and responsive strategy

Meet WCAG 2.2 AA as the product baseline: semantic landmarks/headings,
keyboard-operable controls and tables, visible focus, logical focus management,
skip link, accessible names/tooltips, reduced-motion support, 4.5:1 text
contrast, 3:1 large/non-text contrast, and text alternatives/data tables for
charts. Use locale-aware number/date formatting, support browser zoom to 200%,
and expose sortable/filter state to assistive technology. Never encode movement
or risk only by hue, arrows, or position.

Desktop is the primary research canvas from 1280px upward. At tablet widths,
the left rail collapses, secondary table columns hide by priority, and filters
move to a sheet. On phones, support reading a company brief, alerts, watchlist,
and a reduced Explorer; do not force a multi-column workstation into a narrow
viewport. Company detail becomes a linear dossier; large tables provide a
selectable compact-card view plus a route to the full desktop table.

## Visual-density, chart, and table rules

**Density.** Prefer a 12–14px tabular data scale, 16px minimum body reading
scale, 28–36px row targets for dense tables, 8px spacing rhythm, subtle
one-pixel separators, and short labels. Use whitespace to separate analytical
groups, not to simulate luxury. Every screen must have one primary question.

**Charts.** A chart must answer a question faster than a table. Financial
charts favour comparable quarterly bars/lines, reference bands, explicit units,
zero baselines when meaningful, and direct annotations for events/restatements.
Price charts are time-series tools with corporate-action-aware data, benchmark
comparison, and event markers—not trading widgets. No 3D, pie charts for time
series, decorative gradients, dual axes without a clear reason, or colour-only
series identification. Provide a table/exportable values alternative.

**Tables.** Tables are first-class research instruments: stable column order,
right-aligned numerics with tabular figures, fixed precision/unit conventions,
sticky headers, controlled column widths, explicit sort direction, and an
accessible row focus state. Default sorting and filter criteria are visible.
Virtualization must preserve keyboard navigation, screen-reader semantics, and
row selection behavior.

## Dependency decisions

No dependency is installed by this architecture task. The decisions below set
the implementation default; actual adoption happens only in the named phase.

| Dependency | Purpose and placement | Phase | Decision | Reason / overlap |
|---|---|---:|---|---|
| shadcn/ui | Owned, accessible primitives in `apps/web/components/ui`; app shell, forms, menus, dialogs, tooltips | 1 | Adopt selectively | It supplies editable component source rather than a restrictive visual system. Add only primitives used by the shell. It can wrap cmdk and resizable panels, so do not independently add overlapping components. |
| TanStack Query | Client-side server-state cache, mutation/invalidation, background refresh for interactive API views | 5 | Defer | Phase 1 can use server-rendered/API fetches. Adopt when Explorer/watchlist need shared interactive cache; it overlaps neither React local state nor URL state, which remain for ephemeral/filter state. |
| TanStack Table | Headless sorting, filtering, column visibility/pinning, and accessible Explorer/worklist tables | 5 | Adopt | The Explorer needs table behavior beyond hand-built markup. It is headless and pairs with shadcn styling; it does not virtualize rows itself. |
| TanStack Virtual | Headless row/column virtualization for genuinely large tables/timelines | 5, conditional | Defer initially | Use only after measured Explorer/timeline volume or render cost requires it. It complements, rather than replaces, TanStack Table. |
| Apache ECharts | Financial, score, benchmark, and backtest analytical charts | 5 | Adopt selectively | Strong multi-series analytical/chart accessibility capability. Use a small local React wrapper and lazy-load charts. Do not use it for the primary interactive OHLC price chart. |
| TradingView Lightweight Charts | Interactive price/volume/OHLC chart on company market-structure research | 5 | Adopt selectively | Purpose-built financial time series with a smaller scope than ECharts. It overlaps on simple lines; ECharts owns non-price analytics and Lightweight Charts owns price action. Review licensing/attribution at install. |
| cmdk | Command palette and universal company search | 5 | Do not add directly | shadcn/ui’s Command component uses cmdk. Use that integration if/when command search ships; no second command-menu abstraction. |
| react-resizable-panels | User-resizable wide research panes | 5, conditional | Defer | Valuable only after the company dossier needs persistent simultaneous comparison. shadcn/ui exposes a resizable primitive, so adopt through that route if needed. |
| Lucide Icons | Consistent, accessible interface icons | 1 | Adopt | Lightweight tree-shakeable icon set aligned with shadcn/ui. Icons supplement text, never replace accessible labels. |

Official references: [shadcn/ui](https://ui.shadcn.com/docs), [TanStack Virtual](https://tanstack.com/virtual/latest/docs/introduction), [Apache ECharts](https://echarts.apache.org/en/index.html), [Lightweight Charts](https://tradingview.github.io/lightweight-charts/), and [Lucide React](https://lucide.dev/guide/react).

## Resolved and open questions

No architectural blocker remains for Phase 1. The only deferred product choices
are intentionally evidence-driven: whether the desktop dossier warrants
resizable panes, when measured table size needs virtualization, and whether the
phone Explorer receives a compact-card view or stays read-only. Resolve them
with real Phase 5 usage, not speculative dependencies.
