import Link from "next/link";
import { notFound } from "next/navigation";

import { CompanyListings } from "@/components/company-listings";
import { ApiError, getCompany } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function CompanyPage({ params }: Readonly<{ params: Promise<{ id: string }> }>): Promise<React.ReactNode> {
  const { id } = await params;
  let company;
  try {
    company = await getCompany(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  return (
    <section>
      <Link className="text-sm text-muted hover:text-accent hover:underline" href="/">← Overview</Link>
      <div className="mt-6 border-b border-line pb-6">
        <p className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-accent">Company identity</p>
        <h1 className="text-3xl font-semibold tracking-tight">{company.display_name}</h1>
        <p className="mt-2 text-sm text-muted">{company.legal_name}</p>
      </div>
      <dl className="mt-6 grid gap-px border border-line bg-line sm:grid-cols-2">
        <div className="bg-panel p-4"><dt className="text-xs uppercase tracking-[0.12em] text-muted">Sector</dt><dd className="mt-2 text-sm">{company.sector}</dd></div>
        <div className="bg-panel p-4"><dt className="text-xs uppercase tracking-[0.12em] text-muted">Industry</dt><dd className="mt-2 text-sm">{company.industry}</dd></div>
      </dl>
      <section className="mt-6 border border-line bg-panel">
        <div className="border-b border-line px-4 py-3"><h2 className="font-medium">Securities and exchange listings</h2><p className="mt-1 text-sm text-muted">Canonical company identity remains separate from exchange symbols.</p></div>
        <div className="divide-y divide-line">
          {company.securities.map((security) => (
            <article className="p-4" key={security.id}>
              <div className="grid gap-4 md:grid-cols-[1fr_2fr]">
                <div><p className="text-xs uppercase tracking-[0.12em] text-muted">ISIN</p><p className="mt-1 font-mono text-sm">{security.isin}</p><p className="mt-2 text-xs text-muted">{security.security_type} · {security.status}</p></div>
                <div><p className="text-xs uppercase tracking-[0.12em] text-muted">Listings</p><div className="mt-2"><CompanyListings listings={security.listings} /></div></div>
              </div>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}
