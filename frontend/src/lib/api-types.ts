export type Author = { first: string; last: string };

export type Source = {
  arxiv_id: string;
  chunk_id: number | null;
  type: "chunk" | "abstract";
  section: string;
  text: string;
  char_start: number | null;
  char_end: number | null;
};

export type RunPaper = {
  number: number | null;
  title: string;
  year: number | null;
  authors: Author[];
  badges: Record<string, unknown>;
  full_text: boolean;
  primary_category: string | null;
  sections_used: string[];
  via_chain: string | null;
};

export type Sentence = {
  seq: number;
  section: string;
  text: string;
  verdict: "supported" | "partial_rewritten" | "unsupported_removed";
  sources: string[];
};

export type Step = { seq: number; type: string; payload: Record<string, unknown>; at: string };

export type SelectedPaper = {
  arxiv_id: string;
  title: string;
  year: number | null;
  rerank_score: number;
  full_text: boolean;
  via_anchor?: boolean;
  carryover?: boolean;
  badges: Record<string, unknown>;
};

export type Run = {
  id: string;
  thread_id: string;
  turn: number;
  question: string;
  standalone_question: string | null;
  mode: string;
  status: "queued" | "running" | "done" | "timed_out" | "failed" | "cancelled";
  selected_papers: SelectedPaper[];
  steps: Step[];
  sentences: Sentence[];
  removed_count: number;
  sources: Record<string, Source>;
  papers: Record<string, RunPaper>;
  consensus: { claim: string; items: { arxiv_id: string; chunk_id: number | null; stance: string }[] }[];
  cost_usd: number;
  duration_s: number | null;
};

export type Thread = {
  id: string;
  title: string;
  summary: string | null;
  pinned_papers: string[];
  runs: { id: string; turn: number; question: string; status: string }[];
  citations: Record<string, number>;
};

export const ACTIVE = new Set(["queued", "running"]);

export function authorLine(authors: Author[]): string {
  const names = authors.map((a) => [a.first, a.last].filter(Boolean).join(" "));
  return names.length > 3 ? `${names.slice(0, 3).join(", ")} et al.` : names.join(", ");
}

export const TAG_RE = /\s*\[\[\s*[ca]\s*:\s*[^\]\s]+\s*\]\]/g;

export function stripTags(text: string): string {
  return text.replace(TAG_RE, "").trim();
}

export function badgeList(badges: Record<string, unknown>): string[] {
  const out: string[] = [];
  if (badges.landmark) out.push("landmark");
  if (typeof badges.venue === "string" && badges.venue) out.push(badges.venue);
  if (badges.code_available) out.push("code");
  if (badges.is_survey) out.push("survey");
  if (badges.is_benchmark) out.push("benchmark");
  const cites = Number(badges.fulltext_citations ?? 0);
  if (cites > 0) out.push(`cited by ${cites} full-text papers in this corpus`);
  return out;
}
