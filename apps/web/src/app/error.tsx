"use client";

import { Button } from "@/components/ui/button";

export default function ErrorState({ reset }: Readonly<{ error: Error & { digest?: string }; reset: () => void }>): React.ReactNode {
  return (
    <section className="max-w-2xl border border-negative/50 bg-panel p-6" role="alert">
      <p className="mb-2 text-xs font-semibold uppercase tracking-[0.16em] text-negative">Data unavailable</p>
      <h1 className="text-xl font-semibold">The company universe could not be loaded.</h1>
      <p className="mt-2 text-sm leading-6 text-muted">Check that PostgreSQL and the local FastAPI service are running, then retry. No internal error details are exposed here.</p>
      <Button className="mt-5" onClick={reset}>Retry request</Button>
    </section>
  );
}
