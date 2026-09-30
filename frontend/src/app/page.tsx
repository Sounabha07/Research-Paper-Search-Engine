import { AskBox } from "@/components/ask-box";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-4 py-10">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Ask</h1>
        <p className="text-sm text-muted-foreground">
          Answers from arXiv CS and math papers. Every sentence cites the passage it came from; unsupported
          sentences are removed.
        </p>
      </header>
      <AskBox />
    </main>
  );
}
