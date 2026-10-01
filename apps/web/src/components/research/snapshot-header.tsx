import Link from "next/link";

import { CompanyListings } from "@/components/company-listings";
import { CodeList, ExactValue, ResearchStatus } from "@/components/research/research-primitives";
import type { CompanyResearchContext, OpportunityScoreDetail } from "@/lib/opportunity-api";
import { hrefWithQuery, type SearchParams } from "@/lib/research-query";

export function SnapshotHeader({
  context,
  snapshot,
  isLatest,
  companyPath,
  searchParams
}: Readonly<{
  context: CompanyResearchContext;
  snapshot: OpportunityScoreDetail;
  isLatest: boolean;
  companyPath: string;
  searchParams: SearchParams;
}>): React.ReactNode {
  return (
    <section className="border border-line bg-panel" aria-labelledby="snapshot-heading">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line px-4 py-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
            {isLatest ? "Latest persisted snapshot" : "Historical persisted snapshot"}
          </p>
          <h2 className="mt-1 text-lg font-semibold" id="snapshot-heading">
            {snapshot.company.display_name}
          </h2>
          <p className="mt-1 text-sm text-muted">{snapshot.company.legal_name}</p>
        </div>
        {!isLatest && (
          <Link
            className="border border-line px-3 py-2 text-sm hover:bg-raised"
            href={hrefWithQuery(companyPath, searchParams, { snapshot_id: null })}
          >
            Return to latest
          </Link>
        )}
      </div>
      <div className="grid gap-px bg-line sm:grid-cols-2 xl:grid-cols-4">
        <Summary label="Opportunity Score">
          <span className="text-lg"><ExactValue value={snapshot.final_score} /></span>
        </Summary>
        <Summary label="Snapshot state"><ResearchStatus status={snapshot.snapshot_status} /></Summary>
        <Summary label="Confidence"><span className="font-mono">{snapshot.confidence}</span></Summary>
        <Summary label="Top-level coverage"><span className="font-mono">{snapshot.top_level_component_weight_coverage}</span></Summary>
      </div>
      <div className="grid gap-5 p-4 lg:grid-cols-3">
        <div>
          <p className="text-xs uppercase tracking-[0.1em] text-muted">Security identity</p>
          <p className="mt-2 font-mono text-sm">{snapshot.security.isin}</p>
          <p className="mt-1 text-xs text-muted">{snapshot.security.security_type} / {snapshot.security.security_status}</p>
          <div className="mt-2"><CompanyListings listings={snapshot.security.listings} /></div>
        </div>
        <div className="text-xs">
          <p className="uppercase tracking-[0.1em] text-muted">Immutable model context</p>
          <p className="mt-2">{context.model_version.model_family} / {context.model_version.semantic_version}</p>
          <p className="mt-1">{context.scoring_configuration.configuration_name} / {context.scoring_configuration.configuration_version}</p>
          <p className="mt-1 break-all font-mono text-[11px] text-muted">{context.scoring_configuration.checksum_sha256}</p>
        </div>
        <dl className="grid grid-cols-2 gap-3 text-xs">
          <div><dt className="text-muted">Fiscal endpoint</dt><dd className="mt-1 font-mono">FY{snapshot.ending_fiscal_year} Q{snapshot.ending_fiscal_quarter}</dd></div>
          <div><dt className="text-muted">As-of date</dt><dd className="mt-1 font-mono">{snapshot.as_of_date}</dd></div>
          <div className="col-span-2"><dt className="text-muted">Knowledge cutoff</dt><dd className="mt-1 font-mono">{snapshot.knowledge_cutoff}</dd></div>
        </dl>
      </div>
      <div className="grid gap-4 border-t border-line p-4 md:grid-cols-2">
        <div><p className="mb-2 text-xs text-muted">Available components</p><CodeList values={snapshot.available_component_codes} /></div>
        <div><p className="mb-2 text-xs text-muted">Missing components</p><CodeList empty="No missing components" values={snapshot.missing_component_codes} /></div>
      </div>
      <p className="border-t border-line px-4 py-3 text-xs text-muted">
        Identity metadata is current; snapshot eligibility and score remain historical to the persisted cutoff.
      </p>
    </section>
  );
}

function Summary({ label, children }: Readonly<{ label: string; children: React.ReactNode }>): React.ReactNode {
  return <div className="bg-panel p-4"><p className="mb-2 text-xs text-muted">{label}</p>{children}</div>;
}
