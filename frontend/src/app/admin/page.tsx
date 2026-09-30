import type { Metadata } from "next";

import { AdminStats } from "@/components/admin-stats";
import { ServiceStatus } from "@/components/service-status";

export const metadata: Metadata = { title: "Admin · arXiv Research Engine" };

export default function AdminPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 px-4 py-8">
      <h1 className="text-xl font-semibold tracking-tight">Admin</h1>
      <section className="rounded-lg border border-border p-4">
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Services</h2>
        <ServiceStatus />
      </section>
      <AdminStats />
    </main>
  );
}
