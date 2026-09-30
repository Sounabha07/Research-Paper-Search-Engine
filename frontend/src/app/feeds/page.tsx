import type { Metadata } from "next";

import { FeedsView } from "@/components/feeds";

export const metadata: Metadata = { title: "Feeds · arXiv Research Engine" };

export default function FeedsPage() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-8">
      <h1 className="text-xl font-semibold tracking-tight">Feeds</h1>
      <p className="text-sm text-muted-foreground">
        Weekly digests (Mondays) of new papers similar to a feed&apos;s query or mentioning its keywords.
      </p>
      <FeedsView />
    </main>
  );
}
