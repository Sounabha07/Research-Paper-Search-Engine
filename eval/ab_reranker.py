"""A/B two cross-encoder rerankers on the same questions (full section-11 search, only the reranker differs).

  python /app/eval/ab_reranker.py [--models BAAI/bge-reranker-base,BAAI/bge-reranker-v2-m3] [--out ab.json]
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import time
from pathlib import Path

import torch

from core.config import get_config
from core.db import create_pool
from core.embeddings import RERANK_MAX_TOKENS, load_embedder
from core.logging import setup_logging
from retrieval.search import SearchModels, search

QUESTIONS = [
    "How does LoRA reduce the number of trainable parameters when fine-tuning large language models?",
    "What are the main approaches to speculative decoding for faster LLM inference?",
    "How do diffusion models compare to GANs for image generation?",
    "What is known about the sensitivity conjecture and its proof?",
    "What techniques make attention faster and more memory-efficient for long sequences?",
    "How does QLoRA extend LoRA?",
    "What is speculative decoding?",
]


def load_cross_encoder(name: str) -> object:
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(name, device="cuda")
    model.max_seq_length = RERANK_MAX_TOKENS
    model.half()
    return model


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="BAAI/bge-reranker-base,BAAI/bge-reranker-v2-m3")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    setup_logging("WARNING")
    cfg = get_config()
    embedder = load_embedder(cfg)
    pool = await create_pool(1, 1)
    results: dict[str, list[dict[str, object]]] = {}
    memory: dict[str, dict[str, float]] = {}
    for name in args.models.split(","):
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        base = torch.cuda.memory_allocated()
        reranker = load_cross_encoder(name)
        weights = (torch.cuda.memory_allocated() - base) / 2**20
        models = SearchModels(embedder, reranker, cfg)  # type: ignore[arg-type]
        rows = []
        async with pool.acquire() as conn:
            await search(conn, models, cfg, "warm-up query about graph neural networks", with_passages=False)
            for q in QUESTIONS:
                started = time.perf_counter()
                r = await search(conn, models, cfg, q, limit=10, with_passages=False)
                rows.append({
                    "q": q, "rerank_ms": r.timings_ms.get("papers.rerank", 0.0),
                    "total_ms": (time.perf_counter() - started) * 1000,
                    "top": [(p.arxiv_id, round(p.rerank_score, 3), p.title, p.name_match, p.via_anchor)
                            for p in r.papers],
                })  # fmt: skip
        memory[name] = {"weights_mb": weights, "peak_mb": (torch.cuda.max_memory_allocated() - base) / 2**20}
        results[name] = rows
        del reranker, models
        gc.collect()

    names = list(results)
    for i, q in enumerate(QUESTIONS):
        print(f"\n### Q{i + 1}: {q}")
        for n in names:
            row = results[n][i]
            print(f"  [{n.split('/')[-1]}] rerank {row['rerank_ms']:.0f} ms")
            for j, (a, s, t, nm, anc) in enumerate(row["top"], 1):  # type: ignore[misc]
                flags = ("N" if nm else " ") + ("A" if anc else " ")
                print(f"    {j:>2}. {a:<11} {s:.3f} {flags} {t[:70]}")
    print("\nmodel                     rerank ms (median / mean)   GPU weights   GPU peak")
    for n in names:
        ms = [r["rerank_ms"] for r in results[n]]
        print(f"{n.split('/')[-1]:<25} {statistics.median(ms):8.0f} / {statistics.mean(ms):6.0f}"  # type: ignore[arg-type]
              f"   {memory[n]['weights_mb']:8.0f} MB   {memory[n]['peak_mb']:6.0f} MB")  # fmt: skip
    if args.out:
        Path(args.out).write_text(json.dumps({"results": results, "memory": memory}, indent=1), encoding="utf-8")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
