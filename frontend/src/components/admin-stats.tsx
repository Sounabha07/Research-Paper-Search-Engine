"use client";

import { useEffect, useState } from "react";

type Failure = {
  arxiv_id: string;
  title: string;
  state: string;
  fail_count: number;
  last_error: string | null;
  updated_at: string;
};

type Stats = {
  papers: {
    total: number;
    by_state: Record<string, number>;
    landmarks: number;
    with_abstract_embedding: number;
    venue_accepted: number;
    code_available: number;
    with_citation_data: number;
  };
  fetch_queues: Record<string, number>;
  throughput: {
    ready_last_hour: number;
    ready_last_day: number;
    llm_calls_last_day: number;
    llm_cost_usd_last_day: number;
    llm_cost_usd_total: number;
  };
  recent_failures: Failure[];
  disk_bytes: Record<string, number>;
  generated_at: string;
};

// Pipeline order, so the table reads top to bottom as papers progress.
const STATE_ORDER = [
  "discovered",
  "metadata_done",
  "signals_done",
  "queued_fulltext",
  "pdf_fetched",
  "parsed",
  "chunked",
  "embedded",
  "ready",
  "failed",
  "abstract_only",
];
const POLL_MS = 30000;
const nf = new Intl.NumberFormat("en-US");

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

function pct(part: number, whole: number): string {
  return whole ? `${((100 * part) / whole).toFixed(1)}%` : "–";
}

function Tile({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-border p-4">
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h2>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right tabular-nums">{value}</dd>
    </>
  );
}

export function AdminStats() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch("/api/admin/stats", { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as Stats;
        if (!cancelled) {
          setStats(body);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "request failed");
      }
    };
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (!stats) {
    return <p className="text-sm text-muted-foreground">{error ? `Stats unavailable: ${error}` : "Loading…"}</p>;
  }

  const p = stats.papers;
  const t = stats.throughput;
  const states = [
    ...STATE_ORDER.filter((s) => s in p.by_state),
    ...Object.keys(p.by_state).filter((s) => !STATE_ORDER.includes(s)),
  ];
  const maxState = Math.max(1, ...Object.values(p.by_state));
  const diskTotal = Object.values(stats.disk_bytes).reduce((a, b) => a + b, 0);

  return (
    <div className="flex flex-col gap-4">
      {error && <p className="text-xs text-destructive">Refresh failed ({error}); showing last data.</p>}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="Tier 1 papers" value={nf.format(p.total)} />
        <Tile
          label="Abstract embeddings"
          value={nf.format(p.with_abstract_embedding)}
          sub={pct(p.with_abstract_embedding, p.total)}
        />
        <Tile label="Full text ready" value={nf.format(p.by_state.ready ?? 0)} />
        <Tile label="Landmarks" value={nf.format(p.landmarks)} />
        <Tile label="Venue accepted" value={nf.format(p.venue_accepted)} sub={pct(p.venue_accepted, p.total)} />
        <Tile label="Code available" value={nf.format(p.code_available)} sub={pct(p.code_available, p.total)} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Papers by state">
          {states.length === 0 ? (
            <p className="text-sm text-muted-foreground">No papers yet. Run make ingest-metadata.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {states.map((s) => (
                  <tr key={s} className="border-t border-border first:border-t-0">
                    <td className="w-36 py-1.5 pr-3 font-mono text-xs">{s}</td>
                    <td className="py-1.5 pr-3">
                      <div
                        className="h-2 rounded-r bg-primary/70"
                        style={{ width: `${Math.max(1, (100 * p.by_state[s]) / maxState)}%` }}
                      />
                    </td>
                    <td className="w-24 py-1.5 text-right tabular-nums">{nf.format(p.by_state[s])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>

        <div className="flex flex-col gap-4">
          <Section title="Fetch queues & throughput">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
              <Row label="fetch:urgent" value={nf.format(stats.fetch_queues.urgent ?? 0)} />
              <Row label="fetch:bulk" value={nf.format(stats.fetch_queues.bulk ?? 0)} />
              <Row label="ready, last hour" value={nf.format(t.ready_last_hour)} />
              <Row label="ready, last 24h" value={nf.format(t.ready_last_day)} />
              <Row label="LLM calls, last 24h" value={nf.format(t.llm_calls_last_day)} />
              <Row
                label="LLM cost, 24h / total"
                value={`$${t.llm_cost_usd_last_day.toFixed(4)} / $${t.llm_cost_usd_total.toFixed(4)}`}
              />
            </dl>
          </Section>

          <Section title={`Disk usage · ${formatBytes(diskTotal)}`}>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
              {Object.entries(stats.disk_bytes)
                .sort((a, b) => b[1] - a[1])
                .map(([name, bytes]) => (
                  <Row key={name} label={`${name}/`} value={formatBytes(bytes)} />
                ))}
            </dl>
          </Section>
        </div>
      </div>

      <Section title="Recent failures">
        {stats.recent_failures.length === 0 ? (
          <p className="text-sm text-muted-foreground">None.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                <th className="py-1 pr-3 font-medium">Paper</th>
                <th className="py-1 pr-3 font-medium">State</th>
                <th className="py-1 pr-3 font-medium">Fails</th>
                <th className="py-1 font-medium">Last error</th>
              </tr>
            </thead>
            <tbody>
              {stats.recent_failures.map((f) => (
                <tr key={f.arxiv_id} className="border-t border-border align-top">
                  <td className="py-1.5 pr-3">
                    <div className="font-mono text-xs">{f.arxiv_id}</div>
                    <div className="line-clamp-1 text-xs text-muted-foreground">{f.title}</div>
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-xs">{f.state}</td>
                  <td className="py-1.5 pr-3 tabular-nums">{f.fail_count}</td>
                  <td className="py-1.5 font-mono text-xs text-muted-foreground">{f.last_error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <p className="text-xs text-muted-foreground">
        Updated {new Date(stats.generated_at).toLocaleTimeString()} · refreshes every {POLL_MS / 1000}s
      </p>
    </div>
  );
}
