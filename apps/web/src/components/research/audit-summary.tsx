import Link from "next/link";

import type { OpportunityAuditSummary } from "@/lib/opportunity-api";

export function AuditSummary({ audit }: Readonly<{ audit: OpportunityAuditSummary }>): React.ReactNode {
  return (
    <section className="border border-line bg-panel" aria-labelledby="audit-heading">
      <div className="border-b border-line px-4 py-3">
        <h2 className="font-medium" id="audit-heading">Persisted snapshot audit</h2>
        <p className="mt-1 break-all font-mono text-[11px] text-muted">{audit.snapshot_fingerprint_sha256}</p>
      </div>
      <dl className="grid gap-px bg-line text-xs sm:grid-cols-3">
        <AuditIdentity label="Snapshot algorithm" value={audit.algorithm_version} />
        <AuditIdentity label="Curve algorithm" value={audit.curve_algorithm_version} />
        <AuditIdentity label="Aggregation" value={audit.opportunity_score_aggregation_version} />
      </dl>
      <div className="grid gap-4 border-t border-line p-4 lg:grid-cols-2">
        <AuditAlgorithms label="Component algorithm identities" value={audit.component_algorithm_versions} />
        <AuditAlgorithms label="Phase-3 algorithm identities" value={audit.phase3_algorithm_versions} />
      </div>
      <div className="border-t border-line">
        <h3 className="px-4 pt-4 text-xs font-semibold uppercase tracking-[0.1em] text-muted">Evidence categories</h3>
        <ul className="mt-3 grid gap-px bg-line sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {audit.categories.map(({ category, count }) => (
            <li className="flex items-center justify-between gap-3 bg-panel px-4 py-3 text-xs" key={category}>
              <span className="font-mono">{category}</span>
              {count > 0 ? (
                <Link className="font-mono text-accent hover:underline" href={`/opportunities/${audit.snapshot_id}/audit/${category}`}>{count}<span className="sr-only"> items; open category</span></Link>
              ) : <span className="font-mono text-muted">0</span>}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function AuditIdentity({ label, value }: Readonly<{ label: string; value: string }>): React.ReactNode {
  return <div className="bg-panel p-3"><dt className="text-muted">{label}</dt><dd className="mt-1 break-all font-mono">{value}</dd></div>;
}

function AuditAlgorithms({ label, value }: Readonly<{ label: string; value: Record<string, unknown>[] }>): React.ReactNode {
  return <details className="border border-line"><summary className="cursor-pointer px-3 py-2 text-xs hover:bg-raised">{label} ({value.length})</summary><pre className="max-h-80 overflow-auto border-t border-line p-3 text-[11px] leading-5 text-muted">{JSON.stringify(value, null, 2)}</pre></details>;
}
