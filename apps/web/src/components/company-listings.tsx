import type { Listing } from "@/lib/api";

export function CompanyListings({ listings }: Readonly<{ listings: Listing[] }>) {
  if (listings.length === 0) {
    return <span className="text-muted">—</span>;
  }

  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="Exchange listings">
      {listings.map((listing) => (
        <li className="border border-line bg-canvas px-1.5 py-0.5 font-mono text-xs text-ink" key={listing.id}>
          <span className="text-muted">{listing.exchange}</span> {listing.symbol}
        </li>
      ))}
    </ul>
  );
}
