import type { OpportunityScoreChange } from "@/lib/opportunity-api";
import { CodeList, ExactValue, ResearchStatus, SignedValue } from "@/components/research/research-primitives";

export function LatestChange({ change }: Readonly<{ change: OpportunityScoreChange | null }>): React.ReactNode {
  if (change === null) {
    return (
      <section className="border border-line bg-panel p-4">
        <h2 className="font-medium">Latest persisted change</h2>
        <p className="mt-2 text-sm text-muted">No prior persisted V5 snapshot for comparison.</p>
      </section>
    );
  }

  return (
    <section className="border border-line bg-panel" aria-labelledby="change-heading">
      <div className="border-b border-line px-4 py-3">
        <h2 className="font-medium" id="change-heading">Latest persisted change</h2>
        <p className="mt-1 text-xs text-muted">Numerical and availability differences only; no directional interpretation.</p>
      </div>
      <div className="grid gap-px bg-line sm:grid-cols-2 xl:grid-cols-4">
        <div className="bg-panel p-3"><p className="text-xs text-muted">Status transition</p><div className="mt-2 flex flex-wrap items-center gap-2"><ResearchStatus status={change.comparison_status} /><span aria-hidden="true">→</span><ResearchStatus status={change.current_status} /></div></div>
        <div className="bg-panel p-3"><p className="text-xs text-muted">Final score delta</p><p className="mt-2"><SignedValue value={change.final_score_delta} /></p></div>
        <div className="bg-panel p-3"><p className="text-xs text-muted">Confidence delta</p><p className="mt-2"><SignedValue value={change.confidence_delta} /></p></div>
        <div className="bg-panel p-3"><p className="text-xs text-muted">Coverage delta</p><p className="mt-2"><SignedValue value={change.coverage_delta} /></p></div>
      </div>
      <div className="grid gap-4 border-t border-line p-4 md:grid-cols-2">
        <div><p className="mb-2 text-xs uppercase tracking-[0.1em] text-muted">Newly available</p><CodeList values={change.newly_available_component_codes} /></div>
        <div><p className="mb-2 text-xs uppercase tracking-[0.1em] text-muted">Newly missing</p><CodeList values={change.newly_missing_component_codes} /></div>
      </div>
      <div className="overflow-x-auto border-t border-line">
        <table className="w-full min-w-[720px] text-left text-xs">
          <caption className="sr-only">Component changes between the latest two persisted snapshots</caption>
          <thead className="bg-raised/50 uppercase tracking-[0.08em] text-muted"><tr><th className="px-3 py-2" scope="col">Component</th><th className="px-3 py-2 text-right" scope="col">Previous</th><th className="px-3 py-2 text-right" scope="col">Current</th><th className="px-3 py-2 text-right" scope="col">Delta</th><th className="px-3 py-2" scope="col">Availability</th></tr></thead>
          <tbody className="divide-y divide-line">
            {change.component_changes.map((component) => (
              <tr key={component.component_code}>
                <th className="px-3 py-2 font-mono font-normal" scope="row">{component.component_code}</th>
                <td className="px-3 py-2 text-right"><ExactValue unavailable="Previous component unavailable" value={component.comparison_score} /></td>
                <td className="px-3 py-2 text-right"><ExactValue unavailable="Current component unavailable" value={component.current_score} /></td>
                <td className="px-3 py-2 text-right"><SignedValue value={component.score_delta} /></td>
                <td className="px-3 py-2">{component.availability_change}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
