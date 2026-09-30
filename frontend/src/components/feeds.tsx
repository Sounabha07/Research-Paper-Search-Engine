"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import type { Digest, Feed } from "@/lib/api-types";

function FeedCard({ feed, onDeleted }: { feed: Feed; onDeleted: () => void }) {
  const [digests, setDigests] = useState<Digest[] | null>(null);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetch(`/api/feeds/${feed.id}/digests?limit=4`, { cache: "no-store" });
    if (r.ok) setDigests((await r.json()) as Digest[]);
  }, [feed.id]);
  useEffect(() => {
    void load();
  }, [load]);

  const build = async () => {
    setBuilding(true);
    setError(null);
    const r = await fetch(`/api/feeds/${feed.id}/digests`, { method: "POST" });
    if (!r.ok) setError(`HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
    await load();
    setBuilding(false);
  };
  const remove = async () => {
    if (!window.confirm(`Delete the feed “${feed.name}” and its digests?`)) return;
    const r = await fetch(`/api/feeds/${feed.id}`, { method: "DELETE" });
    if (r.ok) onDeleted();
  };

  const latest = digests?.[0];
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <header className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-semibold">{feed.name}</h2>
          <p className="text-sm text-muted-foreground">{feed.query_text}</p>
          {feed.keywords.length > 0 && (
            <p className="mt-1 flex flex-wrap gap-1">
              {feed.keywords.map((k) => (
                <span key={k} className="rounded border border-border px-1.5 py-px text-[11px] text-muted-foreground">
                  {k}
                </span>
              ))}
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2 text-xs">
          <button onClick={build} disabled={building} className="rounded-md border border-border px-2 py-1 hover:bg-muted disabled:opacity-50">
            {building ? "Building…" : "Build digest now"}
          </button>
          <button onClick={remove} className="rounded-md border border-border px-2 py-1 text-destructive hover:bg-muted">
            Delete
          </button>
        </div>
      </header>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {digests === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : !latest ? (
        <p className="text-sm text-muted-foreground">No digest yet. The next one is built on Monday, or build one now.</p>
      ) : (
        <div className="flex flex-col gap-2 text-sm">
          <p className="text-xs text-muted-foreground">
            {latest.period} · papers since {new Date(latest.content.since).toLocaleDateString()} ·{" "}
            {latest.content.papers.length} of {latest.content.candidates} candidates
            {latest.content.model && ` · summarized by ${latest.content.model}`}
          </p>
          {latest.content.overview && <p>{latest.content.overview}</p>}
          <ol className="flex flex-col gap-1.5">
            {latest.content.papers.map((p) => (
              <li key={p.arxiv_id}>
                <Link href={`/papers/${p.arxiv_id}`} className="font-medium hover:underline">
                  {p.title}
                </Link>
                <span className="text-xs text-muted-foreground tabular-nums">
                  {" "}· relevance {p.relevance.toFixed(2)}
                  {p.keywords.length > 0 && ` · matches ${p.keywords.join(", ")}`}
                </span>
                {p.one_line && <span className="block text-muted-foreground">{p.one_line}</span>}
              </li>
            ))}
          </ol>
          {digests.length > 1 && (
            <p className="text-xs text-muted-foreground">Earlier digests: {digests.slice(1).map((d) => d.period).join(", ")}</p>
          )}
        </div>
      )}
    </section>
  );
}

export function FeedsView() {
  const [feeds, setFeeds] = useState<Feed[] | null>(null);
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [keywords, setKeywords] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetch("/api/feeds", { cache: "no-store" });
    if (r.ok) setFeeds((await r.json()) as Feed[]);
    else setError(`HTTP ${r.status}`);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    const r = await fetch("/api/feeds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), query_text: query.trim(), keywords: keywords.split(",").map((k) => k.trim()).filter(Boolean) }),
    });
    if (r.ok) {
      setName("");
      setQuery("");
      setKeywords("");
      await load();
    } else setError(`HTTP ${r.status}: ${(await r.text()).slice(0, 160)}`);
  };

  const input = "rounded-md border border-border bg-background px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring";
  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={create} className="flex flex-col gap-2 rounded-lg border border-border p-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">New feed</h2>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (e.g. Efficient LLM inference)" className={input} />
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="What you want to follow, in a sentence" className={input} />
        <input value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="Keywords, comma-separated (optional)" className={input} />
        <button type="submit" disabled={!name.trim() || query.trim().length < 3}
          className="self-start rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50">
          Create feed
        </button>
      </form>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {feeds === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : feeds.length === 0 ? (
        <p className="text-sm text-muted-foreground">No feeds yet.</p>
      ) : (
        feeds.map((f) => <FeedCard key={f.id} feed={f} onDeleted={load} />)
      )}
    </div>
  );
}
