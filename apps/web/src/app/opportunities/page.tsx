export default function OpportunitiesPage(): React.ReactNode { return <Placeholder title="Opportunities" description="The research opportunity queue begins after Phase 4 scoring is available." />; }

function Placeholder({ title, description }: Readonly<{ title: string; description: string }>): React.ReactNode { return <section className="border border-line bg-panel p-6"><p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">Planned workflow</p><h1 className="mt-2 text-xl font-semibold">{title}</h1><p className="mt-2 text-sm text-muted">{description}</p></section>; }
