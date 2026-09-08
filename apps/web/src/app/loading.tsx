export default function Loading(): React.ReactNode {
  return (
    <section aria-busy="true" aria-label="Loading company universe">
      <div className="mb-8 h-8 w-64 animate-pulse bg-raised" />
      <div className="border border-line bg-panel p-5">
        <div className="mb-5 h-4 w-48 animate-pulse bg-raised" />
        {[1, 2, 3].map((row) => <div className="mb-3 h-10 animate-pulse bg-raised/60" key={row} />)}
      </div>
    </section>
  );
}
