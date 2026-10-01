import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiFailure, Pagination } from "@/components/research/research-primitives";
import { ApiError } from "@/lib/api";
import { auditCategories, getOpportunityScoreAuditCategory } from "@/lib/opportunity-api";
import { nonNegativeInteger, positiveInteger, type SearchParams } from "@/lib/research-query";

export const dynamic = "force-dynamic";

export default async function AuditCategoryPage({
  params,
  searchParams
}: Readonly<{
  params: Promise<{ snapshotId: string; category: string }>;
  searchParams: Promise<SearchParams>;
}>): Promise<React.ReactNode> {
  const { snapshotId, category } = await params;
  const query = await searchParams;
  const limit = positiveInteger(query.limit, 50, 100);
  const offset = nonNegativeInteger(query.offset, 0);
  const pathname = `/opportunities/${snapshotId}/audit/${category}`;
  const auditCategory = auditCategories.find((candidate) => candidate === category);
  if (!auditCategory) {
    return <ApiFailure message="The requested audit category is not supported." retryHref="/opportunities" />;
  }

  let audit;
  try {
    audit = await getOpportunityScoreAuditCategory(snapshotId, auditCategory, limit, offset);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    const message = error instanceof ApiError ? error.message : "The persisted audit category is unavailable.";
    return <ApiFailure message={message} retryHref={pathname} />;
  }

  return (
    <section>
      <Link className="text-sm text-muted hover:text-accent hover:underline" href="/opportunities">← Research queue</Link>
      <div className="mt-6 border-b border-line pb-5">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-accent">Bounded deep audit</p>
        <h1 className="mt-2 text-2xl font-semibold">{audit.category}</h1>
        <p className="mt-2 break-all font-mono text-[11px] text-muted">Snapshot fingerprint: {audit.snapshot_fingerprint_sha256}</p>
        <p className="mt-2 text-sm text-muted">Persisted semantic items in their stored order. No live evidence is fetched or substituted.</p>
      </div>
      {audit.total === 0 ? (
        <div className="mt-6 border border-line bg-panel p-5"><h2 className="font-medium">This persisted audit category contains 0 items.</h2><p className="mt-2 text-sm text-muted">No synthetic evidence was created for the empty category.</p></div>
      ) : (
        <section className="mt-6 overflow-hidden border border-line bg-panel" aria-labelledby="items-heading">
          <div className="border-b border-line px-4 py-3"><h2 className="font-medium" id="items-heading">Persisted snapshot audit items</h2><p className="mt-1 text-xs text-muted">{audit.total} immutable items</p></div>
          <ol className="divide-y divide-line">
            {audit.items.map((item, index) => (
              <li className="p-4" key={`${audit.offset + index}`}>
                <p className="mb-2 text-xs font-semibold uppercase tracking-[0.1em] text-muted">Persisted snapshot audit item {audit.offset + index + 1}</p>
                <pre className="max-h-[32rem] overflow-auto border border-line bg-canvas p-3 text-[11px] leading-5 text-muted">{JSON.stringify(item, null, 2)}</pre>
              </li>
            ))}
          </ol>
          <Pagination limit={audit.limit} offset={audit.offset} pathname={pathname} searchParams={query} total={audit.total} />
        </section>
      )}
    </section>
  );
}
