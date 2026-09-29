import type { Metadata } from "next";

import { AdminStats } from "@/components/admin-stats";

export const metadata: Metadata = { title: "Admin · arXiv Research Engine" };

export default function AdminPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 px-4 py-8">
      <h1 className="text-xl font-semibold tracking-tight">Admin</h1>
      <AdminStats />
    </main>
  );
}
