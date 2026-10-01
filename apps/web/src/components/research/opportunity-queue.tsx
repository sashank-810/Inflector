import Link from "next/link";

import type { OpportunitySnapshotSummary } from "@/lib/opportunity-api";
import { hrefWithQuery, type SearchParams } from "@/lib/research-query";
import { CodeList, ExactValue, Pagination, ResearchStatus } from "@/components/research/research-primitives";

export function OpportunityQueue({
  items,
  total,
  limit,
  offset,
  modelFamily,
  searchParams
}: Readonly<{
  items: OpportunitySnapshotSummary[];
  total: number;
  limit: number;
  offset: number;
  modelFamily: string;
  searchParams: SearchParams;
}>): React.ReactNode {
  return (
    <section className="overflow-hidden border border-line bg-panel" aria-labelledby="queue-heading">
      <div className="border-b border-line px-4 py-3">
        <h2 className="font-medium" id="queue-heading">Persisted research contexts</h2>
        <p className="mt-1 text-xs text-muted">Direct sorting of stored V5 fields; rows are not recommendations or a composite rank.</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1280px] text-left text-xs">
          <caption className="sr-only">Latest persisted V5 snapshot for each explicit company, security, and configuration context</caption>
          <thead className="border-b border-line bg-raised/50 uppercase tracking-[0.08em] text-muted">
            <tr>
              <th className="px-3 py-2 font-medium" scope="col">Company</th>
              <th className="px-3 py-2 font-medium" scope="col">Security</th>
              <th className="px-3 py-2 text-right font-medium" scope="col">Opportunity Score</th>
              <th className="px-3 py-2 font-medium" scope="col">Snapshot state</th>
              <th className="px-3 py-2 text-right font-medium" scope="col">Confidence</th>
              <th className="px-3 py-2 text-right font-medium" scope="col">Coverage</th>
              <th className="px-3 py-2 font-medium" scope="col">Available / missing</th>
              <th className="px-3 py-2 font-medium" scope="col">Fiscal endpoint</th>
              <th className="px-3 py-2 font-medium" scope="col">Knowledge cutoff</th>
              <th className="px-3 py-2 font-medium" scope="col">Configuration</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {items.map((item) => {
              const href = hrefWithQuery(`/companies/${item.company.company_id}`, {}, {
                model_family: modelFamily,
                security_id: item.security.security_id,
                scoring_configuration_id: item.scoring_configuration_id
              });
              return (
                <tr className="align-top hover:bg-raised/30" key={item.snapshot_id}>
                  <td className="px-3 py-3">
                    <Link className="font-medium text-ink hover:text-accent hover:underline" href={href}>{item.company.display_name}</Link>
                    <p className="mt-1 text-muted">{item.company.sector} · {item.company.industry}</p>
                  </td>
                  <td className="px-3 py-3 font-mono">
                    {item.security.isin}
                    <p className="mt-1 font-sans text-muted">{item.security.security_type} · {item.security.security_status}</p>
                  </td>
                  <td className="px-3 py-3 text-right text-sm"><ExactValue value={item.final_score} /></td>
                  <td className="px-3 py-3"><ResearchStatus status={item.snapshot_status} /></td>
                  <td className="px-3 py-3 text-right font-mono">{item.confidence}</td>
                  <td className="px-3 py-3 text-right font-mono">{item.top_level_component_weight_coverage}</td>
                  <td className="max-w-72 px-3 py-3">
                    <p className="mb-1 text-muted">{item.available_component_codes.length} available · {item.missing_component_codes.length} missing</p>
                    <CodeList empty="No missing components" values={item.missing_component_codes} />
                  </td>
                  <td className="px-3 py-3 font-mono">FY{item.ending_fiscal_year} Q{item.ending_fiscal_quarter}</td>
                  <td className="px-3 py-3 font-mono text-muted">{item.knowledge_cutoff}</td>
                  <td className="max-w-56 px-3 py-3 font-mono text-[11px] text-muted break-all">{item.configuration_checksum_sha256}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pagination limit={limit} offset={offset} pathname="/opportunities" searchParams={searchParams} total={total} />
    </section>
  );
}
