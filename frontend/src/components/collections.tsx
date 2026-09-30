"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Badges } from "@/components/badges";
import { authorLine, type Collection, type CollectionSummary } from "@/lib/api-types";

export function CollectionsList() {
  const [list, setList] = useState<CollectionSummary[] | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await fetch("/api/collections", { cache: "no-store" });
    if (r.ok) setList((await r.json()) as CollectionSummary[]);
    else setError(`HTTP ${r.status}`);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    const r = await fetch("/api/collections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (r.ok) {
      setName("");
      await load();
    } else setError(`HTTP ${r.status}`);
  };

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={create} className="flex gap-2">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New collection name…"
          className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring" />
        <button type="submit" disabled={!name.trim()}
          className="rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground disabled:opacity-50">
          Create
        </button>
      </form>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {list === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : list.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No collections yet. Save papers from a paper page, or answers from a thread, with “Save to collection”.
        </p>
      ) : (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
          {list.map((c) => (
            <li key={c.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-sm">
              <Link href={`/collections/${c.id}`} className="min-w-0 truncate font-medium hover:underline">
                {c.name}
              </Link>
              <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                {c.items} {c.items === 1 ? "item" : "items"} · {new Date(c.updated_at).toLocaleDateString()}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function CollectionView({ id }: { id: string }) {
  const router = useRouter();
  const [c, setC] = useState<Collection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const load = useCallback(async () => {
    const r = await fetch(`/api/collections/${id}`, { cache: "no-store" });
    if (!r.ok) {
      setError(r.status === 404 ? "Collection not found." : `HTTP ${r.status}`);
      return;
    }
    const body = (await r.json()) as Collection;
    setC(body);
    setName(body.name);
    setDescription(body.description ?? "");
  }, [id]);
  useEffect(() => {
    void load();
  }, [load]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    await fetch(`/api/collections/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() || c?.name, description: description.trim() || null }),
    });
    setEditing(false);
    await load();
  };
  const remove = async (itemId: number) => {
    await fetch(`/api/collections/${id}/items/${itemId}`, { method: "DELETE" });
    await load();
  };
  const destroy = async () => {
    if (!window.confirm(`Delete the collection “${c?.name}”? The papers and answers themselves are kept.`)) return;
    const r = await fetch(`/api/collections/${id}`, { method: "DELETE" });
    if (r.ok) router.push("/collections");
  };

  if (!c) return <p className="text-sm text-muted-foreground">{error ?? "Loading…"}</p>;
  const papers = c.items.filter((i) => i.item_type === "paper");
  const runs = c.items.filter((i) => i.item_type === "run");

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        {editing ? (
          <form onSubmit={save} className="flex flex-col gap-2">
            <input value={name} onChange={(e) => setName(e.target.value)} aria-label="Name"
              className="rounded-md border border-border bg-background px-3 py-1.5 text-lg font-semibold outline-none focus:ring-2 focus:ring-ring" />
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Description (optional)" rows={2}
              className="rounded-md border border-border bg-background px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring" />
            <div className="flex gap-2">
              <button type="submit" className="rounded-md bg-primary px-3 py-1 text-sm text-primary-foreground">Save</button>
              <button type="button" onClick={() => setEditing(false)} className="rounded-md border border-border px-3 py-1 text-sm">
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="flex items-start justify-between gap-3">
              <h1 className="text-xl font-semibold tracking-tight">{c.name}</h1>
              <div className="flex shrink-0 gap-2 text-xs">
                <a href={`/api/collections/${id}/bibtex`} className="rounded-md border border-border px-2 py-1 hover:bg-muted">
                  Export BibTeX
                </a>
                <button onClick={() => setEditing(true)} className="rounded-md border border-border px-2 py-1 hover:bg-muted">
                  Edit
                </button>
                <button onClick={destroy} className="rounded-md border border-border px-2 py-1 text-destructive hover:bg-muted">
                  Delete
                </button>
              </div>
            </div>
            {c.description && <p className="text-sm text-muted-foreground">{c.description}</p>}
            <p className="text-xs text-muted-foreground">
              BibTeX includes the saved papers and every paper cited by the saved answers.
            </p>
          </>
        )}
      </header>

      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Papers ({papers.length})</h2>
        {papers.length === 0 && <p className="text-sm text-muted-foreground">No papers saved.</p>}
        <ul className="flex flex-col gap-3">
          {papers.map((i) => (
            <li key={i.id} className="flex items-start justify-between gap-3 text-sm">
              <span className="min-w-0">
                <Link href={`/papers/${i.arxiv_id}`} className="font-medium hover:underline">
                  {i.title}
                </Link>
                <span className="block text-muted-foreground">
                  {authorLine(i.authors)} ({i.year}) · {i.full_text ? "full text" : "abstract only"}
                </span>
                <span className="mt-0.5 block">
                  <Badges badges={i.badges} />
                </span>
              </span>
              <button onClick={() => remove(i.id)} className="shrink-0 text-xs text-muted-foreground hover:text-foreground">
                Remove
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Answers ({runs.length})</h2>
        {runs.length === 0 && <p className="text-sm text-muted-foreground">No answers saved.</p>}
        <ul className="flex flex-col gap-2">
          {runs.map((i) => (
            <li key={i.id} className="flex items-start justify-between gap-3 text-sm">
              <span className="min-w-0">
                <Link href={`/threads/${i.thread_id}`} className="font-medium hover:underline">
                  {i.question}
                </Link>
                <span className="text-xs text-muted-foreground"> · {i.run_status}</span>
              </span>
              <button onClick={() => remove(i.id)} className="shrink-0 text-xs text-muted-foreground hover:text-foreground">
                Remove
              </button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
