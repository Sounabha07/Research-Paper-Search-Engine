"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

type Paper = {
  arxiv_id: string;
  title: string;
  year: number | null;
  authors: string[];
  primary_category: string | null;
  badges: Record<string, unknown>;
  full_text: boolean;
  rerank_score: number;
  prior: number;
  final_score: number;
};

type Passage = {
  arxiv_id: string;
  source: "chunk" | "abstract";
  chunk_id: number | null;
  section: string;
  text: string;
  rerank_score: number;
};

type SearchResponse = {
  query: string;
  papers: Paper[];
  passages: Passage[];
  timings_ms: Record<string, number>;
  reranker_device: string;
};

function sum(t: Record<string, number>, ...keys: string[]): number {
  return keys.reduce((acc, k) => acc + (t[k] ?? 0), 0);
}

function authorLine(authors: string[]): string {
  return authors.length > 3 ? `${authors.slice(0, 3).join(", ")} et al.` : authors.join(", ");
}

function Badges({ badges }: { badges: Record<string, unknown> }) {
  const items: string[] = [];
  if (badges.landmark) items.push("landmark");
  if (typeof badges.venue === "string" && badges.venue) items.push(badges.venue);
  if (badges.code_available) items.push("code");
  if (badges.is_survey) items.push("survey");
  if (badges.is_benchmark) items.push("benchmark");
  const cites = Number(badges.fulltext_citations ?? 0);
  if (cites > 0) items.push(`cited by ${cites} full-text papers in this corpus`);
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((b) => (
        <span key={b} className="rounded border border-border px-1.5 py-px text-[11px] text-muted-foreground">
          {b}
        </span>
      ))}
    </span>
  );
}

function FullTextButton({ arxivId }: { arxivId: string }) {
  const [status, setStatus] = useState<string | null>(null);
  const request = async () => {
    setStatus("requesting…");
    try {
      const res = await fetch(`/api/papers/${arxivId}/fulltext`, { method: "POST" });
      const body = (await res.json()) as { status?: string; detail?: string };
      setStatus(body.status ?? body.detail ?? `HTTP ${res.status}`);
    } catch {
      setStatus("request failed");
    }
  };
  if (status) return <span className="text-xs text-muted-foreground">full text: {status}</span>;
  return (
    <button onClick={request} className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground">
      Get full text
    </button>
  );
}

export function SearchView() {
  const router = useRouter();
  const params = useSearchParams();
  const inputRef = useRef<HTMLInputElement>(null);
  const [q, setQ] = useState(params.get("q") ?? "");
  const [yearFrom, setYearFrom] = useState(params.get("year_from") ?? "");
  const [yearTo, setYearTo] = useState(params.get("year_to") ?? "");
  const [categories, setCategories] = useState(params.get("categories") ?? "");
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const run = useCallback(async (sp: URLSearchParams) => {
    const query = sp.get("q");
    if (!query) return;
    const api = new URLSearchParams({ q: query });
    if (sp.get("year_from")) api.set("year_from", sp.get("year_from")!);
    if (sp.get("year_to")) api.set("year_to", sp.get("year_to")!);
    for (const c of (sp.get("categories") ?? "").split(",").map((s) => s.trim()).filter(Boolean)) {
      api.append("categories", c);
    }
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/search?${api}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
      setResult((await res.json()) as SearchResponse);
    } catch (e) {
      setError(e instanceof Error ? e.message : "search failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    run(new URLSearchParams(params.toString()));
  }, [params, run]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA"].includes(target.tagName)) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const sp = new URLSearchParams();
    if (q.trim()) sp.set("q", q.trim());
    if (yearFrom) sp.set("year_from", yearFrom);
    if (yearTo) sp.set("year_to", yearTo);
    if (categories.trim()) sp.set("categories", categories.trim());
    router.push(`/search?${sp}`);
  };

  const t = result?.timings_ms ?? {};
  const passagesBy = new Map<string, Passage[]>();
  for (const p of result?.passages ?? []) {
    passagesBy.set(p.arxiv_id, [...(passagesBy.get(p.arxiv_id) ?? []), p]);
  }

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={submit} className="flex flex-col gap-2">
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search papers…  (press / to focus)"
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <input value={yearFrom} onChange={(e) => setYearFrom(e.target.value)} placeholder="from year" inputMode="numeric"
            className="w-24 rounded-md border border-border bg-background px-2 py-1" />
          <input value={yearTo} onChange={(e) => setYearTo(e.target.value)} placeholder="to year" inputMode="numeric"
            className="w-24 rounded-md border border-border bg-background px-2 py-1" />
          <input value={categories} onChange={(e) => setCategories(e.target.value)} placeholder="categories, e.g. cs.LG, math"
            className="min-w-48 flex-1 rounded-md border border-border bg-background px-2 py-1" />
          <button type="submit" disabled={loading}
            className="rounded-md bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50">
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
      </form>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {result && (
        <>
          <p className="text-xs text-muted-foreground tabular-nums">
            {result.papers.length} papers · {Math.round(t.total ?? 0)} ms total (keyword{" "}
            {Math.round(sum(t, "papers.keyword", "passages.keyword"))}, vector{" "}
            {Math.round(sum(t, "papers.vector", "passages.vector", "embed_queries"))}, fusion{" "}
            {sum(t, "papers.fusion", "passages.fusion").toFixed(1)}, rerank{" "}
            {Math.round(sum(t, "papers.rerank", "passages.rerank"))}) · reranker on {result.reranker_device}
          </p>
          <ol className="flex flex-col gap-3">
            {result.papers.map((p, i) => (
              <li key={p.arxiv_id} className="rounded-lg border border-border p-3">
                <div className="flex items-baseline gap-2">
                  <span className="w-5 shrink-0 text-right text-xs text-muted-foreground tabular-nums">{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <a href={`https://arxiv.org/abs/${p.arxiv_id}`} target="_blank" rel="noreferrer"
                      className="font-medium hover:underline">
                      {p.title}
                    </a>
                    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                      <span>{p.year}</span>
                      <span className="truncate">{authorLine(p.authors)}</span>
                      <span className="font-mono">{p.arxiv_id}</span>
                      <span>{p.full_text ? "full text" : "abstract only"}</span>
                      {!p.full_text && <FullTextButton arxivId={p.arxiv_id} />}
                      <span className="tabular-nums">rerank {p.rerank_score.toFixed(3)}</span>
                    </div>
                    <div className="mt-1">
                      <Badges badges={p.badges} />
                    </div>
                    {(passagesBy.get(p.arxiv_id) ?? []).map((x) => (
                      <blockquote key={`${x.source}-${x.chunk_id}`}
                        className="mt-2 border-l-2 border-border pl-3 text-sm text-muted-foreground">
                        <span className="mr-1 text-[11px] uppercase tracking-wide">{x.section}</span>
                        <span className="line-clamp-4">{x.text}</span>
                      </blockquote>
                    ))}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </>
      )}
    </div>
  );
}
