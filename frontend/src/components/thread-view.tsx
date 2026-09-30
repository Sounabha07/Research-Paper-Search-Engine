"use client";

import { useCallback, useEffect, useState } from "react";

import { ModeToggle } from "@/components/mode-toggle";
import { RunView } from "@/components/run-view";
import { ACTIVE, type Thread } from "@/lib/api-types";

export function ThreadView({ threadId }: { threadId: string }) {
  const [thread, setThread] = useState<Thread | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [followUp, setFollowUp] = useState("");
  const [mode, setMode] = useState<"quick" | "deep">("quick");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const res = await fetch(`/api/threads/${threadId}`, { cache: "no-store" });
    if (!res.ok) {
      setError(`HTTP ${res.status}`);
      return;
    }
    setThread((await res.json()) as Thread);
  }, [threadId]);

  useEffect(() => {
    load();
  }, [load]);

  const running = thread?.runs.some((r) => ACTIVE.has(r.status)) ?? false;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (followUp.trim().length < 3) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`/api/threads/${threadId}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: followUp.trim(), mode }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
      setFollowUp("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "request failed");
    } finally {
      setBusy(false);
    }
  };

  const pin = async (arxivId: string) => {
    await fetch(`/api/threads/${threadId}/pin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ arxiv_id: arxivId }),
    });
    await load();
  };
  const unpin = async (arxivId: string) => {
    await fetch(`/api/threads/${threadId}/pin/${arxivId}`, { method: "DELETE" });
    await load();
  };

  if (!thread) return <p className="text-sm text-muted-foreground">{error ?? "Loading…"}</p>;
  return (
    <div className="flex flex-col gap-8">
      <div className="flex items-center justify-between gap-3">
        <h1 className="min-w-0 truncate text-sm text-muted-foreground">{thread.title}</h1>
        {Object.keys(thread.citations).length > 0 && (
          <a href={`/api/threads/${threadId}/bibtex`} className="shrink-0 text-xs underline underline-offset-2">
            Export BibTeX
          </a>
        )}
      </div>
      {thread.pinned_papers.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-2 text-xs">
          <span className="font-medium">Pinned (always used):</span>
          {thread.pinned_papers.map((p) => (
            <span key={p} className="flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono">
              <a href={`/papers/${p}`} className="hover:underline">{p}</a>
              <button onClick={() => unpin(p)} aria-label={`Unpin ${p}`} className="text-muted-foreground hover:text-foreground">
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {thread.runs.map((r) => (
        <RunView key={r.id} runId={r.id} onSettled={load} onPin={pin} />
      ))}
      <form onSubmit={submit} className="flex items-center gap-2">
        <ModeToggle mode={mode} onChange={setMode} />
        <input
          value={followUp}
          onChange={(e) => setFollowUp(e.target.value)}
          disabled={running || busy}
          placeholder={running ? "Wait for the current answer…" : "Ask a follow-up…"}
          className="flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring disabled:opacity-60"
        />
        <button type="submit" disabled={running || busy}
          className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-50">
          Ask
        </button>
      </form>
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
