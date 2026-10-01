import Link from "next/link";
import { notFound } from "next/navigation";

import { CompanyListings } from "@/components/company-listings";
import { AuditSummary } from "@/components/research/audit-summary";
import { ComponentDetailTable } from "@/components/research/component-detail";
import { ContextSelector } from "@/components/research/context-selector";
import { HistoryTable } from "@/components/research/history-table";
import { LatestChange } from "@/components/research/latest-change";
import { ApiFailure, ModelFamilyForm } from "@/components/research/research-primitives";
import { SnapshotHeader } from "@/components/research/snapshot-header";
import { ApiError, getCompany } from "@/lib/api";
import {
  getCompanyResearchContexts,
  getOpportunityScore,
  getOpportunityScoreAudit,
  getOpportunityScoreHistory
} from "@/lib/opportunity-api";
import { nonNegativeInteger, positiveInteger, singleValue, type SearchParams } from "@/lib/research-query";

export const dynamic = "force-dynamic";

export default async function CompanyPage({
  params,
  searchParams
}: Readonly<{ params: Promise<{ id: string }>; searchParams: Promise<SearchParams> }>): Promise<React.ReactNode> {
  const { id } = await params;
  const query = await searchParams;
  let company;
  try {
    company = await getCompany(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const modelFamily = singleValue(query.model_family)?.trim() ?? "";
  const securityId = singleValue(query.security_id)?.trim() ?? "";
  const configurationId = singleValue(query.scoring_configuration_id)?.trim() ?? "";
  const requestedSnapshotId = singleValue(query.snapshot_id)?.trim() || undefined;
  const companyPath = `/companies/${id}`;

  if (modelFamily === "") {
    return (
      <CompanyFrame company={company}>
        <section className="mt-6 border border-line bg-panel p-5">
          <h2 className="font-medium">Choose a research model</h2>
          <p className="mt-2 text-sm text-muted">Research is loaded only for an explicit model family; no current model is inferred.</p>
          <div className="mt-4"><ModelFamilyForm submitLabel="Discover research contexts" /></div>
        </section>
      </CompanyFrame>
    );
  }

  let contexts;
  try {
    contexts = await getCompanyResearchContexts(id, modelFamily);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    return <CompanyFrame company={company}><div className="mt-6"><ApiFailure message={apiMessage(error, "The company research contexts are unavailable.")} retryHref={companyPath} /></div></CompanyFrame>;
  }

  if (contexts.total === 0) {
    return (
      <CompanyFrame company={company}>
        <section className="mt-6 border border-line bg-panel p-5">
          <h2 className="font-medium">No V5 research contexts for this model</h2>
          <p className="mt-2 text-sm text-muted">The company exists, but no persisted V5 snapshots belong to <span className="font-mono">{modelFamily}</span>.</p>
          <div className="mt-4"><ModelFamilyForm submitLabel="Check another model" value={modelFamily} /></div>
        </section>
      </CompanyFrame>
    );
  }

  if ((securityId === "") !== (configurationId === "")) {
    return <CompanyFrame company={company}><ResearchState title="Incomplete research context" text="Both security_id and scoring_configuration_id are required. Neither value was inferred." href={`${companyPath}?model_family=${encodeURIComponent(modelFamily)}`} link="Choose a complete context" /></CompanyFrame>;
  }
  if (securityId === "") {
    return <CompanyFrame company={company}><ContextSelector companyId={id} contexts={contexts.items} modelFamily={modelFamily} /></CompanyFrame>;
  }

  const context = contexts.items.find((item) => item.security.security_id === securityId && item.scoring_configuration.id === configurationId);
  if (!context) {
    return <CompanyFrame company={company}><ResearchState title="Research context not found" text="This security and scoring configuration do not identify a persisted V5 context for the requested model family." href={`${companyPath}?model_family=${encodeURIComponent(modelFamily)}`} link="Return to context chooser" /></CompanyFrame>;
  }

  try {
    const history = await getOpportunityScoreHistory(id, {
      modelFamily,
      securityId,
      scoringConfigurationId: configurationId,
      limit: positiveInteger(query.history_limit, 20, 100),
      offset: nonNegativeInteger(query.history_offset, 0)
    });
    const latestSnapshotId = history.latest_change?.current_snapshot_id ?? context.latest_snapshot.snapshot_id;
    const selectedSnapshotId = requestedSnapshotId ?? latestSnapshotId;
    const [snapshot, audit] = await Promise.all([
      getOpportunityScore(selectedSnapshotId),
      getOpportunityScoreAudit(selectedSnapshotId)
    ]);
    if (snapshot.company.company_id !== id || snapshot.security.security_id !== securityId || snapshot.scoring_configuration_id !== configurationId) {
      return <CompanyFrame company={company}><ResearchState title="Snapshot does not belong to this research context" text="The requested historical snapshot was not rendered. Select a snapshot from this context's history." /></CompanyFrame>;
    }
    return (
      <CompanyFrame company={company}>
        <div className="mt-6 space-y-6">
          <SnapshotHeader companyPath={companyPath} context={context} isLatest={snapshot.snapshot_id === latestSnapshotId} searchParams={query} snapshot={snapshot} />
          <LatestChange change={history.latest_change} />
          <PersistedDecisionAudit snapshot={snapshot} />
          <ComponentDetailTable components={snapshot.components} />
          <HistoryTable history={history} pathname={companyPath} searchParams={query} />
          <AuditSummary audit={audit} />
        </div>
      </CompanyFrame>
    );
  } catch (error) {
    return <CompanyFrame company={company}><div className="mt-6"><ApiFailure message={apiMessage(error, "The selected research dossier is unavailable.")} retryHref={companyPath} /></div></CompanyFrame>;
  }
}

function CompanyFrame({ company, children }: Readonly<{ company: Awaited<ReturnType<typeof getCompany>>; children?: React.ReactNode }>): React.ReactNode {
  return (
    <section>
      <Link className="text-sm text-muted hover:text-accent hover:underline" href="/">← Overview</Link>
      <div className="mt-6 border-b border-line pb-6"><p className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-accent">Company identity</p><h1 className="text-3xl font-semibold tracking-tight">{company.display_name}</h1><p className="mt-2 text-sm text-muted">{company.legal_name}</p></div>
      <dl className="mt-6 grid gap-px border border-line bg-line sm:grid-cols-2"><div className="bg-panel p-4"><dt className="text-xs uppercase tracking-[0.12em] text-muted">Sector</dt><dd className="mt-2 text-sm">{company.sector}</dd></div><div className="bg-panel p-4"><dt className="text-xs uppercase tracking-[0.12em] text-muted">Industry</dt><dd className="mt-2 text-sm">{company.industry}</dd></div></dl>
      <section className="mt-6 border border-line bg-panel"><div className="border-b border-line px-4 py-3"><h2 className="font-medium">Securities and exchange listings</h2><p className="mt-1 text-sm text-muted">Canonical company identity remains separate from exchange symbols.</p></div><div className="divide-y divide-line">{company.securities.map((security) => <article className="p-4" key={security.id}><div className="grid gap-4 md:grid-cols-[1fr_2fr]"><div><p className="text-xs uppercase tracking-[0.12em] text-muted">ISIN</p><p className="mt-1 font-mono text-sm">{security.isin}</p><p className="mt-2 text-xs text-muted">{security.security_type} / {security.status}</p></div><div><p className="text-xs uppercase tracking-[0.12em] text-muted">Listings</p><div className="mt-2"><CompanyListings listings={security.listings} /></div></div></div></article>)}</div></section>
      {children}
    </section>
  );
}

function ResearchState({ title, text, href, link }: Readonly<{ title: string; text: string; href?: string; link?: string }>): React.ReactNode {
  return <section className="mt-6 border border-line bg-panel p-5" role="status"><h2 className="font-medium">{title}</h2><p className="mt-2 text-sm text-muted">{text}</p>{href && link && <Link className="mt-4 inline-block text-sm text-accent hover:underline" href={href}>{link}</Link>}</section>;
}

function PersistedDecisionAudit({ snapshot }: Readonly<{ snapshot: Awaited<ReturnType<typeof getOpportunityScore>> }>): React.ReactNode {
  return <section className="grid gap-3 lg:grid-cols-3" aria-label="Persisted decision audit"><AuditDisclosure label={`Eligibility (${snapshot.eligibility.eligible ? "eligible" : "ineligible"})`} value={snapshot.eligibility} /><AuditDisclosure label="Confidence details" value={snapshot.confidence_details} /><AuditDisclosure label="Context resolution" value={snapshot.context_resolution} /></section>;
}

function AuditDisclosure({ label, value }: Readonly<{ label: string; value: object }>): React.ReactNode {
  return <details className="border border-line bg-panel"><summary className="cursor-pointer px-4 py-3 text-sm hover:bg-raised">{label}</summary><pre className="max-h-80 overflow-auto border-t border-line p-3 text-[11px] leading-5 text-muted">{JSON.stringify(value, null, 2)}</pre></details>;
}

function apiMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}
