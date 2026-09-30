"""Full-text pipeline stages (spec 8), run by the arq worker. Each stage is idempotent per paper.

pdf_fetched -> parse (GROBID, PyMuPDF fallback) -> parsed
            -> chunk + embed (one transaction)  -> embedded
            -> resolve references into edges    -> ready   (publishes fetch:done:{id})

PDF lifecycle: deleted right after the chunk/embedding commit; kept on parse failure for
retry; after MAX_PARSE_FAILURES deleted and the paper marked abstract_only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

import asyncpg
from pgvector import HalfVector
from redis.asyncio import Redis

from core.arxiv import file_stem
from core.config import AppConfig, Settings
from core.embeddings import embed_passages
from core.fetchq import enqueue_meta
from core.redis import fetch_done_channel
from ingest import metrics
from ingest.chunking import Chunk, chunk_document, embedding_input
from ingest.grobid import GrobidClient, GrobidError
from ingest.pdftext import parse_pdf
from ingest.references import ResolutionStats, lock_citation_graph, resolve_for_paper, store_references
from ingest.tei import ParsedDoc, parse_tei, read_parsed, to_tei, write_parsed

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

GROBID_ATTEMPTS = 2
MAX_PARSE_FAILURES = 3


def pdf_path(settings: Settings, arxiv_id: str) -> Path:
    return settings.data_dir / "pdf_tmp" / f"{file_stem(arxiv_id)}.pdf"


def parsed_path(settings: Settings, arxiv_id: str) -> Path:
    return settings.data_dir / "parsed" / f"{file_stem(arxiv_id)}.tei.xml.gz"


@dataclass
class PipelineDeps:
    cfg: AppConfig
    settings: Settings
    pool: asyncpg.Pool
    redis: Redis
    grobid: GrobidClient
    embedder: SentenceTransformer
    gpu_lock: asyncio.Lock


async def _set_state(conn: asyncpg.Connection | asyncpg.Pool, arxiv_id: str, state: str, **cols: Any) -> None:
    sets = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(cols))
    await conn.execute(
        f"UPDATE papers SET state = $2{', ' + sets if sets else ''} WHERE arxiv_id = $1",
        arxiv_id,
        state,
        *cols.values(),
    )


async def _publish_done(redis: Redis, arxiv_id: str, status: str) -> None:
    await redis.publish(fetch_done_channel(arxiv_id), status)


# ---- parse ------------------------------------------------------------------------


async def parse_stage(deps: PipelineDeps, arxiv_id: str) -> str:
    row = await deps.pool.fetchrow("SELECT state, fail_count FROM papers WHERE arxiv_id = $1", arxiv_id)
    if row is None or row["state"] != "pdf_fetched":
        return f"skip ({row['state'] if row else 'missing'})"
    pdf_file = pdf_path(deps.settings, arxiv_id)
    if not pdf_file.exists():
        await _set_state(deps.pool, arxiv_id, "failed", last_error="pdf missing before parse")
        return "pdf missing"
    pdf = pdf_file.read_bytes()

    started = time.monotonic()
    tei: bytes | None = None
    parser = "grobid"
    errors: list[str] = []
    grobid_s = 0.0  # time inside GROBID, summed over attempts
    for _ in range(GROBID_ATTEMPTS):
        try:
            result = await deps.grobid.process_fulltext(pdf, pdf_file.name)
            grobid_s += result.request_s
            tei = result.tei
            await asyncio.to_thread(parse_tei, tei)  # must be readable; CPU-bound, keep it off the event loop
            break
        except (GrobidError, ET.ParseError) as e:
            errors.append(f"grobid: {e}")
            tei = None
    if tei is None:
        parser = "pymupdf"
        try:
            doc = await asyncio.to_thread(parse_pdf, pdf)
            tei = to_tei(doc)
        except Exception as e:
            errors.append(f"pymupdf: {e}")
    parse_s = time.monotonic() - started

    if tei is None:
        fails = row["fail_count"] + 1
        error = "; ".join(errors)[:1000]
        if fails >= MAX_PARSE_FAILURES:
            pdf_file.unlink(missing_ok=True)
            await _set_state(deps.pool, arxiv_id, "abstract_only", fail_count=fails, last_error=error)
            await _publish_done(deps.redis, arxiv_id, "abstract_only")
            return "abstract_only"
        # keep the PDF and stay in pdf_fetched so a retry parses it again
        await _set_state(deps.pool, arxiv_id, "pdf_fetched", fail_count=fails, last_error=error)
        raise RuntimeError(f"parse failed ({fails}/{MAX_PARSE_FAILURES}): {error}")

    write_parsed(parsed_path(deps.settings, arxiv_id), tei)
    await _set_state(deps.pool, arxiv_id, "parsed", parser=parser, last_error=None)
    await metrics.record(
        deps.redis, arxiv_id, parse_s=round(parse_s, 2), grobid_s=round(grobid_s, 2), parser=parser,
        grobid_errors=len(errors),
    )  # fmt: skip
    log.info(
        "parsed", extra={"arxiv_id": arxiv_id, "parser": parser, "s": round(parse_s, 1), "grobid_s": round(grobid_s, 1)}
    )
    return parser


# ---- chunk + embed + references -----------------------------------------------------


def _count_tokens(embedder: SentenceTransformer) -> Any:
    tok = embedder.tokenizer

    def count(texts: Sequence[str]) -> list[int]:
        return [len(ids) for ids in tok(list(texts), add_special_tokens=False)["input_ids"]]

    return count


async def chunk_embed_stage(deps: PipelineDeps, arxiv_id: str) -> str:
    paper = await deps.pool.fetchrow("SELECT state, title FROM papers WHERE arxiv_id = $1", arxiv_id)
    if paper is None or paper["state"] not in ("parsed", "embedded"):
        return f"skip ({paper['state'] if paper else 'missing'})"
    if paper["state"] == "parsed":
        # parsing and tokenizing are CPU-bound: run them in a thread so the worker's event loop (searches,
        # research runs waiting on fetches) keeps responding
        doc, chunks = await asyncio.to_thread(_parse_and_chunk, deps, arxiv_id)
        vectors: Any = []
        if chunks:
            async with deps.gpu_lock:
                vectors = await asyncio.to_thread(
                    embed_passages, deps.embedder, [embedding_input(paper["title"], c) for c in chunks]
                )
        async with deps.pool.acquire() as conn, conn.transaction():
            await _write_chunks(conn, arxiv_id, chunks, vectors)
            await store_references(conn, arxiv_id, doc.references)
            await _set_state(conn, arxiv_id, "embedded")
        pdf_path(deps.settings, arxiv_id).unlink(missing_ok=True)  # spec 8.4: right after the commit
        await metrics.record(
            deps.redis, arxiv_id, chunks=len(chunks), references=len(doc.references),
            tokens=sum(c.token_count for c in chunks),
        )  # fmt: skip

    stats = await finalize(deps, arxiv_id)
    return f"ready ({stats.resolved}/{stats.total} refs resolved)"


def _parse_and_chunk(deps: PipelineDeps, arxiv_id: str) -> tuple[ParsedDoc, list[Chunk]]:
    doc = parse_tei(read_parsed(parsed_path(deps.settings, arxiv_id)))
    _, chunks = chunk_document(doc, _count_tokens(deps.embedder), deps.cfg.chunking)
    return doc, chunks


async def _write_chunks(conn: asyncpg.Connection, arxiv_id: str, chunks: list[Chunk], vectors: Any) -> None:
    await conn.execute("DELETE FROM chunks WHERE arxiv_id = $1", arxiv_id)
    await conn.copy_records_to_table(
        "chunks",
        records=[
            (arxiv_id, c.section, c.position, c.text, c.token_count, c.char_start, c.char_end, HalfVector(v))
            for c, v in zip(chunks, vectors, strict=True)
        ],
        columns=["arxiv_id", "section", "position", "text", "token_count", "char_start", "char_end", "embedding"],
    )


REFS_PENDING = "refs:pending"  # ready papers whose references are not resolved yet (re-done after a restart)


async def finalize(deps: PipelineDeps, arxiv_id: str) -> ResolutionStats:
    """Mark ready and notify waiters as soon as the chunks are embedded (answers need only the chunks), then
    resolve references into edges. Resolution holds the global citation-graph lock, so papers finishing together
    used to wait on each other before becoming usable (up to ~70 s for a deep-mode fetch of 8 papers)."""
    # added before 'ready', so a crash in between is recovered
    await deps.redis.sadd(REFS_PENDING, arxiv_id)  # type: ignore[misc]
    await _set_state(deps.pool, arxiv_id, "ready")
    await _publish_done(deps.redis, arxiv_id, "ready")
    return await resolve_references(deps, arxiv_id)


# Resolution is serialized by a Postgres advisory lock. Queue here first, so that papers waiting their turn do
# not each hold one of the pool's few connections (8 waiting jobs starved a deep-mode run's own queries).
_RESOLVE_LOCK = asyncio.Lock()


async def resolve_references(deps: PipelineDeps, arxiv_id: str) -> ResolutionStats:
    async with _RESOLVE_LOCK, deps.pool.acquire() as conn, conn.transaction():
        await lock_citation_graph(conn)  # still needed against other processes (CLI backfills)
        stats = await resolve_for_paper(conn, arxiv_id, deps.cfg.corpus.stub_min_corpus_citations)
    await deps.redis.srem(REFS_PENDING, arxiv_id)  # type: ignore[misc]
    if stats.missing_arxiv_ids:
        await enqueue_meta(deps.redis, stats.missing_arxiv_ids)
    await metrics.record(deps.redis, arxiv_id, resolution=stats.as_dict())
    log.info(
        "references resolved",
        extra={
            "arxiv_id": arxiv_id,
            **stats.as_dict(),
            "rate": round(stats.resolved / stats.total, 3) if stats.total else None,
            "missing_arxiv_ids": len(stats.missing_arxiv_ids),
        },
    )
    return stats
