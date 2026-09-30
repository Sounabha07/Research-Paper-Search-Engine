import { Suspense } from "react";

import { PaperReader } from "@/components/paper-reader";

export default async function PaperPage({ params }: PageProps<"/papers/[...id]">) {
  const { id } = await params;
  const arxivId = id.map(decodeURIComponent).join("/"); // old-style ids contain a slash (math/0211159)
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-8">
      <Suspense>
        <PaperReader arxivId={arxivId} />
      </Suspense>
    </main>
  );
}
