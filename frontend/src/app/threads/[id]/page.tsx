import { ThreadView } from "@/components/thread-view";

export default async function ThreadPage({ params }: PageProps<"/threads/[id]">) {
  const { id } = await params;
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-6 px-4 py-8">
      <ThreadView threadId={id} />
    </main>
  );
}
