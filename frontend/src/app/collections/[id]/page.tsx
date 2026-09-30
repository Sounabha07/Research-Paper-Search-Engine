import { CollectionView } from "@/components/collections";

export default async function CollectionPage({ params }: PageProps<"/collections/[id]">) {
  const { id } = await params;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-8">
      <CollectionView id={id} />
    </main>
  );
}
