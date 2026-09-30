"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { Badges } from "@/components/badges";
import { authorLine, type Author } from "@/lib/api-types";

type Paper = {
  arxiv_id: string;
  title: string;
  authors: Author[];
  abstract: string;
  year: number | null;
  primary_category: string | null;
  badges: Record<string, unknown>;
  full_text: boolean;
  state: string;
};

type Content = {
  full_text: boolean;
  text: string;
  headings: { text: string; start: number; end: number }[];
  chunks: { id: number; section: string; start: number; end: number }[];
};

type Block = { start: number; end: number; heading: boolean };

// Server offsets (chunk char_start/char_end, heading positions) count Unicode code points, as Python does.
// JavaScript strings index UTF-16 units, which differ for characters outside the BMP (e.g. math italic 𝑁),
// so all slicing here works on an array of code points.
function blocks(cps: string[], content: Content): Block[] {
  const headingStarts = new Set(content.headings.map((h) => h.start));
  const out: Block[] = [];
  let start = 0;
  for (let i = 0; i <= cps.length; i++) {
    const atSep = i + 1 < cps.length && cps[i] === "\n" && cps[i + 1] === "\n";
    if (atSep || i === cps.length) {
      out.push({ start, end: i, heading: headingStarts.has(start) });
      if (atSep) {
        start = i + 2;
        i += 1;
      }
    }
  }
  return out;
}

function Highlighted({ cps, start, end, hlStart, hlEnd, markRef }: {
  cps: string[]; start: number; end: number; hlStart: number | null; hlEnd: number | null;
  markRef: React.RefObject<HTMLElement | null>;
}) {
  if (hlStart === null || hlEnd === null || hlEnd <= start || hlStart >= end) {
    return <>{cps.slice(start, end).join("")}</>;
  }
  const a = Math.max(hlStart, start);
  const b = Math.min(hlEnd, end);
  const isFirst = hlStart >= start;
  return (
    <>
      {cps.slice(start, a).join("")}
      <mark ref={isFirst ? (markRef as React.RefObject<HTMLElement>) : undefined}
        className="rounded bg-yellow-200/70 px-0.5 text-inherit dark:bg-yellow-500/30">
        {cps.slice(a, b).join("")}
      </mark>
      {cps.slice(b, end).join("")}
    </>
  );
}

export function PaperReader({ arxivId }: { arxivId: string }) {
  const params = useSearchParams();
  const chunkParam = params.get("chunk");
  const [paper, setPaper] = useState<Paper | null>(null);
  const [content, setContent] = useState<Content | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fullTextStatus, setFullTextStatus] = useState<string | null>(null);
  const markRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    Promise.all([
      fetch(`/api/papers/${arxivId}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))),
      fetch(`/api/papers/${arxivId}/content`).then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([p, c]) => {
        setPaper(p as Paper);
        setContent(c as Content | null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed to load"));
  }, [arxivId]);

  useEffect(() => {
    markRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [content, chunkParam]);

  if (!paper) return <p className="text-sm text-muted-foreground">{error ?? "Loading…"}</p>;

  const cps = content ? Array.from(content.text) : [];
  const chunk = content?.chunks.find((c) => String(c.id) === chunkParam);
  const hl = chunk ? { s: chunk.start, e: chunk.end } : null;
  const abstractHl = chunkParam === "abstract";

  const requestFullText = async () => {
    setFullTextStatus("requesting…");
    const res = await fetch(`/api/papers/${arxivId}/fulltext`, { method: "POST" });
    const body = (await res.json()) as { status?: string; detail?: string };
    setFullTextStatus(body.status ?? body.detail ?? `HTTP ${res.status}`);
  };

  return (
    <article className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold leading-snug">{paper.title}</h1>
        <p className="text-sm text-muted-foreground">
          {authorLine(paper.authors)} · {paper.year} · {paper.primary_category} ·{" "}
          <a className="underline underline-offset-2" href={`https://arxiv.org/abs/${paper.arxiv_id}`} target="_blank" rel="noreferrer">
            arXiv {paper.arxiv_id}
          </a>{" "}
          ·{" "}
          <a className="underline underline-offset-2" href={`https://arxiv.org/pdf/${paper.arxiv_id}`} target="_blank" rel="noreferrer">
            PDF
          </a>
        </p>
        <Badges badges={paper.badges} />
      </header>

      {!paper.full_text && (
        <div className="flex items-center gap-3 text-sm">
          <span className="text-muted-foreground">Abstract only.</span>
          {fullTextStatus ? (
            <span className="text-muted-foreground">Full text: {fullTextStatus}</span>
          ) : (
            <button onClick={requestFullText} className="rounded-md border border-border px-2 py-1 text-xs hover:bg-muted">
              Get full text
            </button>
          )}
        </div>
      )}

      <section className="rounded-lg border border-border p-4 text-sm leading-relaxed">
        <h2 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Abstract</h2>
        {abstractHl ? (
          <mark ref={markRef as React.RefObject<HTMLElement>} className="rounded bg-yellow-200/70 px-0.5 text-inherit dark:bg-yellow-500/30">
            {paper.abstract}
          </mark>
        ) : (
          paper.abstract
        )}
      </section>

      {content?.full_text && (
        <section className="flex flex-col gap-3 text-[15px] leading-relaxed">
          {blocks(cps, content).map((b) =>
            b.heading ? (
              <h3 key={b.start} className="mt-3 font-semibold">
                {cps.slice(b.start, b.end).join("")}
              </h3>
            ) : (
              <p key={b.start}>
                <Highlighted cps={cps} start={b.start} end={b.end} hlStart={hl?.s ?? null} hlEnd={hl?.e ?? null} markRef={markRef} />
              </p>
            ),
          )}
        </section>
      )}
    </article>
  );
}
