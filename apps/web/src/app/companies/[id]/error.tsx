"use client";

export default function CompanyResearchError({ reset }: Readonly<{ reset: () => void }>): React.ReactNode {
  return <section className="border border-negative/50 bg-panel p-6" role="alert"><h1 className="text-xl font-semibold">The company research dossier could not be loaded.</h1><p className="mt-2 text-sm text-muted">Existing company and snapshot data was not changed.</p><button className="mt-4 border border-line px-3 py-2 text-sm hover:bg-raised" onClick={reset} type="button">Retry request</button></section>;
}
