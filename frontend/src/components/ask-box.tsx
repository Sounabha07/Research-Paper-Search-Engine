"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

type RecentThread = { id: string; title: string; turns: number; last_status: string | null; updated_at: string };

export function AskBox() {
  const router = useRouter();
  const ref = useRef<HTMLTextAreaElement>(null);
  const [question, setQuestion] = useState("");
  const [yearFrom, setYearFrom] = useState("");
  const [yearTo, setYearTo] = useState("");
  const [categories, setCategories] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState<RecentThread[]>([]);

  useEffect(() => {
    fetch("/api/threads?limit=15", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : []))
      .then(setRecent)
      .catch(() => setRecent([]));
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA"].includes(t.tagName)) {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (question.trim().length < 3) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/threads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: question.trim(),
          mode: "quick",
          filters: {
            year_from: yearFrom ? Number(yearFrom) : null,
            year_to: yearTo ? Number(yearTo) : null,
            categories: categories.split(",").map((s) => s.trim()).filter(Boolean),
          },
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
      const body = (await res.json()) as { thread_id: string };
      router.push(`/threads/${body.thread_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "request failed");
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={submit} className="flex flex-col gap-2">
        <textarea
          ref={ref}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) submit(e);
          }}
          rows={3}
          placeholder="Ask a research question…  (press / to focus, Enter to send)"
          className="w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <div className="flex overflow-hidden rounded-md border border-border text-xs">
            <span className="bg-primary px-2.5 py-1 text-primary-foreground">Quick</span>
            <span className="px-2.5 py-1 text-muted-foreground" title="Deep mode arrives in milestone 6">
              Deep
            </span>
          </div>
          <input value={yearFrom} onChange={(e) => setYearFrom(e.target.value)} placeholder="from year" inputMode="numeric"
            className="w-24 rounded-md border border-border bg-background px-2 py-1" />
          <input value={yearTo} onChange={(e) => setYearTo(e.target.value)} placeholder="to year" inputMode="numeric"
            className="w-24 rounded-md border border-border bg-background px-2 py-1" />
          <input value={categories} onChange={(e) => setCategories(e.target.value)} placeholder="categories, e.g. cs.LG, math"
            className="min-w-40 flex-1 rounded-md border border-border bg-background px-2 py-1" />
          <button type="submit" disabled={busy}
            className="rounded-md bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50">
            {busy ? "Starting…" : "Ask"}
          </button>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
      </form>

      {recent.length > 0 && (
        <section>
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Recent</h2>
          <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {recent.map((t) => (
              <li key={t.id}>
                <Link href={`/threads/${t.id}`} className="flex items-baseline gap-3 px-3 py-2 text-sm hover:bg-muted/50">
                  <span className="min-w-0 flex-1 truncate">{t.title}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {t.turns} {t.turns === 1 ? "turn" : "turns"} · {t.last_status}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
