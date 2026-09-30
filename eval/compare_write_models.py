"""WRITE on gemini-3.5-flash vs gemini-3.5-flash-lite, same evidence notes, same prompt, same verifier.

  python /app/eval/compare_write_models.py RUN_ID [RUN_ID ...] [--out /app/eval/write_models.json]

For each run, rebuilds the WRITE prompt from its stored evidence notes (with source status, as the pipeline does
now), writes with each model, and verifies both drafts with the current VERIFY. Uses the shared response cache
and rate limiter, so a rerun costs nothing. If gemini-3.5-flash is out of its daily quota, the client falls back
to flash-lite; such rows are marked and the run's saved WRITE output (if written by flash) is verified instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from agent import prompts
from agent.run import format_notes, verify_sentences
from agent.text import parse_answer, strip_tags
from agent.verify import SourceText
from core.config import get_config
from core.db import raw_connection
from core.llm import LLMClient
from core.logging import setup_logging
from core.redis import get_redis

from verifier_adversarial import paper_meta  # same directory

WRITE_MAX_TOKENS = 8192


async def load_run(run_id: uuid.UUID) -> dict[str, Any]:
    conn = await raw_connection()
    try:
        q = await conn.fetchval(
            "SELECT coalesce(standalone_question, question) FROM research_runs WHERE id = $1", run_id
        )
        notes = await conn.fetch(
            "SELECT claim, CASE WHEN chunk_id IS NOT NULL THEN 'c:' || chunk_id ELSE 'a:' || arxiv_id END AS sid"
            " FROM evidence_notes WHERE run_id = $1 ORDER BY id",
            run_id,
        )
        raw = await conn.fetchval(
            "SELECT payload->>'raw' FROM run_steps WHERE run_id = $1 AND type = 'write' AND payload ? 'raw'"
            " ORDER BY seq DESC LIMIT 1",
            run_id,
        )
        if raw is None:  # runs from before raw drafts were saved: rebuild the draft from the stored sentences
            sents = await conn.fetch(
                "SELECT section, text, sources FROM answer_sentences WHERE run_id = $1 ORDER BY seq", run_id
            )
            parts: list[str] = []
            section = None
            for s in sents:
                if s["section"] != section:
                    section = s["section"]
                    parts.append(f"\n## {section}\n")
                tags = "".join(f"[[{x['id']}]]" for x in json.loads(s["sources"]))
                parts.append(f"{strip_tags(s['text'])} {tags}")
            raw = "\n".join(parts) if sents else None
        saved_model = await conn.fetchval(
            "SELECT model FROM llm_calls WHERE run_id = $1 AND purpose = 'write' ORDER BY id DESC LIMIT 1", run_id
        )
        ids = {n["sid"] for n in notes}
        chunks = [int(i[2:]) for i in ids if i.startswith("c:")]
        papers = [i[2:] for i in ids if i.startswith("a:")]
        texts = {
            f"c:{r['id']}": (r["title"], r["text"])
            for r in await conn.fetch(
                "SELECT c.id, c.text, p.title FROM chunks c JOIN papers p USING (arxiv_id) WHERE c.id = ANY($1::bigint[])",
                chunks,
            )
        }
        texts |= {
            f"a:{r['arxiv_id']}": (r["title"], f"{r['title']}. {r['abstract']}")
            for r in await conn.fetch("SELECT arxiv_id, title, abstract FROM papers WHERE arxiv_id = ANY($1)", papers)
        }
    finally:
        await conn.close()
    meta = await paper_meta(set(texts))
    sources = {k: SourceText(t, x, *meta.get(k, ("", ""))) for k, (t, x) in texts.items()}
    return {"question": q, "notes": [prompts.Note(claim=n["claim"], source_id=n["sid"]) for n in notes],
            "sources": sources, "saved_raw": raw, "saved_model": saved_model}  # fmt: skip


async def score(llm: LLMClient, raw: str, sources: dict[str, SourceText]) -> dict[str, Any]:
    cfg = get_config()
    drafts = parse_answer(raw, set(sources))
    finals = await verify_sentences(llm, cfg, drafts, sources, None)
    kept = [f for f in finals if f.verdict != "unsupported_removed"]
    text = " ".join(strip_tags(f.text) for f in kept)
    papers = {sources[t].title for f in kept for t in f.tags if t in sources}
    return {
        "sentences": len(finals),
        "kept": len(kept),
        "removed": sum(f.verdict == "unsupported_removed" for f in finals),
        "rewritten": sum(f.verdict == "partial_rewritten" for f in finals),
        "words_kept": len(re.findall(r"\w+", text)),
        "papers_cited": len(papers),
        "reasons": [f.reason for f in finals if f.reason],
        "answer": "\n".join(f"[{f.verdict[:4]}] {strip_tags(f.text)}" for f in finals),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--out", default="/app/eval/write_models.json")
    args = parser.parse_args()
    setup_logging("WARNING")
    cfg = get_config()
    llm = LLMClient(cfg, redis=get_redis())  # response cache + shared rate limiter
    rows = []
    for rid in args.runs:
        run = await load_run(uuid.UUID(rid))
        prompt = prompts.write_prompt(run["question"], format_notes(run["notes"], run["sources"]), "")
        for model in (cfg.models.llm_main, cfg.models.llm_light):
            r = await llm.generate(prompt, model=model, purpose="compare_write", temperature=0.3,
                                   max_output_tokens=WRITE_MAX_TOKENS, thinking_level=cfg.models.write_thinking_level)  # fmt: skip
            row: dict[str, Any] = {"run": rid, "question": run["question"], "model": model, "wrote": r.model,
                                   "latency_s": r.latency_ms / 1000, "out_tokens": r.output_tokens,
                                   "cost_usd": r.cost_usd, "cache_hit": r.cache_hit}  # fmt: skip
            if r.model != model and run["saved_raw"] and run["saved_model"] == model:
                row["note"] = (
                    f"{model} unavailable (daily quota); scored the run's saved {model} answer instead"
                    " (older prompt without source status; rewritten sentences appear as rewritten)"
                )
                row |= await score(llm, run["saved_raw"], run["sources"])
            else:
                row |= await score(llm, r.text, run["sources"])
            rows.append(row)
            print(
                f"{run['question'][:48]:<48} {model:<22} wrote={r.model:<22} {row['latency_s']:5.1f}s "
                f"kept {row['kept']:>2}/{row['sentences']:<2} removed {row['removed']} rewritten {row['rewritten']} "
                f"words {row['words_kept']:>3} papers {row['papers_cited']:>2} {row.get('note', '')}"
            )
    Path(args.out).write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    for model in (cfg.models.llm_main, cfg.models.llm_light):
        rs = [r for r in rows if r["model"] == model]
        n = len(rs) or 1
        print(
            f"{model:<22} mean: removed {sum(r['removed'] for r in rs) / n:.1f}, rewritten "
            f"{sum(r['rewritten'] for r in rs) / n:.1f}, kept sentences {sum(r['kept'] for r in rs) / n:.1f}, "
            f"words {sum(r['words_kept'] for r in rs) / n:.0f}, papers {sum(r['papers_cited'] for r in rs) / n:.1f}, "
            f"time {sum(r['latency_s'] for r in rs) / n:.1f} s"
        )


if __name__ == "__main__":
    asyncio.run(main())
