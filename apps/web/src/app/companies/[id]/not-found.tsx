import Link from "next/link";

export default function CompanyNotFound(): React.ReactNode {
  return <section className="border border-line bg-panel p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">Not found</p><h1 className="mt-2 text-xl font-semibold">Company not found</h1><p className="mt-2 text-sm text-muted">This canonical company identity does not exist in the local universe.</p><Link className="mt-5 inline-block text-sm text-accent hover:underline" href="/">Return to Overview</Link></section>;
}
