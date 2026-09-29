import { ServiceStatus } from "@/components/service-status";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-4 py-12">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Ask</h1>
        <p className="text-sm text-muted-foreground">
          Skeleton build. The ask box arrives in a later milestone.
        </p>
      </header>
      <section className="rounded-lg border border-border p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Services
        </h2>
        <ServiceStatus />
      </section>
    </main>
  );
}
