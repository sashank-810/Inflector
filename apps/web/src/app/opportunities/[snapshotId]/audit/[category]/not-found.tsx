import Link from "next/link";

export default function AuditCategoryNotFound(): React.ReactNode {
  return <section className="border border-line bg-panel p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">Not found</p><h1 className="mt-2 text-xl font-semibold">Opportunity score snapshot not found</h1><p className="mt-2 text-sm text-muted">The requested V5 audit resource was not available.</p><Link className="mt-4 inline-block text-sm text-accent hover:underline" href="/opportunities">Return to research queue</Link></section>;
}
