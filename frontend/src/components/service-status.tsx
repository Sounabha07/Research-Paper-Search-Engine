"use client";

import { useEffect, useState } from "react";

type Check = { ok: boolean; detail: string; latency_ms: number };
type Ready = { status: string; checks: Record<string, Check> };

const POLL_MS = 5000;

export function ServiceStatus() {
  const [data, setData] = useState<Ready | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch("/api/health/ready", { cache: "no-store" });
        const body = (await res.json()) as Ready;
        if (!cancelled) {
          setData(body);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("API unreachable");
      }
    };
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (!data) return <p className="text-sm text-muted-foreground">Checking services…</p>;

  return (
    <div className="w-full">
      <p className="mb-3 text-sm">
        Overall:{" "}
        <span className={data.status === "ok" ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"}>
          {data.status}
        </span>
      </p>
      <table className="w-full text-sm">
        <tbody>
          {Object.entries(data.checks).map(([name, c]) => (
            <tr key={name} className="border-t border-border">
              <td className="py-1.5 pr-3 font-medium">{name}</td>
              <td className="py-1.5 pr-3">{c.ok ? "✓" : "✗"}</td>
              <td className="py-1.5 font-mono text-xs text-muted-foreground">{c.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
