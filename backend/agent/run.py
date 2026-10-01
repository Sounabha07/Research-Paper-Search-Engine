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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
from redis.asyncio import Redis

from agent import prompts
from agent.text import DraftSentence, number_citations, parse_answer, source_id, strip_tags
from agent.threads import answer_markdown, citation_numbers, conversation_context, thread_cited
from agent.verify import SourceText, altered_math, find_span, missing_names
from core.config import AppConfig
from core.embeddings import abstract_text
from core.fetchq import enqueue_fulltext
from core.llm import LLMClient
from core.redis import fetch_done_channel, run_channel
from retrieval.search import Filters, PaperHit, Passage, SearchModels, _json, search, search_passages

log = logging.getLogger(__name__)

WRITE_MAX_TOKENS = 8192
JSON_MAX_TOKENS = 8192
CHAIN_MAX_CANDIDATES = 100


class RunCancelled(Exception):
    pass


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
    span: str | None = None  # exact supporting excerpt copied by the verifier
    span_source: str | None = None  # the cited source containing it


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
        # the budget counts from the run's first start; a resumed run gets at most resume_grace_s more (load_progress)
        elapsed = (datetime.now(UTC) - run["started_at"]).total_seconds() if run["started_at"] else 0.0
        self.deadline = time.monotonic() + timeout - max(0.0, elapsed)
        self.seq = 0
        self.timed_out = False
        self.done: dict[str, dict[str, Any]] = {}  # completed steps, for resuming after a worker restart

    async def load_progress(self) -> None:
        """Resume: keep completed steps (their payloads hold the step outputs); redo the rest."""
        await self.deps.pool.execute(
            "DELETE FROM run_steps WHERE run_id = $1 AND (type IN ('verify', 'consensus') OR payload ? 'error')",
            self.run_id,
        )
        rows = await self.deps.pool.fetch(
            "SELECT seq, type, payload FROM run_steps WHERE run_id = $1 ORDER BY seq", self.run_id
        )
        self.done = {r["type"]: json.loads(r["payload"]) for r in rows}
        self.seq = max((r["seq"] for r in rows), default=0)
        if self.done:
            # a worker restart (plus ~90 s of model loading) must not use up the whole budget of a resumed run
            self.deadline = max(self.deadline, time.monotonic() + self.deps.cfg.agent.resume_grace_s)

    async def check_cancel(self) -> None:
        if await self.deps.redis.exists(cancel_key(self.run_id)):
            raise RunCancelled

    async def progress(self, text: str, **extra: Any) -> None:
        await self.deps.redis.publish(run_channel(str(self.run_id)), json.dumps({"progress": text, **extra}))

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
    return Filters(
        year_from=f.get("year_from"), year_to=f.get("year_to"), categories=tuple(f.get("categories") or ()),
        restrict_ids=tuple(f.get("restrict_ids") or ()),
    )  # fmt: skip


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
    await st.step("read", {"notes": len(notes), "dropped_invalid_source": len(res.parsed.notes) - len(notes),
                           "items": [n.model_dump() for n in notes]})  # fmt: skip
    return notes


def format_notes(notes: list[prompts.Note], info: dict[str, SourceText]) -> str:
    """WRITE input: each note with its source's first author, status (preprint / venue / landmark) and title."""
    lines = []
    for n in notes:
        s = info.get(n.source_id)
        about = f' ({s.authors.split(", ")[0] or "unknown author"}; {s.status}; "{s.title[:90]}")' if s else ""
        lines.append(f"- [[{n.source_id}]]{about} {n.claim}")
    return "\n".join(lines)


def mostly_untagged(raw: str, allowed: set[str]) -> bool:
    """A draft where fewer than half of the sentences carry a valid source tag (flash-lite occasionally drops
    the tags altogether, which would make VERIFY remove the whole answer)."""
    drafts = parse_answer(raw, allowed)
    return not drafts or sum(d.valid for d in drafts) * 2 < len(drafts)


async def step_write(
    st: RunState, question: str, notes: list[prompts.Note], context: str, info: dict[str, SourceText]
) -> tuple[str, str]:
    """Returns (answer text, the model that wrote it; the light model after a 503/429 fallback). A draft that is
    mostly untagged is written once more (at a different temperature, so the response cache does not repeat it)."""
    cfg = st.deps.cfg
    prompt = prompts.write_prompt(question, format_notes(notes, info), context)
    res = None
    for temperature in (0.3, 0.5):
        res = await st.deps.llm.generate(
            prompt, model=cfg.models.write_model, purpose="write", run_id=st.run_id, temperature=temperature,
            max_output_tokens=WRITE_MAX_TOKENS, thinking_level=cfg.models.write_thinking_level,
        )  # fmt: skip
        if not mostly_untagged(res.text, set(info)):
            break
        log.warning("write draft mostly untagged, writing again", extra={"run_id": str(st.run_id)})
    assert res is not None
    return res.text, res.model


async def verify_sentences(
    llm: LLMClient,
    cfg: AppConfig,
    drafts: list[DraftSentence],
    sources: dict[str, SourceText],
    run_id: uuid.UUID | None,
    require_span: bool = True,
    name_check: bool = True,
) -> list[Final]:
    """Spec 12 VERIFY: batches of verify_batch_size; partial -> rewrite once (the rewrite is re-checked and
    removed unless supported); unsupported -> removed.

    With require_span, a supported/partial verdict also needs an exact supporting excerpt from a cited
    source (agent/verify.py), else the sentence is removed. With name_check, names in the sentence must
    appear in its cited sources.
    """
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
            cited = "\n".join(f"  [{t}]{_source_label(sources.get(t))} {sources[t].text if t in sources else ''}"
                              for t in d.tags)  # fmt: skip
            items.append(f"Sentence {k}: {d.with_tags()}\nSources cited by sentence {k}:\n{cited}")
        res = await llm.generate(
            prompts.verify_prompt("\n\n".join(items)),
            model=cfg.models.llm_light, purpose="verify", schema=prompts.VerifyOut, run_id=run_id,
            temperature=0.0, max_output_tokens=JSON_MAX_TOKENS,
        )  # fmt: skip
        assert res.parsed is not None
        by_index = {v.index: v for v in res.parsed.results}
        return [
            _apply_verdict(i, d, by_index.get(k), sources, require_span, name_check) for k, (i, d) in enumerate(batch)
        ]

    n = cfg.agent.verify_batch_size
    batches = [to_check[k : k + n] for k in range(0, len(to_check), n)]
    for result in await asyncio.gather(*(check(b) for b in batches)):
        finals += result

    # A rewrite is checked once more (not rewritten again): it is kept only if it is now "supported" with a
    # valid span, so a rewrite cannot carry an unsupported detail over from the original sentence.
    rewritten = [f for f in finals if f.verdict == "partial_rewritten"] if require_span else []
    if rewritten:
        again = [(f.seq, DraftSentence(f.section, strip_tags(f.text), list(f.tags))) for f in rewritten]
        rechecks = await asyncio.gather(*(check(again[k : k + n]) for k in range(0, len(again), n)))
        by_seq = {r.seq: r for batch in rechecks for r in batch}
        for k, f in enumerate(finals):
            r = by_seq.get(f.seq)
            if f.verdict == "partial_rewritten" and (r is None or r.verdict != "supported"):
                finals[k] = Final(f.seq, f.section, f.text, "unsupported_removed", f.tags, "rewrite not supported")
            elif f.reason == SPURIOUS_ATTRIBUTION:  # the original sentence itself passed the re-check
                finals[k] = Final(f.seq, f.section, f.text, "supported", f.tags, span=r.span if r else None,
                                  span_source=r.span_source if r else None)  # fmt: skip
    finals.sort(key=lambda f: f.seq)
    return finals


def is_preprint(status: str) -> bool:
    return status.startswith("preprint")


def _source_label(s: SourceText | None) -> str:
    if not s or not s.status:
        return ""
    kind = s.status if is_preprint(s.status) else f"{s.status}, NOT a preprint"
    return f" (status: {kind}; first author: {s.authors.split(', ')[0]})"


STATUS_COLUMNS = "is_landmark, is_foundational, venue_accepted, venue_name, journal_ref"


def source_status(row: Mapping[str, Any], year: int | None) -> str:
    """How settled a source's claims are, for WRITE and VERIFY. Landmark and foundational papers are never
    preprints, with or without a venue in the arXiv metadata. `row` has the STATUS_COLUMNS of `papers`."""
    known = "landmark" if row["is_landmark"] else "foundational" if row["is_foundational"] else None
    if row["venue_accepted"] or row["journal_ref"]:
        reviewed = f"peer-reviewed: {row['venue_name'] or row['journal_ref']}"
        return f"{known}, {reviewed}" if known else reviewed
    if known:
        return known
    return f"preprint ({year})" if year else "preprint"


async def source_texts(
    pool: asyncpg.Pool, by_id: dict[str, Passage], titles: dict[str, tuple[str, int | None]]
) -> dict[str, SourceText]:
    """Source id -> text (abstracts prefixed with the title), paper title, status, and author last names."""
    rows = await pool.fetch(
        f"SELECT arxiv_id, {STATUS_COLUMNS},"
        " extract(year FROM v1_date)::int AS year, authors FROM papers WHERE arxiv_id = ANY($1)",
        list({p.arxiv_id for p in by_id.values()}),
    )
    meta = {r["arxiv_id"]: (source_status(r, r["year"]), author_names(r["authors"])) for r in rows}  # fmt: skip
    out = {}
    for sid, p in by_id.items():
        title = titles.get(p.arxiv_id, ("", None))[0]
        status, authors = meta.get(p.arxiv_id, ("", ""))
        text = f"{title}. {p.text}" if p.source == "abstract" else p.text
        out[sid] = SourceText(title=title, text=text, status=status, authors=authors)
    return out


def author_names(authors: Any, limit: int = 6) -> str:
    items = json.loads(authors) if isinstance(authors, str) else authors or []
    return ", ".join(a["last"] for a in items[:limit] if a.get("last"))


def _apply_verdict(
    i: int, d: DraftSentence, v: prompts.Verdict | None, sources: dict[str, SourceText], require_span: bool,
    name_check: bool,
) -> Final:  # fmt: skip
    if v is None:
        return Final(i, d.section, d.with_tags(), "unsupported_removed", d.tags, "no verdict")
    if v.verdict == "unsupported" or (v.verdict == "partial" and not v.rewrite):
        return Final(i, d.section, d.with_tags(), "unsupported_removed", d.tags, v.verdict)
    text = d.with_tags() if v.verdict == "supported" else _rewrite(v.rewrite or "", d)
    verdict = "supported" if v.verdict == "supported" else "partial_rewritten"
    reason = None
    if verdict == "partial_rewritten" and spurious_preprint_attribution(v.rewrite or "", d, sources):
        # the verifier attributed a claim to "a preprint" that is not one (seen: QLoRA, a landmark, rewritten as
        # "a 2024 preprint by Dettmers"): keep the original sentence and let the re-check judge it as written
        text, reason = d.with_tags(), SPURIOUS_ATTRIBUTION
    if verdict == "partial_rewritten" and altered_math(strip_tags(text), d.text, sources, d.tags):
        return Final(i, d.section, text, "unsupported_removed", d.tags, "rewrite altered math")
    span_source = find_span(v.span, sources, d.tags)
    if require_span and span_source is None:
        return Final(i, d.section, text, "unsupported_removed", d.tags, "no valid span")
    if name_check:
        missing = missing_names(strip_tags(text), sources, d.tags)
        if missing:
            return Final(
                i, d.section, text, "unsupported_removed", d.tags, f"names not in source: {', '.join(missing)}"
            )
    return Final(
        i, d.section, text, verdict, d.tags, reason, span=v.span if span_source else None, span_source=span_source
    )


SPURIOUS_ATTRIBUTION = "spurious preprint attribution dropped"
_PREPRINT_WORD = re.compile(r"\bpre-?prints?\b", re.I)


def spurious_preprint_attribution(rewrite: str, d: DraftSentence, sources: dict[str, SourceText]) -> bool:
    """The rewrite calls something a preprint although no cited source is one (statuses known)."""
    statuses = [sources[t].status for t in d.tags if t in sources and sources[t].status]
    return (
        bool(statuses)
        and not any(is_preprint(s) for s in statuses)
        and bool(_PREPRINT_WORD.search(rewrite))
        and not _PREPRINT_WORD.search(d.text)
    )


async def step_verify(st: RunState, drafts: list[DraftSentence], sources: dict[str, SourceText]) -> list[Final]:
    finals = await verify_sentences(st.deps.llm, st.deps.cfg, drafts, sources, st.run_id)
    counts = {
        v: sum(1 for f in finals if f.verdict == v) for v in ("supported", "partial_rewritten", "unsupported_removed")
    }
    reasons: dict[str, int] = {}
    for f in finals:
        if f.reason:
            key = f.reason.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
    await st.step("verify", {**counts, "sentences": len(finals), "removed_reasons": reasons})
    return finals


LOOSE_TAG = re.compile(r"\s*\[\s*[ca]\s*:\s*[^\]\s]+\s*\]")


def _rewrite(rewrite: str, original: DraftSentence) -> str:
    """Keep the rewrite's wording, but only the original sentence's tags (no second verify loop)."""
    text = " ".join(LOOSE_TAG.sub("", strip_tags(rewrite)).split())  # the verifier sometimes echoes "[c:12]"
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


# ---- deep-mode steps -----------------------------------------------------------------------------


async def step_fetch(st: RunState, hits: list[PaperHit]) -> None:
    """Spec 12 FETCH: queue selected papers without full text (urgent, rank order) and wait for them up to
    fetch_wait_s (bounded by the run deadline), streaming progress. Papers still missing use their abstract.
    Only the top deep_max_fetch selected papers are fetched; the rest use their abstract (or cached full text)."""
    deps, cfg = st.deps, st.deps.cfg
    top = hits[: cfg.agent.deep_max_fetch]
    targets = [h.arxiv_id for h in top if h.state != "ready"]
    not_fetched = sum(1 for h in hits[cfg.agent.deep_max_fetch :] if h.state != "ready")
    if not targets:
        await st.step("fetch", {"requested": 0, "ready": 0, "abstract_only": 0, "still_missing": [], "waited_s": 0,
                                "beyond_max_fetch": not_fetched})  # fmt: skip
        return
    started = time.monotonic()
    pubsub = deps.redis.pubsub()
    await pubsub.subscribe(*(fetch_done_channel(a) for a in targets))  # before enqueueing: no missed messages
    try:
        async with deps.pool.acquire() as conn:
            outcome = await enqueue_fulltext(conn, deps.redis, targets, urgent=True)
        finished: dict[str, str] = {a: "ready" for a, o in outcome.items() if o == "already_ready"}
        finished |= {a: "abstract_only" for a, o in outcome.items() if o in ("abstract_only", "not_found")}
        pending = [a for a in targets if a not in finished]
        wait_until = min(started + cfg.agent.fetch_wait_s, st.deadline)
        last_db_check = time.monotonic()

        async def report() -> None:
            n_ready = sum(1 for s in finished.values() if s == "ready")
            await st.progress(f"fetched {n_ready} of {len(targets)}", fetched=n_ready, total=len(targets))

        await report()
        while pending and time.monotonic() < wait_until:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is not None:
                arxiv_id = str(msg["channel"]).removeprefix(fetch_done_channel(""))
                if arxiv_id in pending:
                    pending.remove(arxiv_id)
                    finished[arxiv_id] = str(msg["data"])
                    await report()
            if time.monotonic() - last_db_check > 5:  # a paper can finish between steps of this loop
                last_db_check = time.monotonic()
                rows = await deps.pool.fetch(
                    "SELECT arxiv_id, state FROM papers"
                    " WHERE arxiv_id = ANY($1) AND state IN ('ready', 'abstract_only')",
                    pending,
                )
                for r in rows:
                    pending.remove(r["arxiv_id"])
                    finished[r["arxiv_id"]] = r["state"]
                if rows:
                    await report()
            await st.check_cancel()
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()  # type: ignore[no-untyped-call]
    await st.step("fetch", {
        "requested": len(targets),
        "ready": sum(1 for s in finished.values() if s == "ready"),
        "abstract_only": sum(1 for s in finished.values() if s != "ready"),
        "still_missing": pending,
        "waited_s": round(time.monotonic() - started, 1),
        "beyond_max_fetch": not_fetched,
    })  # fmt: skip


async def step_chain(st: RunState, question: str, hits: list[PaperHit]) -> list[PaperHit]:
    """Spec 12 CHAIN (one hop): rerank the in-corpus references of the top selected papers; add up to
    chain_max_added whose score reaches the lowest selected paper's. Marked via_chain with the citing paper."""
    deps, cfg = st.deps, st.deps.cfg
    top = [h.arxiv_id for h in hits[: cfg.agent.chain_from_top_papers]]
    selected = {h.arxiv_id for h in hits}
    rows = await deps.pool.fetch(
        "SELECT e.citing_id, e.cited_id FROM citation_edges e JOIN papers p ON p.arxiv_id = e.cited_id"
        " WHERE e.citing_id = ANY($1::text[]) AND p.abstract <> ''",
        top,
    )
    via: dict[str, str] = {}
    for citing in top:  # a reference cited by several top papers is credited to the highest-ranked one
        for r in rows:
            if r["citing_id"] == citing and r["cited_id"] not in selected:
                via.setdefault(r["cited_id"], citing)
    candidates = list(via)[:CHAIN_MAX_CANDIDATES]
    added: list[PaperHit] = []
    if candidates:
        loaded = await load_hits(deps.pool, [{"arxiv_id": a} for a in candidates])
        scores = await deps.models.rerank(question, [abstract_text(h.title, h.abstract) for h in loaded])
        threshold = min((h.rerank_score for h in hits), default=0.0)
        for h, s in zip(loaded, scores, strict=True):
            h.rerank_score = h.final_score = s
            h.via_chain = via[h.arxiv_id]
        ranked = sorted(loaded, key=lambda h: -h.rerank_score)
        added = [h for h in ranked if h.rerank_score >= threshold][: cfg.agent.chain_max_added]
    await st.step("chain", {
        "from_papers": top, "candidates": len(candidates),
        "added": [{"arxiv_id": h.arxiv_id, "title": h.title, "rerank_score": round(h.rerank_score, 4),
                   "via_chain": h.via_chain, "full_text": h.state == "ready"} for h in added],
    })  # fmt: skip
    return added


# ---- orchestration ------------------------------------------------------------------------------


def cancel_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:cancel"


def _lock_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:lock"


async def execute_run(deps: AgentDeps, run_id: uuid.UUID) -> str:
    run = await deps.pool.fetchrow("SELECT * FROM research_runs WHERE id = $1", run_id)
    if run is None:
        return "missing"
    if run["status"] not in ("queued", "running"):
        return f"skip ({run['status']})"
    lock_ttl = deps.cfg.agent.deep_timeout_s + 300
    if not await deps.redis.set(_lock_key(run_id), "1", nx=True, ex=lock_ttl):
        return "already running elsewhere"
    try:
        run = await deps.pool.fetchrow(
            "UPDATE research_runs SET status = 'running', started_at = coalesce(started_at, now()) WHERE id = $1"
            " RETURNING *",
            run_id,
        )
        st = RunState(deps, run)
        await st.load_progress()
        try:
            status = await _run(st)
        except RunCancelled:
            await st.progress("cancelled")
            status = "cancelled"
        except Exception as e:
            log.exception("run failed", extra={"run_id": str(run_id)})
            await st.step("write", {"error": f"{type(e).__name__}: {e}"[:1000]})
            status = "failed"
        await _finish(deps, run_id, status)
        return status
    finally:
        await deps.redis.delete(_lock_key(run_id), cancel_key(run_id))


async def _run(st: RunState) -> str:
    deps, cfg = st.deps, st.deps.cfg
    async with deps.pool.acquire() as conn:
        context = await conversation_context(conn, st.thread_id, st.turn, cfg.agent.thread_full_turns)
        carry_papers, reuse_chunks = await thread_cited(conn, st.thread_id)
        pinned = list(await conn.fetchval("SELECT pinned_papers FROM threads WHERE id = $1", st.thread_id) or [])

    # REWRITE
    if "rewrite" in st.done:
        question, queries = st.done["rewrite"]["standalone_question"], st.done["rewrite"]["queries"]
    else:
        rw = await step_rewrite(st, context)
        question, queries = rw.standalone_question, rw.queries
        await deps.pool.execute("UPDATE research_runs SET standalone_question = $2 WHERE id = $1", st.run_id, question)
    await st.check_cancel()

    # FIND_PAPERS
    if "find_papers" in st.done:
        hits = await load_hits(deps.pool, st.done["find_papers"]["papers"])
    else:
        async with deps.pool.acquire() as conn:
            result = await search(
                conn, deps.models, cfg, question, queries=queries, filters=st.filters, with_passages=False,
                carryover=carry_papers, carryover_boost=cfg.agent.thread_carryover_boost, pinned=pinned,
            )  # fmt: skip
        hits = result.papers
        await _save_selected(st, hits, carry_papers, pinned)
        await st.step("find_papers", {"papers": _selected_payload(hits, carry_papers, pinned),
                                      "timings_ms": result.timings_ms, "counts": result.counts})  # fmt: skip
    await st.check_cancel()

    if st.mode == "deep":
        # FETCH
        if "fetch" not in st.done and not st.past_deadline():
            await step_fetch(st, hits)
        await _refresh_states(deps.pool, hits)
        await st.check_cancel()
        # CHAIN
        if "chain" in st.done:
            added = await load_hits(deps.pool, st.done["chain"]["added"])
        elif not st.past_deadline():
            added = await step_chain(st, question, hits)
        else:
            added = []
        if added:
            hits = hits + added
            await _save_selected(st, hits, carry_papers, pinned)
        await st.check_cancel()

    # PASSAGES
    titles = {h.arxiv_id: (h.title, h.year) for h in hits}
    if "passages" in st.done:
        passages = await load_passages(deps.pool, st.done["passages"]["passages"])
    else:
        async with deps.pool.acquire() as conn:
            passages, p_timings, _ = await search_passages(
                conn, deps.models, cfg, question, queries, hits, reuse_chunks
            )
        await st.step("passages", {"passages": [_passage_payload(p) for p in passages], "timings_ms": p_timings})
    missing_titles = {p.arxiv_id for p in passages} - set(titles)
    if missing_titles:  # reused chunks from papers not selected this turn
        rows = await deps.pool.fetch(
            "SELECT arxiv_id, title, extract(year FROM v1_date)::int AS year FROM papers WHERE arxiv_id = ANY($1)",
            list(missing_titles),
        )
        titles.update({r["arxiv_id"]: (r["title"], r["year"]) for r in rows})
    if not passages:
        await st.step("write", {"error": "no relevant papers found (all below rerank_min_score)"})
        return "done"
    await st.check_cancel()

    # READ
    if "read" in st.done and "items" in st.done["read"]:
        notes = [prompts.Note(**n) for n in st.done["read"]["items"]]
    elif st.past_deadline():
        notes = [prompts.Note(claim=p.text, source_id=_passage_id(p)) for p in passages]  # write from raw evidence
        await st.step("read", {"skipped": "timeout", "notes": len(notes), "items": [n.model_dump() for n in notes]})
    else:
        notes = await step_read(st, question, passages, titles)
    await st.check_cancel()

    # WRITE
    by_id = {_passage_id(p): p for p in passages}
    sources = await source_texts(deps.pool, by_id, titles)
    if "write" in st.done and "raw" in st.done["write"]:
        raw = st.done["write"]["raw"]
        drafts = parse_answer(raw, set(by_id))
    else:
        raw, model = await step_write(st, question, notes, context, sources)
        drafts = parse_answer(raw, set(by_id))
        await st.step("write", {"sentences": len(drafts), "untagged_or_invalid": sum(1 for d in drafts if not d.valid),
                                "repaired_tags": sum(len(d.repaired_tags) for d in drafts),
                                "model": model, "fallback": model != cfg.models.write_model, "raw": raw})  # fmt: skip
    await st.check_cancel()

    # VERIFY || CONSENSUS (consensus runs on the draft's tagged sentences)
    draft_text = " ".join(d.text for d in drafts if d.valid)

    async def consensus() -> list[prompts.ConsensusClaim]:
        if not draft_text or st.past_deadline():
            return []
        return await step_consensus(st, draft_text, passages, titles)

    finals, claims = await asyncio.gather(step_verify(st, drafts, sources), consensus())
    await _store(st, finals, by_id, notes, claims)
    return "timed_out" if st.timed_out else "done"


def _selected_payload(hits: list[PaperHit], carry: Sequence[str], pinned: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {"arxiv_id": h.arxiv_id, "title": h.title, "year": h.year, "rerank_score": round(h.rerank_score, 4),
         "final_score": round(h.final_score, 4), "full_text": h.state == "ready", "via_anchor": h.via_anchor,
         "name_match": h.name_match, "carryover": h.arxiv_id in carry, "pinned": h.arxiv_id in pinned,
         "via_chain": h.via_chain, "badges": h.badges}
        for h in hits
    ]  # fmt: skip


async def _save_selected(st: RunState, hits: list[PaperHit], carry: Sequence[str], pinned: Sequence[str]) -> None:
    await st.deps.pool.execute(
        "UPDATE research_runs SET selected_papers = $2 WHERE id = $1",
        st.run_id, json.dumps(_selected_payload(hits, carry, pinned)),
    )  # fmt: skip


def _passage_payload(p: Passage) -> dict[str, Any]:
    return {
        "id": _passage_id(p),
        "arxiv_id": p.arxiv_id,
        "section": p.section,
        "rerank_score": round(p.rerank_score, 4),
    }


async def _refresh_states(pool: asyncpg.Pool, hits: list[PaperHit]) -> None:
    rows = await pool.fetch("SELECT arxiv_id, state FROM papers WHERE arxiv_id = ANY($1)", [h.arxiv_id for h in hits])
    states = {r["arxiv_id"]: r["state"] for r in rows}
    for h in hits:
        h.state = states.get(h.arxiv_id, h.state)


async def load_hits(pool: asyncpg.Pool, items: list[dict[str, Any]]) -> list[PaperHit]:
    """PaperHits for saved selections (resume) or new candidates, in the given order."""
    ids = [i["arxiv_id"] for i in items]
    rows = {
        r["arxiv_id"]: r
        for r in await pool.fetch(
            "SELECT arxiv_id, title, abstract, extract(year FROM v1_date)::int AS year, authors, primary_category,"
            " badges, state, coalesce(score, meta_score, 0) AS prior FROM papers WHERE arxiv_id = ANY($1::text[])",
            ids,
        )
    }
    out = []
    for i in items:
        r = rows.get(i["arxiv_id"])
        if r is None:
            continue
        out.append(PaperHit(
            arxiv_id=r["arxiv_id"], title=r["title"], abstract=r["abstract"], year=r["year"],
            authors=_json(r["authors"]), primary_category=r["primary_category"], badges=_json(r["badges"]),
            state=r["state"], prior=float(r["prior"]), rrf=0.0, rerank_score=float(i.get("rerank_score", 0.0)),
            final_score=float(i.get("final_score", i.get("rerank_score", 0.0))), via_anchor=bool(i.get("via_anchor")),
            name_match=bool(i.get("name_match")), via_chain=i.get("via_chain"),
        ))  # fmt: skip
    return out


async def load_passages(pool: asyncpg.Pool, items: list[dict[str, Any]]) -> list[Passage]:
    """Passages from a saved PASSAGES step (resume)."""
    chunk_ids = [int(i["id"][2:]) for i in items if i["id"].startswith("c:")]
    chunks = {
        r["id"]: r
        for r in await pool.fetch(
            "SELECT id, arxiv_id, section, text, char_start, char_end FROM chunks WHERE id = ANY($1::bigint[])",
            chunk_ids,
        )
    }
    abstracts = {
        r["arxiv_id"]: r["abstract"]
        for r in await pool.fetch(
            "SELECT arxiv_id, abstract FROM papers WHERE arxiv_id = ANY($1)",
            [i["arxiv_id"] for i in items if i["id"].startswith("a:")],
        )
    }
    out = []
    for i in items:
        if i["id"].startswith("c:") and int(i["id"][2:]) in chunks:
            c = chunks[int(i["id"][2:])]
            out.append(Passage(c["arxiv_id"], "chunk", c["id"], c["section"] or "", c["text"], c["char_start"],
                               c["char_end"], 0.0, float(i.get("rerank_score", 0.0))))  # fmt: skip
        elif i["arxiv_id"] in abstracts:
            out.append(Passage(i["arxiv_id"], "abstract", None, "Abstract", abstracts[i["arxiv_id"]], None, None, 0.0,
                               float(i.get("rerank_score", 0.0))))  # fmt: skip
    return out


async def _store(
    st: RunState,
    finals: list[Final],
    by_id: dict[str, Passage],
    notes: list[prompts.Note],
    claims: list[prompts.ConsensusClaim],
) -> None:
    def src(tag: str, f: Final) -> dict[str, Any]:
        p = by_id[tag]
        out = {"id": tag, "arxiv_id": p.arxiv_id, "chunk_id": p.chunk_id, "type": p.source, "section": p.section}
        if f.span and f.span_source == tag:
            out["span"] = f.span
        return out

    async with st.deps.pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM answer_sentences WHERE run_id = $1", st.run_id)
        await conn.executemany(
            "INSERT INTO answer_sentences (run_id, seq, section, text, verdict, sources) VALUES ($1,$2,$3,$4,$5,$6)",
            [(st.run_id, f.seq, f.section, f.text, f.verdict, json.dumps([src(t, f) for t in f.tags])) for f in finals],
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
