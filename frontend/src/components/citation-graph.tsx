"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import type { GraphData, GraphNode } from "@/lib/api-types";

// A small force-directed layout on a canvas (repulsion, springs along edges, gravity to the center).
// The spec names react-force-graph-2d; it could not be installed (npm registry failures), and for graphs of
// at most a few hundred nodes this is enough. Positions persist across expansions.

type SimNode = GraphNode & { x: number; y: number; vx: number; vy: number };
type Edge = { source: string; target: string };

const HEIGHT = 440;
const TICKS = 300;

function useDark(): boolean {
  const [dark, setDark] = useState(false);
  useEffect(() => {
    const el = document.documentElement;
    const update = () => setDark(el.classList.contains("dark"));
    update();
    const obs = new MutationObserver(update);
    obs.observe(el, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);
  return dark;
}

/** Older papers blue, newer orange; stubs (outside the corpus) gray. */
function yearColor(year: number | null, lo: number, hi: number, stub: boolean): string {
  if (stub || year === null) return "hsl(0 0% 60%)";
  const t = hi > lo ? (year - lo) / (hi - lo) : 1;
  return `hsl(${Math.round(220 - t * 190)} 75% 52%)`;
}

function radius(n: GraphNode): number {
  return 3 + Math.sqrt(n.citations) * 2.2;
}

/** One step of the layout: pairwise repulsion, edge springs, gravity, damping. */
function tick(nodes: SimNode[], edges: Edge[], byId: Map<string, SimNode>, alpha: number): void {
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 0.01) {
        dx = Math.random() - 0.5;
        dy = Math.random() - 0.5;
        d2 = 0.25;
      }
      const f = (900 * alpha) / d2;
      a.vx += dx * f;
      a.vy += dy * f;
      b.vx -= dx * f;
      b.vy -= dy * f;
    }
  }
  for (const e of edges) {
    const a = byId.get(e.source);
    const b = byId.get(e.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = ((d - 60) / d) * 0.05 * alpha;
    a.vx += dx * f;
    a.vy += dy * f;
    b.vx -= dx * f;
    b.vy -= dy * f;
  }
  for (const n of nodes) {
    n.vx -= n.x * 0.01 * alpha;
    n.vy -= n.y * 0.01 * alpha;
    n.vx *= 0.6;
    n.vy *= 0.6;
    n.x += n.vx;
    n.y += n.vy;
  }
}

/**
 * Citation graph (spec 15): node size = citations counted within this corpus, color = year, click a node
 * to expand its references and cited-by. `url` returns the initial graph (a paper's or a run's).
 */
export function CitationGraph({ url, title }: { url: string; title?: string }) {
  const dark = useDark();
  const box = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(600);
  const nodes = useRef(new Map<string, SimNode>());
  const edges = useRef<Edge[]>([]);
  const alpha = useRef(1);
  const view = useRef({ scale: 1 });
  const [version, setVersion] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<SimNode | null>(null);
  const [hover, setHover] = useState<{ n: SimNode; x: number; y: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);

  const merge = useCallback((g: GraphData, around?: SimNode) => {
    for (const n of g.nodes) {
      const old = nodes.current.get(n.id);
      if (old) Object.assign(old, { ...n, seed: old.seed || n.seed });
      else {
        const cx = around?.x ?? 0;
        const cy = around?.y ?? 0;
        nodes.current.set(n.id, { ...n, x: cx + (Math.random() - 0.5) * 80, y: cy + (Math.random() - 0.5) * 80, vx: 0, vy: 0 });
      }
    }
    const seen = new Set(edges.current.map((e) => `${e.source}>${e.target}`));
    edges.current = [...edges.current, ...g.edges.filter((e) => !seen.has(`${e.source}>${e.target}`))];
    setTruncated((t) => t || g.truncated);
    alpha.current = 1;
    setVersion((v) => v + 1);
  }, []);

  useEffect(() => {
    nodes.current = new Map();
    edges.current = [];
    setExpanded(new Set());
    setSelected(null);
    setError(null);
    fetch(url)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((g: GraphData) => {
        merge(g);
        if (url.includes("/papers/")) setExpanded(new Set(g.nodes.filter((n) => n.seed).map((n) => n.id)));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "failed to load graph"));
  }, [url, merge]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const obs = new ResizeObserver(([entry]) => setWidth(Math.max(280, Math.floor(entry.contentRect.width))));
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const all = [...nodes.current.values()];
  const years = all.filter((n) => !n.stub && n.year !== null).map((n) => n.year as number);
  const lo = years.length ? Math.min(...years) : 2000;
  const hi = years.length ? Math.max(...years) : 2026;

  // layout + drawing loop; stops once the layout has cooled
  useEffect(() => {
    const c = canvas.current;
    if (!c || nodes.current.size === 0) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = width * dpr;
    c.height = HEIGHT * dpr;
    let frame = 0;
    let raf = 0;
    const list = [...nodes.current.values()];
    const draw = () => {
      if (alpha.current > 0.01) {
        tick(list, edges.current, nodes.current, alpha.current);
        alpha.current *= 1 - 1 / TICKS * 3;
      }
      // fit the layout into the canvas
      let maxX = 1;
      let maxY = 1;
      for (const n of list) {
        maxX = Math.max(maxX, Math.abs(n.x));
        maxY = Math.max(maxY, Math.abs(n.y));
      }
      const scale = Math.min(1.6, (width / 2 - 24) / maxX, (HEIGHT / 2 - 24) / maxY);
      view.current.scale = scale;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, HEIGHT);
      ctx.translate(width / 2, HEIGHT / 2);
      ctx.scale(scale, scale);
      ctx.strokeStyle = dark ? "rgba(255,255,255,0.16)" : "rgba(0,0,0,0.16)";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.lineWidth = 1 / scale;
      for (const e of edges.current) {
        const a = nodes.current.get(e.source);
        const b = nodes.current.get(e.target);
        if (!a || !b) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const ex = b.x - (dx / d) * (radius(b) / scale);
        const ey = b.y - (dy / d) * (radius(b) / scale);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(ex, ey);
        ctx.stroke();
        const s = 4 / scale;
        ctx.beginPath();
        ctx.moveTo(ex, ey);
        ctx.lineTo(ex - (dx / d) * s - (dy / d) * s * 0.5, ey - (dy / d) * s + (dx / d) * s * 0.5);
        ctx.lineTo(ex - (dx / d) * s + (dy / d) * s * 0.5, ey - (dy / d) * s - (dx / d) * s * 0.5);
        ctx.fill();
      }
      for (const n of list) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, radius(n) / scale, 0, 2 * Math.PI); // constant size on screen
        ctx.fillStyle = yearColor(n.year, lo, hi, n.stub);
        ctx.fill();
        if (n.seed || selected?.id === n.id) {
          ctx.beginPath();
          ctx.arc(n.x, n.y, (radius(n) + 2.5) / scale, 0, 2 * Math.PI);
          ctx.strokeStyle = selected?.id === n.id ? "#eab308" : dark ? "rgba(255,255,255,0.9)" : "rgba(0,0,0,0.8)";
          ctx.lineWidth = 1.5 / scale;
          ctx.stroke();
        }
      }
      frame++;
      if (alpha.current > 0.01 || frame < 2) raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [version, width, dark, selected, lo, hi]);

  const hit = (ev: React.MouseEvent<HTMLCanvasElement>): SimNode | null => {
    const rect = ev.currentTarget.getBoundingClientRect();
    const s = view.current.scale;
    const x = (ev.clientX - rect.left - width / 2) / s;
    const y = (ev.clientY - rect.top - HEIGHT / 2) / s;
    let best: SimNode | null = null;
    let bestD = Infinity;
    for (const n of nodes.current.values()) {
      const d = Math.hypot(n.x - x, n.y - y);
      if (d <= (radius(n) + 4) / s && d < bestD) {
        best = n;
        bestD = d;
      }
    }
    return best;
  };

  const expand = async (n: SimNode) => {
    if (n.stub || expanded.has(n.id)) return;
    setLoading(true);
    try {
      const r = await fetch(`/api/papers/${n.id}/graph?hops=1`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const g = (await r.json()) as GraphData;
      merge({ ...g, nodes: g.nodes.map((x) => ({ ...x, seed: false })) }, n);
      setExpanded((s) => new Set(s).add(n.id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to expand");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      {title && <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>}
      <div ref={box} className="relative overflow-hidden rounded-lg border border-border">
        {error && <p className="p-3 text-sm text-destructive">{error}</p>}
        {!error && all.length === 0 && <p className="p-3 text-sm text-muted-foreground">Loading graph…</p>}
        <canvas
          ref={canvas}
          role="img"
          aria-label={`Citation graph with ${all.length} papers`}
          style={{ width, height: all.length ? HEIGHT : 0, display: "block", cursor: hover ? "pointer" : "default" }}
          onMouseMove={(ev) => {
            const n = hit(ev);
            const rect = ev.currentTarget.getBoundingClientRect();
            setHover(n ? { n, x: ev.clientX - rect.left, y: ev.clientY - rect.top } : null);
          }}
          onMouseLeave={() => setHover(null)}
          onClick={(ev) => {
            const n = hit(ev);
            if (!n) return;
            setSelected(n);
            void expand(n);
          }}
        />
        {hover && (
          <div className="pointer-events-none absolute z-10 max-w-xs rounded border border-border bg-background px-2 py-1 text-xs shadow"
            style={{ left: Math.min(hover.x + 12, width - 250), top: Math.min(hover.y + 12, HEIGHT - 60) }}>
            <div className="font-medium">{hover.n.title}</div>
            <div className="text-muted-foreground">
              {hover.n.year ?? "?"} · {hover.n.citations} in-corpus citations
              {hover.n.stub ? " · outside the corpus" : hover.n.full_text ? " · full text" : " · abstract only"}
            </div>
          </div>
        )}
        {loading && <span className="absolute right-2 top-2 text-xs text-muted-foreground">expanding…</span>}
      </div>

      {selected && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border px-3 py-2 text-sm">
          <span className="min-w-0 flex-1">
            <span className="font-medium">{selected.title}</span>{" "}
            <span className="text-muted-foreground">
              ({selected.year ?? "?"}) · {selected.citations} in-corpus citations ·{" "}
              {selected.stub ? "outside the corpus" : selected.full_text ? "full text" : "abstract only"}
            </span>
          </span>
          {!selected.stub && (
            <Link href={`/papers/${selected.id}`} className="text-xs underline underline-offset-2">
              Open paper
            </Link>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span>{lo}</span>
          <span className="inline-block h-2 w-20 rounded"
            style={{ background: `linear-gradient(to right, ${yearColor(lo, lo, hi, false)}, ${yearColor(hi, lo, hi, false)})` }} />
          <span>{hi}</span>
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: yearColor(null, lo, hi, true) }} />
          outside the corpus
        </span>
        <span>ring = {url.includes("/runs/") ? "papers used for this answer" : "this paper"}</span>
        <span>node size = citations counted within this corpus (by its full-text papers only)</span>
        <span>click a node to expand its references and citing papers</span>
        {truncated && <span>graph truncated at 200 nodes</span>}
      </div>
    </div>
  );
}
