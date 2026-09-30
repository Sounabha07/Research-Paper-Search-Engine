import type { Metadata } from "next";

import { CollectionsList } from "@/components/collections";

export const metadata: Metadata = { title: "Collections · arXiv Research Engine" };

export default function CollectionsPage() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-8">
      <h1 className="text-xl font-semibold tracking-tight">Collections</h1>
      <CollectionsList />
    </main>
  );
}
