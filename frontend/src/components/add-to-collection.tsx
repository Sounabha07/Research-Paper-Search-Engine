"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import type { CollectionSummary } from "@/lib/api-types";

/** "Save" menu: add a paper or an answer (run) to a collection, or to a new one. */
export function AddToCollection({ arxivId, runId }: { arxivId?: string; runId?: string }) {
  const [open, setOpen] = useState(false);
  const [list, setList] = useState<CollectionSummary[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const query = arxivId ? `arxiv_id=${encodeURIComponent(arxivId)}` : `run_id=${runId}`;

  const load = async () => {
    const r = await fetch(`/api/collections?${query}`, { cache: "no-store" });
    if (r.ok) setList((await r.json()) as CollectionSummary[]);
  };

  useEffect(() => {
    if (!open) return;
    void load();
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as globalThis.Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const add = async (collectionId: string) => {
    setBusy(true);
    await fetch(`/api/collections/${collectionId}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(arxivId ? { item_type: "paper", arxiv_id: arxivId } : { item_type: "run", run_id: runId }),
    });
    await load();
    setBusy(false);
  };

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    const r = await fetch("/api/collections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    if (r.ok) {
      setName("");
      await add(((await r.json()) as { id: string }).id);
    }
    setBusy(false);
  };

  return (
    <div ref={ref} className="relative inline-block">
      <button onClick={() => setOpen((o) => !o)}
        className="rounded-md border border-border px-2 py-1 text-xs hover:bg-muted">
        Save to collection
      </button>
      {open && (
        <div className="absolute right-0 z-20 mt-1 w-64 rounded-md border border-border bg-background p-2 text-sm shadow-lg">
          {list === null ? (
            <p className="px-1 text-xs text-muted-foreground">Loading…</p>
          ) : (
            <ul className="flex max-h-56 flex-col overflow-y-auto">
              {list.map((c) => (
                <li key={c.id}>
                  <button disabled={busy || c.contains} onClick={() => add(c.id)}
                    className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-1 text-left hover:bg-muted disabled:hover:bg-transparent">
                    <span className="truncate">{c.name}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">{c.contains ? "saved ✓" : c.items}</span>
                  </button>
                </li>
              ))}
              {list.length === 0 && <li className="px-1 text-xs text-muted-foreground">No collections yet.</li>}
            </ul>
          )}
          <form onSubmit={create} className="mt-2 flex gap-1 border-t border-border pt-2">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="New collection…"
              className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1 text-xs outline-none focus:ring-2 focus:ring-ring" />
            <button type="submit" disabled={busy || !name.trim()}
              className="rounded bg-primary px-2 py-1 text-xs text-primary-foreground disabled:opacity-50">
              Add
            </button>
          </form>
          <Link href="/collections" className="mt-1 block px-1 text-xs text-muted-foreground underline underline-offset-2">
            All collections
          </Link>
        </div>
      )}
    </div>
  );
}
