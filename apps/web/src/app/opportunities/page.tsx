import Link from "next/link";

import { OpportunityQueue } from "@/components/research/opportunity-queue";
import { ApiFailure, ModelFamilyForm } from "@/components/research/research-primitives";
import { ApiError } from "@/lib/api";
import { getOpportunityScores, opportunitySorts, opportunityStatuses } from "@/lib/opportunity-api";
import { hrefWithQuery, nonNegativeInteger, opportunitySort, opportunityStatus, positiveInteger, singleValue, type SearchParams } from "@/lib/research-query";

export const dynamic = "force-dynamic";

const sortLabels = {
  knowledge_cutoff_desc: "Latest knowledge cutoff",
  opportunity_score_desc: "Opportunity Score — descending",
  confidence_desc: "Confidence — descending",
  coverage_desc: "Coverage — descending",
  company_name_asc: "Company — A to Z"
} as const;

const statusLabels = {
  ineligible: "Ineligible",
  implemented_components_unavailable: "Components unavailable",
  partial_component_set: "Partial component set",
  final_score_available: "Final score available"
} as const;

export default async function OpportunitiesPage({ searchParams }: Readonly<{ searchParams: Promise<SearchParams> }>): Promise<React.ReactNode> {
  const query = await searchParams;
  const modelFamily = singleValue(query.model_family)?.trim() ?? "";
  if (modelFamily === "") {
    return <section><PageIntro /><div className="border border-line bg-panel p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">Explicit research context</p><h2 className="mt-2 text-xl font-semibold">Research model required</h2><p className="mt-2 max-w-2xl text-sm text-muted">Enter the exact model family to open its persisted V5 research queue. No active or latest model is inferred.</p><div className="mt-5"><ModelFamilyForm /></div></div></section>;
  }

  const status = opportunityStatus(query.status);
  const sort = opportunitySort(query.sort);
  const configurationChecksum = singleValue(query.configuration_checksum_sha256)?.trim() || undefined;
  const limit = positiveInteger(query.limit, 50, 100);
  const offset = nonNegativeInteger(query.offset, 0);
  let queue;
  try {
    queue = await getOpportunityScores({ modelFamily, configurationChecksum, status, sort, limit, offset });
  } catch (error) {
    const message = error instanceof ApiError ? error.message : "The persisted research queue is unavailable.";
    return <ApiFailure message={message} retryHref={hrefWithQuery("/opportunities", query, {})} />;
  }

  return (
    <section>
      <PageIntro />
      <QueueFilters configurationChecksum={configurationChecksum} modelFamily={modelFamily} sort={sort ?? "knowledge_cutoff_desc"} status={status} />
      {queue.total === 0 ? (
        <div className="mt-5 border border-line bg-panel p-6"><h2 className="font-medium">No persisted V5 research snapshots match this model and filter set.</h2><p className="mt-2 text-sm text-muted">This is an empty persisted research queue, not a conclusion about investment opportunities.</p><Link className="mt-4 inline-block text-sm text-accent hover:underline" href={`/opportunities?model_family=${encodeURIComponent(modelFamily)}`}>Clear filters</Link></div>
      ) : <div className="mt-5"><OpportunityQueue items={queue.items} limit={queue.limit} modelFamily={modelFamily} offset={queue.offset} searchParams={query} total={queue.total} /></div>}
    </section>
  );
}

function PageIntro(): React.ReactNode {
  return <div className="mb-6"><p className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-accent">Research queue</p><h1 className="text-2xl font-semibold tracking-tight">Persisted Opportunity Score contexts</h1><p className="mt-2 max-w-3xl text-sm text-muted">Inspect latest immutable V5 snapshots by explicit model, security, and scoring configuration. Direct sorts organize stored fields only.</p></div>;
}

function QueueFilters({ modelFamily, status, configurationChecksum, sort }: Readonly<{ modelFamily: string; status?: string; configurationChecksum?: string; sort: string }>): React.ReactNode {
  return (
    <form className="grid gap-3 border border-line bg-panel p-4 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_2fr_1fr_auto] xl:items-end" method="get">
      <FilterLabel label="Model family"><input className="h-9 w-full border border-line bg-canvas px-3 font-mono text-sm" defaultValue={modelFamily} name="model_family" required /></FilterLabel>
      <FilterLabel label="Status"><select className="h-9 w-full border border-line bg-canvas px-3 text-sm" defaultValue={status ?? ""} name="status"><option value="">All states</option>{opportunityStatuses.map((value) => <option key={value} value={value}>{statusLabels[value]}</option>)}</select></FilterLabel>
      <FilterLabel label="Configuration checksum"><input className="h-9 w-full border border-line bg-canvas px-3 font-mono text-sm" defaultValue={configurationChecksum} name="configuration_checksum_sha256" pattern="[0-9a-fA-F]{64}" placeholder="Optional exact SHA-256" /></FilterLabel>
      <FilterLabel label="Sort"><select className="h-9 w-full border border-line bg-canvas px-3 text-sm" defaultValue={sort} name="sort">{opportunitySorts.map((value) => <option key={value} value={value}>{sortLabels[value]}</option>)}</select></FilterLabel>
      <button className="h-9 border border-accent/70 bg-accent/10 px-4 text-sm hover:bg-accent/20" type="submit">Apply</button>
    </form>
  );
}

function FilterLabel({ label, children }: Readonly<{ label: string; children: React.ReactNode }>): React.ReactNode {
  return <label className="grid gap-1.5 text-xs text-muted"><span>{label}</span>{children}</label>;
}
