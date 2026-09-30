"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Badges } from "@/components/badges";
import { ACTIVE, authorLine, type Run, type Sentence, type Source, stripTags } from "@/lib/api-types";

const POLL_MS = 1000;

const STEP_LABEL: Record<string, string> = {
  rewrite: "Rewrote the question",
  find_papers: "Found papers",
  fetch: "Fetched full text",
  chain: "Followed references",
  passages: "Selected passages",
  read: "Read passages",
  write: "Wrote the answer",
  verify: "Verified sentences",
  consensus: "Checked consensus",
};

/** Loads a run and follows it live: server-sent events (/runs/{id}/events) trigger reloads and carry fetch
 * progress; if the stream fails, it falls back to polling. */
export function useRun(runId: string, onSettled?: () => void) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let source: EventSource | null = null;
    let settled = false;
    const load = async (): Promise<Run | null> => {
      try {
        const res = await fetch(`/api/runs/${runId}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as Run;
        if (stop) return null;
        setRun(body);
        setError(null);
        if (!ACTIVE.has(body.status) && !settled) {
          settled = true;
          source?.close();
          setProgress(null);
          onSettled?.();
        }
        return body;
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e.message : "failed to load run");
        return null;
      }
    };
    const poll = async () => {
      const body = await load();
      if (!stop && (!body || ACTIVE.has(body.status))) timer = setTimeout(poll, POLL_MS);
    };
    load().then((body) => {
      if (stop || !body || !ACTIVE.has(body.status)) return;
      source = new EventSource(`/api/runs/${runId}/events`);
      source.onmessage = (ev) => {
        const data = JSON.parse(ev.data) as { progress?: string; status?: string; type?: string };
        if (data.progress) setProgress(data.progress);
        else load();
      };
      source.onerror = () => {
        source?.close();
        if (!stop && !settled) poll();
      };
    });
    return () => {
      stop = true;
      source?.close();
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);
  return { run, error, progress };
}

function Chip({ number, sources, run }: { number: number; sources: Source[]; run: Run }) {
  const [open, setOpen] = useState(false);
  const first = sources[0];
  const href = first
    ? `/papers/${first.arxiv_id}?chunk=${first.chunk_id ?? "abstract"}`
    : "#";
  const paper = first ? run.papers[first.arxiv_id] : undefined;
  return (
    <span className="relative" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <Link
        href={href}
        className="mx-0.5 rounded bg-muted px-1 py-px align-baseline text-[11px] font-medium text-foreground tabular-nums hover:bg-primary hover:text-primary-foreground"
      >
        [{number}]
      </Link>
      {open && first && (
        <span className="absolute left-0 top-5 z-20 block w-96 max-w-[80vw] rounded-md border border-border bg-popover p-3 text-left text-xs text-popover-foreground shadow-lg">
          <span className="mb-1 block font-medium">
            [{number}] {paper?.title}
          </span>
          {sources.map((s, i) => (
            <span key={i} className="mt-2 block">
              <span className="block text-[10px] uppercase tracking-wide text-muted-foreground">{s.section}</span>
              <span className="line-clamp-8 block whitespace-pre-line">{s.text}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  );
}

function SentenceView({ s, run }: { s: Sentence; run: Run }) {
  const byPaper = new Map<string, Source[]>();
  for (const id of s.sources) {
    const src = run.sources[id];
    if (src) byPaper.set(src.arxiv_id, [...(byPaper.get(src.arxiv_id) ?? []), src]);
  }
  const chips = [...byPaper.entries()]
    .map(([arxivId, srcs]) => ({ n: run.papers[arxivId]?.number ?? 0, srcs }))
    .sort((a, b) => a.n - b.n);
  return (
    <span>
      {stripTags(s.text)}
      {chips.map((c) => (
        <Chip key={c.n} number={c.n} sources={c.srcs} run={run} />
      ))}{" "}
    </span>
  );
}

function Answer({ run, onPin }: { run: Run; onPin?: (arxivId: string) => void }) {
  const kept = run.sentences.filter((s) => s.verdict !== "unsupported_removed");
  const removed = run.sentences.filter((s) => s.verdict === "unsupported_removed");
  const sections: { title: string; sentences: Sentence[] }[] = [];
  for (const s of kept) {
    const last = sections[sections.length - 1];
    if (last && last.title === s.section) last.sentences.push(s);
    else sections.push({ title: s.section, sentences: [s] });
  }
  const cited = Object.entries(run.papers)
    .filter(([, p]) => p.number !== null)
    .sort((a, b) => (a[1].number ?? 0) - (b[1].number ?? 0));

  if (!kept.length) {
    const lastWrite = [...run.steps].reverse().find((s) => s.type === "write");
    const err = lastWrite?.payload?.error as string | undefined;
    return <p className="text-sm text-muted-foreground">{err ?? "No supported answer."}</p>;
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 text-[15px] leading-relaxed">
        {sections.map((sec, i) => (
          <div key={i}>
            {sec.title && <h3 className="mb-1 font-semibold">{sec.title}</h3>}
            <p>
              {sec.sentences.map((s) => (
                <SentenceView key={s.seq} s={s} run={run} />
              ))}
            </p>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground tabular-nums">
        {run.removed_count} {run.removed_count === 1 ? "claim" : "claims"} removed · ${Number(run.cost_usd).toFixed(4)}
        {run.duration_s !== null && ` · ${run.duration_s.toFixed(1)} s`}
        {run.status === "timed_out" && " · timed out (answer written from the evidence gathered so far)"}
      </p>
      {removed.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">Show removed claims</summary>
          <ul className="mt-1 list-disc pl-5">
            {removed.map((s) => (
              <li key={s.seq}>{stripTags(s.text)}</li>
            ))}
          </ul>
        </details>
      )}
      <section>
        <h4 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Sources</h4>
        <ol className="flex flex-col gap-1.5 text-sm">
          {cited.map(([arxivId, p]) => (
            <li key={arxivId} className="flex gap-2">
              <span className="w-7 shrink-0 text-right text-muted-foreground tabular-nums">[{p.number}]</span>
              <span className="min-w-0">
                <span className="text-muted-foreground">{authorLine(p.authors)} ({p.year}). </span>
                <Link href={`/papers/${arxivId}`} className="hover:underline">
                  {p.title}
                </Link>
                {p.sections_used.length > 0 && (
                  <span className="text-xs text-muted-foreground"> · sections: {p.sections_used.join(", ")}</span>
                )}
                {p.via_chain && (
                  <span className="text-xs text-muted-foreground">
                    {" "}· found via references of{" "}
                    {run.papers[p.via_chain]?.number ? `[${run.papers[p.via_chain].number}]` : p.via_chain}
                  </span>
                )}
                {onPin && (
                  <button onClick={() => onPin(arxivId)} className="ml-2 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground">
                    pin
                  </button>
                )}
                <span className="mt-0.5 block">
                  <Badges badges={p.badges} />
                </span>
              </span>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function Trace({ run, progress }: { run: Run; progress: string | null }) {
  return (
    <ol className="flex flex-col gap-2 text-xs">
      {run.steps.map((s) => {
        const p = s.payload as Record<string, unknown>;
        return (
          <li key={s.seq} className="rounded border border-border p-2">
            <div className="font-medium">{STEP_LABEL[s.type] ?? s.type}</div>
            {s.type === "rewrite" && (
              <ul className="mt-1 list-disc pl-4 text-muted-foreground">
                {(p.queries as string[]).map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            )}
            {s.type === "find_papers" && (
              <ol className="mt-1 flex flex-col gap-1 text-muted-foreground">
                {run.selected_papers.map((sp, i) => (
                  <li key={sp.arxiv_id} className="flex gap-1">
                    <span className="w-4 shrink-0 text-right tabular-nums">{i + 1}</span>
                    <span className="min-w-0">
                      <span className="text-foreground">{sp.title}</span> ({sp.year}) ·{" "}
                      {sp.full_text ? "full text" : "abstract only"} · {sp.rerank_score.toFixed(2)}
                      {sp.via_anchor && " · anchor"}
                      {sp.pinned && " · pinned"}
                      {sp.via_chain && " · via references"}
                      {sp.carryover && " · from earlier in thread"}
                    </span>
                  </li>
                ))}
              </ol>
            )}
            {s.type === "fetch" && (
              <div className="text-muted-foreground">
                {Number(p.requested) === 0
                  ? "all selected papers already had full text"
                  : `${p.ready} of ${p.requested} now full text in ${p.waited_s} s` +
                    ((p.still_missing as string[]).length ? ` · ${(p.still_missing as string[]).length} still pending (abstract used)` : "")}
              </div>
            )}
            {s.type === "chain" && (
              <div className="text-muted-foreground">
                {(p.added as { title: string }[]).length === 0
                  ? `no reference scored high enough (${p.candidates} checked)`
                  : (p.added as { title: string; rerank_score: number }[]).map((a) => (
                      <div key={a.title}>+ {a.title} ({a.rerank_score.toFixed(2)})</div>
                    ))}
              </div>
            )}
            {s.type === "passages" && (
              <div className="text-muted-foreground">{(p.passages as unknown[]).length} passages</div>
            )}
            {s.type === "read" && <div className="text-muted-foreground">{String(p.notes)} evidence notes</div>}
            {s.type === "write" && (
              <div className="text-muted-foreground">
                {p.error ? String(p.error) : `${p.sentences} sentences (${p.untagged_or_invalid} untagged or invalid)`}
              </div>
            )}
            {s.type === "verify" && (
              <div className="text-muted-foreground">
                {String(p.supported)} supported · {String(p.partial_rewritten)} rewritten · {String(p.unsupported_removed)} removed
              </div>
            )}
            {s.type === "consensus" && <div className="text-muted-foreground">{String(p.claims)} central claims</div>}
          </li>
        );
      })}
      {ACTIVE.has(run.status) && (
        <li className="animate-pulse px-2 text-muted-foreground">{progress ?? "working…"}</li>
      )}
    </ol>
  );
}

export function RunView({ runId, onSettled, onPin }: {
  runId: string; onSettled?: () => void; onPin?: (arxivId: string) => void;
}) {
  const { run, error, progress } = useRun(runId, onSettled);
  const [cancelling, setCancelling] = useState(false);
  if (!run) return <p className="text-sm text-muted-foreground">{error ?? "Loading…"}</p>;
  const cancel = async () => {
    setCancelling(true);
    await fetch(`/api/runs/${runId}/cancel`, { method: "POST" });
  };
  return (
    <article className="flex flex-col gap-3 border-b border-border pb-8">
      <div className="flex items-start justify-between gap-3">
        <h2 className="text-lg font-semibold">
          {run.question}
          {run.mode === "deep" && <span className="ml-2 rounded bg-muted px-1.5 py-px align-middle text-[11px] font-normal">deep</span>}
        </h2>
        {ACTIVE.has(run.status) && (
          <button onClick={cancel} disabled={cancelling}
            className="shrink-0 rounded-md border border-border px-2 py-1 text-xs hover:bg-muted disabled:opacity-50">
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        )}
      </div>
      {run.standalone_question && run.standalone_question !== run.question && (
        <p className="-mt-2 text-xs text-muted-foreground">Understood as: {run.standalone_question}</p>
      )}
      <div className="grid gap-6 lg:grid-cols-[18rem_1fr]">
        <aside>
          <Trace run={run} progress={progress} />
        </aside>
        <div>
          {ACTIVE.has(run.status) ? (
            <p className="text-sm text-muted-foreground">Working on it…</p>
          ) : run.status === "failed" ? (
            <p className="text-sm text-destructive">This run failed. See the trace for the error.</p>
          ) : run.status === "cancelled" ? (
            <p className="text-sm text-muted-foreground">Cancelled.</p>
          ) : (
            <Answer run={run} onPin={onPin} />
          )}
        </div>
      </div>
    </article>
  );
}
