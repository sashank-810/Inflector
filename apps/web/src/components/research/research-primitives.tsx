import Link from "next/link";

import type { OpportunityStatus } from "@/lib/opportunity-api";
import { hrefWithQuery, type SearchParams } from "@/lib/research-query";

const statusLabels: Record<OpportunityStatus, string> = {
  final_score_available: "Final score available",
  partial_component_set: "Partial component set",
  implemented_components_unavailable: "Components unavailable",
  ineligible: "Ineligible"
};

export function ExactValue({
  value,
  unavailable = "Final score unavailable"
}: Readonly<{ value: string | null; unavailable?: string }>): React.ReactNode {
  return value === null ? (
    <span aria-label={unavailable} className="text-muted" title={unavailable}>—</span>
  ) : (
    <span className="font-mono tabular-nums">{value}</span>
  );
}

export function SignedValue({ value }: Readonly<{ value: string | null }>): React.ReactNode {
  if (value === null) return <span className="text-muted">not comparable</span>;
  const negative = value.startsWith("-");
  const zero = value === "0";
  const tone = negative ? "text-negative" : zero ? "text-muted" : "text-positive";
  const label = negative ? "negative" : zero ? "unchanged" : "positive";
  return (
    <span className={`${tone} font-mono tabular-nums`}>
      {value} <span className="font-sans text-[11px]">({label})</span>
    </span>
  );
}

export function ResearchStatus({ status }: Readonly<{ status: OpportunityStatus }>): React.ReactNode {
  return (
    <span className="inline-flex border border-line bg-raised px-2 py-1 text-xs text-ink">
      {statusLabels[status]}
    </span>
  );
}

export function ModelFamilyForm({ value, submitLabel = "Open research queue" }: Readonly<{ value?: string; submitLabel?: string }>): React.ReactNode {
  return (
    <form className="flex flex-wrap items-end gap-3" method="get">
      <label className="grid gap-1.5 text-xs text-muted">
        <span>Model family</span>
        <input
          className="h-9 min-w-64 border border-line bg-canvas px-3 font-mono text-sm text-ink"
          defaultValue={value}
          name="model_family"
          placeholder="Enter an exact model family"
          required
        />
      </label>
      <button className="h-9 border border-accent/70 bg-accent/10 px-4 text-sm text-ink hover:bg-accent/20" type="submit">
        {submitLabel}
      </button>
    </form>
  );
}

export function Pagination({
  pathname,
  searchParams,
  total,
  limit,
  offset,
  offsetKey = "offset",
  preserveSnapshot = true
}: Readonly<{
  pathname: string;
  searchParams: SearchParams;
  total: number;
  limit: number;
  offset: number;
  offsetKey?: string;
  preserveSnapshot?: boolean;
}>): React.ReactNode {
  const previousOffset = Math.max(0, offset - limit);
  const nextOffset = offset + limit;
  const commonUpdate = preserveSnapshot ? {} : { snapshot_id: null };
  return (
    <nav aria-label="Pagination" className="flex items-center justify-between gap-4 border-t border-line px-4 py-3 text-sm">
      <p className="text-muted">
        {total === 0 ? "0 records" : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`}
      </p>
      <div className="flex gap-2">
        {offset > 0 ? (
          <Link className="border border-line px-3 py-1.5 hover:bg-raised" href={hrefWithQuery(pathname, searchParams, { ...commonUpdate, [offsetKey]: previousOffset })}>
            Previous
          </Link>
        ) : (
          <span aria-disabled="true" className="border border-line px-3 py-1.5 text-muted">Previous</span>
        )}
        {nextOffset < total ? (
          <Link className="border border-line px-3 py-1.5 hover:bg-raised" href={hrefWithQuery(pathname, searchParams, { ...commonUpdate, [offsetKey]: nextOffset })}>
            Next
          </Link>
        ) : (
          <span aria-disabled="true" className="border border-line px-3 py-1.5 text-muted">Next</span>
        )}
      </div>
    </nav>
  );
}

export function ApiFailure({ message, retryHref }: Readonly<{ message: string; retryHref: string }>): React.ReactNode {
  return (
    <section className="border border-negative/50 bg-panel p-5" role="alert">
      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-negative">Research data unavailable</p>
      <h2 className="mt-2 text-lg font-semibold">The persisted research view could not be loaded.</h2>
      <p className="mt-2 text-sm text-muted">{message} Existing persisted data was not changed.</p>
      <Link className="mt-4 inline-block border border-line px-3 py-2 text-sm hover:bg-raised" href={retryHref}>Retry request</Link>
    </section>
  );
}

export function CodeList({ values, empty = "None" }: Readonly<{ values: string[]; empty?: string }>): React.ReactNode {
  return values.length === 0 ? (
    <span className="text-muted">{empty}</span>
  ) : (
    <ul className="flex flex-wrap gap-1.5">
      {values.map((value) => <li className="border border-line bg-canvas px-1.5 py-0.5 font-mono text-[11px]" key={value}>{value}</li>)}
    </ul>
  );
}
