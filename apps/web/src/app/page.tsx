import Link from "next/link";

import { CompanyListings } from "@/components/company-listings";
import { getCompanies } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function OverviewPage(): Promise<React.ReactNode> {
  const { items, total } = await getCompanies();

  return (
    <section>
      <div className="mb-7 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-accent">Overview</p>
          <h1 className="text-2xl font-semibold tracking-tight">Company identity universe</h1>
          <p className="mt-2 max-w-2xl text-sm text-muted">Seeded fictional issuers retrieved from the local Inflector API. Financial data and opportunity scores are intentionally not yet available.</p>
        </div>
        <p className="font-mono text-sm text-muted">{total} companies</p>
      </div>
      {items.length === 0 ? (
        <div className="border border-line bg-panel p-6">
          <h2 className="font-medium">No companies are seeded</h2>
          <p className="mt-2 text-sm text-muted">Run the documented seed command after applying database migrations.</p>
        </div>
      ) : (
        <div className="overflow-x-auto border border-line bg-panel">
          <table className="w-full min-w-[720px] text-left text-sm">
            <caption className="sr-only">Seeded company identity universe</caption>
            <thead className="border-b border-line bg-raised/50 text-xs uppercase tracking-[0.1em] text-muted">
              <tr><th className="px-4 py-3 font-medium">Company</th><th className="px-4 py-3 font-medium">Sector</th><th className="px-4 py-3 font-medium">Industry</th><th className="px-4 py-3 font-medium">Listings</th></tr>
            </thead>
            <tbody className="divide-y divide-line">
              {items.map((company) => (
                <tr className="hover:bg-raised/40" key={company.id}>
                  <td className="px-4 py-3 font-medium"><Link className="hover:text-accent hover:underline" href={`/companies/${company.id}`}>{company.display_name}</Link></td>
                  <td className="px-4 py-3 text-muted">{company.sector}</td>
                  <td className="px-4 py-3 text-muted">{company.industry}</td>
                  <td className="px-4 py-3"><CompanyListings listings={company.securities.flatMap((security) => security.listings)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
