import Link from "next/link";

import type { OpportunityScoreHistoryResponse } from "@/lib/opportunity-api";
import { hrefWithQuery, type SearchParams } from "@/lib/research-query";
import { ExactValue, Pagination, ResearchStatus } from "@/components/research/research-primitives";

export function HistoryTable({
  history,
  pathname,
  searchParams
}: Readonly<{
  history: OpportunityScoreHistoryResponse;
  pathname: string;
  searchParams: SearchParams;
}>): React.ReactNode {
  return (
    <section className="overflow-hidden border border-line bg-panel" aria-labelledby="history-heading">
      <div className="border-b border-line px-4 py-3">
        <h2 className="font-medium" id="history-heading">Exact-context V5 history</h2>
        <p className="mt-1 text-xs text-muted">
          History is ordered by knowledge cutoff, fiscal endpoint, then immutable fingerprint; persistence time does not determine semantic order.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-left text-xs">
          <caption className="sr-only">Persisted V5 snapshot history for this security and scoring configuration</caption>
          <thead className="bg-raised/50 uppercase tracking-[0.08em] text-muted">
            <tr><th className="px-3 py-2" scope="col">Knowledge cutoff</th><th className="px-3 py-2" scope="col">FY/Q</th><th className="px-3 py-2" scope="col">Snapshot state</th><th className="px-3 py-2 text-right" scope="col">Opportunity Score</th><th className="px-3 py-2 text-right" scope="col">Confidence</th><th className="px-3 py-2 text-right" scope="col">Coverage</th><th className="px-3 py-2" scope="col">Fingerprint</th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {history.items.map((snapshot) => (
              <tr className="hover:bg-raised/30" key={snapshot.snapshot_id}>
                <td className="px-3 py-2"><Link className="font-mono hover:text-accent hover:underline" href={hrefWithQuery(pathname, searchParams, { snapshot_id: snapshot.snapshot_id })}>{snapshot.knowledge_cutoff}</Link></td>
                <td className="px-3 py-2 font-mono">FY{snapshot.ending_fiscal_year} Q{snapshot.ending_fiscal_quarter}</td>
                <td className="px-3 py-2"><ResearchStatus status={snapshot.snapshot_status} /></td>
                <td className="px-3 py-2 text-right"><ExactValue value={snapshot.final_score} /></td>
                <td className="px-3 py-2 text-right font-mono">{snapshot.confidence}</td>
                <td className="px-3 py-2 text-right font-mono">{snapshot.top_level_component_weight_coverage}</td>
                <td className="max-w-72 truncate px-3 py-2 font-mono text-[11px] text-muted" title={snapshot.snapshot_fingerprint_sha256}>{snapshot.snapshot_fingerprint_sha256}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination
        limit={history.limit}
        offset={history.offset}
        offsetKey="history_offset"
        pathname={pathname}
        searchParams={searchParams}
        total={history.total}
      />
    </section>
  );
}
