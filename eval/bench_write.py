"""WRITE step benchmark: the same prompts at different thinking levels (no cache).

  python /app/eval/bench_write.py RUN_ID [RUN_ID ...] [--levels low,minimal]

Rebuilds each run's WRITE prompt from its stored evidence notes (single-turn runs: no conversation
context) and calls the main model once per thinking level. Reports latency, output tokens and cost.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import uuid

from agent import prompts
from core.config import get_config
from core.db import raw_connection
from core.llm import LLMClient
from core.logging import setup_logging
from core.redis import get_redis


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--levels", default="low,minimal")
    args = parser.parse_args()
    setup_logging("WARNING")
    cfg = get_config()
    llm = LLMClient(cfg, redis=get_redis(), use_cache=False)  # timing: no cache
    conn = await raw_connection()
    prompts_by_run = {}
    try:
        for run_id in args.runs:
            q = await conn.fetchval("SELECT standalone_question FROM research_runs WHERE id = $1", uuid.UUID(run_id))
            notes = await conn.fetch(
                "SELECT claim, CASE WHEN chunk_id IS NOT NULL THEN 'c:' || chunk_id ELSE 'a:' || arxiv_id END AS sid"
                " FROM evidence_notes WHERE run_id = $1 ORDER BY id",
                uuid.UUID(run_id),
            )
            notes_text = "\n".join(f"- [[{n['sid']}]] {n['claim']}" for n in notes)
            prompts_by_run[run_id] = prompts.write_prompt(q, notes_text, "")
    finally:
        await conn.close()

    results: dict[str, list[tuple[int, int, float]]] = {}
    for level in args.levels.split(","):
        rows = []
        for run_id, prompt in prompts_by_run.items():
            r = await llm.generate(prompt, model=cfg.models.llm_main, purpose="bench_write", temperature=0.3,
                                   max_output_tokens=8192, thinking_level=level)  # fmt: skip
            rows.append((r.latency_ms, r.output_tokens, r.cost_usd or 0.0))
            print(f"  {level:<8} run {run_id[:8]}: {r.latency_ms / 1000:5.1f} s  out {r.output_tokens:5}  ${r.cost_usd:.4f}"
                  f"  model {r.model}")  # fmt: skip
            await asyncio.sleep(6)  # free-tier request limits
        results[level] = rows
    print("\nlevel     median time  mean time  mean out tokens  mean cost")
    for level, rows in results.items():
        t = [r[0] / 1000 for r in rows]
        print(f"{level:<8}  {statistics.median(t):9.1f} s  {statistics.mean(t):7.1f} s  {statistics.mean(r[1] for r in rows):15.0f}"
              f"  ${statistics.mean(r[2] for r in rows):.4f}")  # fmt: skip


if __name__ == "__main__":
    asyncio.run(main())
