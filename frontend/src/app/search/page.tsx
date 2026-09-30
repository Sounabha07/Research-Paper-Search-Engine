import type { Metadata } from "next";
import { Suspense } from "react";

import { SearchView } from "@/components/search-view";

export const metadata: Metadata = { title: "Search · arXiv Research Engine" };

export default function SearchPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-5 px-4 py-8">
      <h1 className="text-xl font-semibold tracking-tight">Search</h1>
      <Suspense>
        <SearchView />
      </Suspense>
    </main>
  );
}
