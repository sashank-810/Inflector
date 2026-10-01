import Link from "next/link";

import { CompanyListings } from "@/components/company-listings";
import { ExactValue, ResearchStatus } from "@/components/research/research-primitives";
import type { CompanyResearchContext } from "@/lib/opportunity-api";
import { hrefWithQuery } from "@/lib/research-query";

export function ContextSelector({
  companyId,
  contexts,
  modelFamily
}: Readonly<{
  companyId: string;
  contexts: CompanyResearchContext[];
  modelFamily: string;
}>): React.ReactNode {
  return (
    <section className="mt-6 border border-line bg-panel" aria-labelledby="context-heading">
      <div className="border-b border-line px-4 py-3">
        <h2 className="font-medium" id="context-heading">Choose an explicit research context</h2>
        <p className="mt-1 text-sm text-muted">No security or scoring configuration is selected automatically.</p>
      </div>
      <div className="divide-y divide-line">
        {contexts.map((context) => {
          const href = hrefWithQuery(`/companies/${companyId}`, {}, {
            model_family: modelFamily,
            security_id: context.security.security_id,
            scoring_configuration_id: context.scoring_configuration.id
          });
          return (
            <article className="grid gap-4 p-4 lg:grid-cols-[1fr_1.4fr_1fr_auto] lg:items-center" key={`${context.security.security_id}:${context.scoring_configuration.id}`}>
              <div>
                <p className="font-mono text-sm">{context.security.isin}</p>
                <p className="mt-1 text-xs text-muted">{context.security.security_type} · {context.security.security_status}</p>
                <div className="mt-2"><CompanyListings listings={context.security.listings.map((listing) => ({ ...listing, id: listing.id }))} /></div>
              </div>
              <div className="text-xs">
                <p>{context.model_version.model_family} · {context.model_version.semantic_version}</p>
                <p className="mt-1 text-muted">{context.scoring_configuration.configuration_name} · {context.scoring_configuration.configuration_version}</p>
                <p className="mt-1 break-all font-mono text-[11px] text-muted">{context.scoring_configuration.checksum_sha256}</p>
              </div>
              <div className="grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-1">
                <ResearchStatus status={context.latest_snapshot.snapshot_status} />
                <p>Score <ExactValue value={context.latest_snapshot.final_score} /></p>
                <p>Coverage <span className="font-mono">{context.latest_snapshot.top_level_component_weight_coverage}</span></p>
                <p className="font-mono text-muted">{context.latest_snapshot.knowledge_cutoff}</p>
              </div>
              <Link className="border border-accent/60 px-3 py-2 text-center text-sm hover:bg-accent/10" href={href}>Open context</Link>
            </article>
          );
        })}
      </div>
    </section>
  );
}
