"""Answer pipeline (spec 12): a fixed sequence, no agent loop.

REWRITE -> FIND_PAPERS -> [FETCH, CHAIN: deep mode, milestone 6] -> PASSAGES -> READ -> WRITE -> VERIFY
-> CONSENSUS -> DONE.

Every step writes a `run_steps` row and publishes to Redis `run:{id}`. The run has an overall deadline
(`quick_timeout_s` / `deep_timeout_s`): once it passes, remaining optional work is skipped, the answer is
written from whatever evidence exists, verification still runs, and the run is marked `timed_out`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg
from redis.asyncio import Redis

from agent import prompts
from agent.text import DraftSentence, number_citations, parse_answer, source_id, strip_tags
from agent.threads import answer_markdown, citation_numbers, conversation_context, thread_cited
from core.config import AppConfig
from core.llm import LLMClient
from core.redis import run_channel
from retrieval.search import Filters, Passage, SearchModels, search

log = logging.getLogger(__name__)

WRITE_MAX_TOKENS = 8192
JSON_MAX_TOKENS = 8192


@dataclass
class AgentDeps:
    cfg: AppConfig
    pool: asyncpg.Pool
    redis: Redis
    llm: LLMClient
    models: SearchModels


@dataclass
class Final:
    """One answer sentence after verification."""

    seq: int
    section: str
    text: str  # with tags
    verdict: str  # supported | partial_rewritten | unsupported_removed
    tags: list[str]
    reason: str | None = None


class RunState:
    def __init__(self, deps: AgentDeps, run: asyncpg.Record) -> None:
        self.deps = deps
        self.run_id: uuid.UUID = run["id"]
        self.thread_id: uuid.UUID = run["thread_id"]
        self.turn: int = run["turn"]
        self.question: str = run["question"]
        self.mode: str = run["mode"]
        self.filters = _filters(run["filters"])
        timeout = deps.cfg.agent.deep_timeout_s if self.mode == "deep" else deps.cfg.agent.quick_timeout_s
        self.deadline = time.monotonic() + timeout
        self.seq = 0
        self.timed_out = False

    def past_deadline(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.timed_out = True
        return self.timed_out

    async def step(self, type_: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        await self.deps.pool.execute(
            "INSERT INTO run_steps (run_id, seq, type, payload) VALUES ($1, $2, $3, $4)",
            self.run_id, self.seq, type_, json.dumps(payload),
        )  # fmt: skip
        await self.deps.redis.publish(run_channel(str(self.run_id)), json.dumps({"seq": self.seq, "type": type_}))


def _filters(raw: Any) -> Filters:
    f = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    return Filters(year_from=f.get("year_from"), year_to=f.get("year_to"), categories=tuple(f.get("categories") or ()))


def _passage_id(p: Passage) -> str:
    return source_id("c", str(p.chunk_id)) if p.source == "chunk" else source_id("a", p.arxiv_id)


def format_passages(passages: Sequence[Passage], titles: dict[str, tuple[str, int | None]]) -> str:
    out = []
    for p in passages:
        title, year = titles.get(p.arxiv_id, ("", None))
        out.append(f"[{_passage_id(p)}] ({title}, {year}; section: {p.section})\n{p.text}")
    return "\n\n".join(out)


# ---- steps ----------------------------------------------------------------------------------------


async def step_rewrite(st: RunState, context: str) -> prompts.RewriteOut:
    cfg = st.deps.cfg
    res = await st.deps.llm.generate(
        prompts.rewrite_prompt(st.question, context, cfg.agent.max_queries),
        model=cfg.models.llm_light, purpose="rewrite", schema=prompts.RewriteOut, run_id=st.run_id,
        temperature=0.2, max_output_tokens=JSON_MAX_TOKENS,
    )  # fmt: skip
    out = res.parsed
    assert out is not None
    out.queries = [q.strip() for q in out.queries if q.strip()][: cfg.agent.max_queries] or [out.standalone_question]
    await st.step("rewrite", {"standalone_question": out.standalone_question, "queries": out.queries})
    return out


async def step_read(
    st: RunState, question: str, passages: list[Passage], titles: dict[str, tuple[str, int | None]]
) -> list[prompts.Note]:
    cfg = st.deps.cfg
    allowed = {_passage_id(p) for p in passages}
    res = await st.deps.llm.generate(
        prompts.read_prompt(question, format_passages(passages, titles), cfg.agent.max_notes),
        model=cfg.models.llm_light, purpose="read", schema=prompts.NotesOut, run_id=st.run_id,
        temperature=0.1, max_output_tokens=JSON_MAX_TOKENS,
    )  # fmt: skip
    assert res.parsed is not None
    notes = [n for n in res.parsed.notes if n.source_id.strip() in allowed][: cfg.agent.max_notes]
    for n in notes:
        n.source_id = n.source_id.strip()
    await st.step("read", {"notes": len(notes), "dropped_invalid_source": len(res.parsed.notes) - len(notes)})
    return notes


async def step_write(st: RunState, question: str, notes: list[prompts.Note], context: str) -> str:
    cfg = st.deps.cfg
    notes_text = "\n".join(f"- [[{n.source_id}]] {n.claim}" for n in notes)
    res = await st.deps.llm.generate(
        prompts.write_prompt(question, notes_text, context),
        model=cfg.models.llm_main, purpose="write", run_id=st.run_id, temperature=0.3,
        max_output_tokens=WRITE_MAX_TOKENS,
    )  # fmt: skip
    return res.text


async def step_verify(st: RunState, drafts: list[DraftSentence], source_texts: dict[str, str]) -> list[Final]:
    """Spec 12 VERIFY: batches of verify_batch_size; partial -> rewrite once; unsupported -> removed."""
    cfg = st.deps.cfg
    finals: list[Final] = []
    to_check: list[tuple[int, DraftSentence]] = []
    for i, d in enumerate(drafts):
        if not d.valid:
            reason = "invalid source tag" if d.invalid_tags else "no source tag"
            finals.append(Final(i, d.section, d.text, "unsupported_removed", [], reason))
        else:
            to_check.append((i, d))

    async def check(batch: list[tuple[int, DraftSentence]]) -> list[Final]:
        items = []
        for k, (_, d) in enumerate(batch):
            sources = "\n".join(f"  [{t}] {source_texts.get(t, '')}" for t in d.tags)
            items.append(f"Sentence {k}: {d.with_tags()}\nSources cited by sentence {k}:\n{sources}")
        res = await st.deps.llm.generate(
            prompts.verify_prompt("\n\n".join(items)),
            model=cfg.models.llm_light, purpose="verify", schema=prompts.VerifyOut, run_id=st.run_id,
            temperature=0.0, max_output_tokens=JSON_MAX_TOKENS,
        )  # fmt: skip
        assert res.parsed is not None
        by_index = {v.index: v for v in res.parsed.results}
        out = []
        for k, (i, d) in enumerate(batch):
            v = by_index.get(k)
            if v is None:
                out.append(Final(i, d.section, d.with_tags(), "unsupported_removed", d.tags, "no verdict"))
            elif v.verdict == "supported":
                out.append(Final(i, d.section, d.with_tags(), "supported", d.tags))
            elif v.verdict == "partial" and v.rewrite:
                out.append(Final(i, d.section, _rewrite(v.rewrite, d), "partial_rewritten", d.tags))
            else:
                out.append(Final(i, d.section, d.with_tags(), "unsupported_removed", d.tags, v.verdict))
        return out

    n = cfg.agent.verify_batch_size
    batches = [to_check[k : k + n] for k in range(0, len(to_check), n)]
    for result in await asyncio.gather(*(check(b) for b in batches)):
        finals += result
    finals.sort(key=lambda f: f.seq)
    counts = {
        v: sum(1 for f in finals if f.verdict == v) for v in ("supported", "partial_rewritten", "unsupported_removed")
    }
    await st.step("verify", {**counts, "sentences": len(finals)})
    return finals


def _rewrite(rewrite: str, original: DraftSentence) -> str:
    """Keep the rewrite's wording, but only the original sentence's tags (no second verify loop)."""
    text = strip_tags(rewrite)
    return text + " " + "".join(f"[[{t}]]" for t in original.tags)


async def step_consensus(
    st: RunState, answer: str, passages: list[Passage], titles: dict[str, tuple[str, int | None]]
) -> list[prompts.ConsensusClaim]:
    cfg = st.deps.cfg
    res = await st.deps.llm.generate(
        prompts.consensus_prompt(answer, format_passages(passages, titles), cfg.agent.consensus_claims),
        model=cfg.models.llm_light, purpose="consensus", schema=prompts.ConsensusOut, run_id=st.run_id,
        temperature=0.1, max_output_tokens=JSON_MAX_TOKENS,
    )  # fmt: skip
    assert res.parsed is not None
    claims = res.parsed.claims[: cfg.agent.consensus_claims]
    await st.step("consensus", {"claims": len(claims)})
    return claims


# ---- orchestration ------------------------------------------------------------------------------


async def execute_run(deps: AgentDeps, run_id: uuid.UUID) -> str:
    run = await deps.pool.fetchrow("SELECT * FROM research_runs WHERE id = $1", run_id)
    if run is None:
        return "missing"
    if run["status"] not in ("queued", "running"):
        return f"skip ({run['status']})"
    await deps.pool.execute(
        "UPDATE research_runs SET status = 'running', started_at = coalesce(started_at, now()) WHERE id = $1", run_id
    )
    await deps.pool.execute("DELETE FROM run_steps WHERE run_id = $1", run_id)  # restart from scratch (M6: resume)
    st = RunState(deps, run)
    try:
        status = await _run(st)
    except Exception as e:
        log.exception("run failed", extra={"run_id": str(run_id)})
        await st.step("write", {"error": f"{type(e).__name__}: {e}"[:1000]})
        status = "failed"
    await _finish(deps, run_id, status)
    return status


async def _run(st: RunState) -> str:
    deps, cfg = st.deps, st.deps.cfg
    async with deps.pool.acquire() as conn:
        context = await conversation_context(conn, st.thread_id, st.turn, cfg.agent.thread_full_turns)
        carry_papers, reuse_chunks = await thread_cited(conn, st.thread_id)

    rw = await step_rewrite(st, context)
    question = rw.standalone_question
    await deps.pool.execute("UPDATE research_runs SET standalone_question = $2 WHERE id = $1", st.run_id, question)

    async with deps.pool.acquire() as conn:
        result = await search(
            conn, deps.models, cfg, question, queries=rw.queries, filters=st.filters,
            carryover=carry_papers, carryover_boost=cfg.agent.thread_carryover_boost, reuse_chunks=reuse_chunks,
        )  # fmt: skip
    selected = [
        {"arxiv_id": p.arxiv_id, "title": p.title, "year": p.year, "rerank_score": round(p.rerank_score, 4),
         "final_score": round(p.final_score, 4), "full_text": p.state == "ready", "via_anchor": p.via_anchor,
         "carryover": p.arxiv_id in carry_papers, "via_chain": None, "badges": p.badges}
        for p in result.papers
    ]  # fmt: skip
    await deps.pool.execute(
        "UPDATE research_runs SET selected_papers = $2 WHERE id = $1", st.run_id, json.dumps(selected)
    )
    await st.step("find_papers", {"papers": selected, "timings_ms": result.timings_ms, "counts": result.counts})

    passages = result.passages
    titles = {p.arxiv_id: (p.title, p.year) for p in result.papers}
    missing_titles = {p.arxiv_id for p in passages} - set(titles)
    if missing_titles:  # reused chunks from papers not selected this turn
        rows = await deps.pool.fetch(
            "SELECT arxiv_id, title, extract(year FROM v1_date)::int AS year FROM papers WHERE arxiv_id = ANY($1)",
            list(missing_titles),
        )
        titles.update({r["arxiv_id"]: (r["title"], r["year"]) for r in rows})
    await st.step(
        "passages",
        {"passages": [{"id": _passage_id(p), "arxiv_id": p.arxiv_id, "section": p.section,
                       "rerank_score": round(p.rerank_score, 4)} for p in passages]},
    )  # fmt: skip
    if not passages:
        await st.step("write", {"error": "no relevant papers found (all below rerank_min_score)"})
        return "done"

    if st.past_deadline():
        notes = [prompts.Note(claim=p.text, source_id=_passage_id(p)) for p in passages]  # write from raw evidence
        await st.step("read", {"skipped": "timeout", "notes": len(notes)})
    else:
        notes = await step_read(st, question, passages, titles)

    raw = await step_write(st, question, notes, context)
    by_id = {_passage_id(p): p for p in passages}
    drafts = parse_answer(raw, set(by_id))
    await st.step("write", {"sentences": len(drafts), "untagged_or_invalid": sum(1 for d in drafts if not d.valid)})

    source_texts = {
        sid: (f"{titles.get(p.arxiv_id, ('', None))[0]}. {p.text}" if p.source == "abstract" else p.text)
        for sid, p in by_id.items()
    }
    finals = await step_verify(st, drafts, source_texts)

    claims: list[prompts.ConsensusClaim] = []
    kept_text = " ".join(strip_tags(f.text) for f in finals if f.verdict != "unsupported_removed")
    if kept_text and not st.past_deadline():
        claims = await step_consensus(st, kept_text, passages, titles)

    await _store(st, finals, by_id, notes, claims)
    return "timed_out" if st.timed_out else "done"


async def _store(
    st: RunState,
    finals: list[Final],
    by_id: dict[str, Passage],
    notes: list[prompts.Note],
    claims: list[prompts.ConsensusClaim],
) -> None:
    def src(tag: str) -> dict[str, Any]:
        p = by_id[tag]
        return {"id": tag, "arxiv_id": p.arxiv_id, "chunk_id": p.chunk_id, "type": p.source, "section": p.section}

    async with st.deps.pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM answer_sentences WHERE run_id = $1", st.run_id)
        await conn.executemany(
            "INSERT INTO answer_sentences (run_id, seq, section, text, verdict, sources) VALUES ($1,$2,$3,$4,$5,$6)",
            [(st.run_id, f.seq, f.section, f.text, f.verdict, json.dumps([src(t) for t in f.tags])) for f in finals],
        )
        await conn.execute("DELETE FROM evidence_notes WHERE run_id = $1", st.run_id)
        await conn.executemany(
            "INSERT INTO evidence_notes (run_id, claim, chunk_id, arxiv_id, source_type) VALUES ($1,$2,$3,$4,$5)",
            [(st.run_id, n.claim, by_id[n.source_id].chunk_id, by_id[n.source_id].arxiv_id,
              "chunk" if by_id[n.source_id].source == "chunk" else "abstract") for n in notes if n.source_id in by_id],
        )  # fmt: skip
        await conn.execute("DELETE FROM consensus WHERE run_id = $1", st.run_id)
        for c in claims:
            items = [
                {"arxiv_id": by_id[i.source_id].arxiv_id, "chunk_id": by_id[i.source_id].chunk_id, "stance": i.stance}
                for i in c.items
                if i.source_id in by_id
            ]
            await conn.execute(
                "INSERT INTO consensus (run_id, claim, items) VALUES ($1, $2, $3)",
                st.run_id,
                c.claim,
                json.dumps(items),
            )
        # Spec 12c: stable per-thread numbering, in order of first citation.
        cited = [by_id[t].arxiv_id for f in finals if f.verdict != "unsupported_removed" for t in f.tags]
        existing = await citation_numbers(conn, st.thread_id)
        numbers = number_citations(existing, list(dict.fromkeys(cited)))
        await conn.executemany(
            "INSERT INTO thread_citations (thread_id, arxiv_id, number) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            [(st.thread_id, a, n) for a, n in numbers.items() if a not in existing],
        )


async def _finish(deps: AgentDeps, run_id: uuid.UUID, status: str) -> None:
    await deps.pool.execute(
        "UPDATE research_runs r SET status = $2, finished_at = now(),"
        " cost_usd = coalesce(c.cost, 0), input_tokens = coalesce(c.inp, 0), output_tokens = coalesce(c.outp, 0)"
        " FROM (SELECT sum(cost_usd) AS cost, sum(input_tokens) AS inp, sum(output_tokens) AS outp"
        "       FROM llm_calls WHERE run_id = $1) c WHERE r.id = $1",
        run_id,
        status,
    )
    thread_id = await deps.pool.fetchval(
        "UPDATE threads SET updated_at = now() WHERE id = (SELECT thread_id FROM research_runs WHERE id = $1)"
        " RETURNING id",
        run_id,
    )
    await deps.redis.publish(run_channel(str(run_id)), json.dumps({"status": status}))
    if status in ("done", "timed_out") and thread_id is not None:
        try:
            await update_summary(deps, thread_id)
        except Exception:
            log.exception("summary update failed", extra={"thread_id": str(thread_id)})


async def update_summary(deps: AgentDeps, thread_id: uuid.UUID) -> None:
    """Spec 12b: once a thread has more than thread_full_turns turns, fold the turn that just left the
    verbatim window into the running summary."""
    cfg = deps.cfg
    async with deps.pool.acquire() as conn:
        runs = await conn.fetch(
            "SELECT id, turn, question FROM research_runs WHERE thread_id = $1 AND status IN ('done', 'timed_out')"
            " ORDER BY turn",
            thread_id,
        )
        if len(runs) <= cfg.agent.thread_full_turns:
            return
        folded = runs[-cfg.agent.thread_full_turns - 1]
        numbers = await citation_numbers(conn, thread_id)
        answer = await answer_markdown(conn, folded["id"], numbers)
        previous = await conn.fetchval("SELECT summary FROM threads WHERE id = $1", thread_id)
    res = await deps.llm.generate(
        prompts.summary_prompt(previous or "", f"Question: {folded['question']}\nAnswer:\n{answer}"),
        model=cfg.models.llm_light, purpose="thread_summary", temperature=0.2, max_output_tokens=JSON_MAX_TOKENS,
    )  # fmt: skip
    await deps.pool.execute("UPDATE threads SET summary = $2 WHERE id = $1", thread_id, clean_summary(res.text))


def clean_summary(text: str) -> str:
    """Drop a leading label line such as "**Updated Summary:**" that the model sometimes adds."""
    lines = text.strip().splitlines()
    if lines and re.fullmatch(r"\W*(updated\s+)?(running\s+)?summary\W*", lines[0].strip(), re.IGNORECASE):
        lines = lines[1:]
    return "\n".join(lines).strip()
