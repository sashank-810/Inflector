import type { OpportunityComponentDetail } from "@/lib/opportunity-api";
import { CodeList, ExactValue } from "@/components/research/research-primitives";

export function ComponentDetailTable({ components }: Readonly<{ components: OpportunityComponentDetail[] }>): React.ReactNode {
  return (
    <section className="border border-line bg-panel" aria-labelledby="components-heading">
      <div className="border-b border-line px-4 py-3"><h2 className="font-medium" id="components-heading">Persisted component audit</h2><p className="mt-1 text-xs text-muted">Available components in canonical persisted order. No values are recalculated in the browser.</p></div>
      <div className="divide-y divide-line">
        {components.map((component) => (
          <article className="p-4" key={component.component_code}>
            <div className="grid gap-3 md:grid-cols-4 xl:grid-cols-8">
              <div className="md:col-span-2"><p className="text-xs text-muted">Component</p><h3 className="mt-1 font-mono text-sm">{component.component_code}</h3><p className="mt-1 text-xs text-muted">{component.algorithm_version}</p></div>
              <Metric label="Score" value={component.score} />
              <Metric label="Unit" value={component.unit} mono={false} />
              <Metric label="Top-level weight" value={component.configured_top_level_weight} />
              <Metric label="Subfactor coverage" value={component.subfactor_weight_coverage} />
              <div><p className="text-xs text-muted">Final contribution</p><p className="mt-1"><ExactValue unavailable="Final contribution unavailable" value={component.final_contribution} /></p></div>
              <Metric label="Available at" value={component.available_at ?? "—"} />
            </div>
            {(component.missing_subfactors.length > 0 || component.warnings.length > 0) && (
              <div className="mt-4 grid gap-3 md:grid-cols-2"><div><p className="mb-1 text-xs text-muted">Missing subfactors</p><CodeList values={component.missing_subfactors} /></div><div><p className="mb-1 text-xs text-muted">Warnings</p><CodeList values={component.warnings} /></div></div>
            )}
            <details className="mt-4 border border-line bg-canvas">
              <summary className="cursor-pointer px-3 py-2 text-sm hover:bg-raised">Explanations ({component.explanations.length})</summary>
              <div className="divide-y divide-line border-t border-line">
                {component.explanations.length === 0 ? <p className="p-3 text-sm text-muted">No persisted explanations for this component.</p> : component.explanations.map((explanation) => (
                  <article className="p-3" key={explanation.id}>
                    <div className="grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
                      <Metric label="Factor / rank" value={`${explanation.factor_code} / ${explanation.rank}`} />
                      <Metric label="Raw value" value={`${explanation.raw_value} ${explanation.raw_unit}`} />
                      <Metric label="Normalized score" value={explanation.normalized_score} />
                      <Metric label="Configured / effective weight" value={`${explanation.configured_weight} / ${explanation.effective_weight}`} />
                      <Metric label="Component contribution" value={explanation.component_contribution} />
                      <Metric label="Input available at" value={explanation.input_available_at} />
                      <Metric label="Evidence / template" value={`${explanation.evidence_type} / ${explanation.template_code}`} />
                      <Metric label="Direction" value={explanation.direction ?? "Not specified"} mono={false} />
                    </div>
                    <details className="mt-3 border border-line">
                      <summary className="cursor-pointer px-3 py-2 text-xs text-muted hover:bg-raised">Persisted evidence manifest</summary>
                      <pre className="max-h-96 overflow-auto border-t border-line p-3 text-[11px] leading-5 text-muted">{JSON.stringify(explanation.evidence_manifest, null, 2)}</pre>
                    </details>
                  </article>
                ))}
              </div>
            </details>
          </article>
        ))}
      </div>
    </section>
  );
}

function Metric({ label, value, mono = true }: Readonly<{ label: string; value: string; mono?: boolean }>): React.ReactNode {
  return <div><p className="text-xs text-muted">{label}</p><p className={`mt-1 break-words text-xs ${mono ? "font-mono" : ""}`}>{value}</p></div>;
}
