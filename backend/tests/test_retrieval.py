from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import search as search_router
from core.config import AgentConfig, AppConfig
from retrieval.search import (
    Filters,
    PaperHit,
    Passage,
    apply_prior,
    keep_pinned,
    merge_query_lists,
    name_matches,
    rrf_fuse,
    select_evidence,
    title_name,
)


def test_rrf_fuse_and_merge() -> None:
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"]], k=60)
    assert [d for d, _ in fused][:2] == ["b", "a"]
    assert dict(fused)["b"] == pytest.approx(1 / 62 + 1 / 61)
    merged = merge_query_lists([[("a", 0.02), ("b", 0.01)], [("b", 0.015), ("c", 0.012)]])
    assert [d for d, _ in merged] == ["b", "a", "c"]
    assert dict(merged)["b"] == pytest.approx(0.025)


def _hit(arxiv_id: str, rerank: float, prior: float) -> PaperHit:
    return PaperHit(arxiv_id, "t", "a", 2024, [], "cs.LG", {}, "signals_done", prior, 0.0, rerank_score=rerank)


def test_apply_prior_is_capped_and_filters() -> None:
    hits = [_hit("a", 0.50, 1.0), _hit("b", 0.52, 0.0), _hit("c", 0.10, 5.0), _hit("d", 0.51, -1.0)]
    out = apply_prior(hits, prior_boost_max=0.05, min_score=0.2)
    assert [h.arxiv_id for h in out] == ["a", "b", "d"]  # c dropped below min score
    assert out[0].final_score == pytest.approx(0.55)  # prior 1.0 -> +0.05
    assert out[2].final_score == pytest.approx(0.51)  # negative prior clamped to 0
    assert apply_prior([_hit("c", 0.1, 5.0)], 0.05, None)[0].final_score == pytest.approx(0.15)  # capped at 1


def _p(arxiv_id: str, score: float) -> Passage:
    return Passage(arxiv_id, "chunk", 1, "S", "x", 0, 1, 0.0, score)


def _agent(cfg: AppConfig, **kw: object) -> AgentConfig:
    base = {"max_passages_per_paper": 2, "passages_for_answer": 6, "passage_min_score": 0.5,
            "passage_detail_min_score": 0.1, "passage_detail_max": 2, "min_passages_kept": 3}  # fmt: skip
    return cfg.agent.model_copy(update={**base, **kw})


def _ids(ps: list[Passage]) -> list[tuple[str, float]]:
    return [(p.arxiv_id, p.rerank_score) for p in ps]


def test_select_evidence_relevant_then_detail(cfg: AppConfig) -> None:
    ps = [_p("a", 0.9), _p("a", 0.8), _p("a", 0.7), _p("b", 0.6), _p("b", 0.3), _p("c", 0.4), _p("d", 0.05)]
    counts: dict[str, int] = {}
    out = select_evidence(ps, _agent(cfg), counts)
    # relevant (>= 0.5, 2 per paper): a .9, a .8, b .6; detail only from a/b with room: b .3; c has no relevant one
    assert _ids(out) == [("a", 0.9), ("a", 0.8), ("b", 0.6), ("b", 0.3)]
    assert counts == {"passages.relevant": 3, "passages.detail": 1, "passages.filled": 0, "passages.dropped": 3}


def test_select_evidence_caps(cfg: AppConfig) -> None:
    ps = [_p(x, 0.9 - i * 0.01) for i, x in enumerate("aaabbbcccddd")]
    out = select_evidence(ps, _agent(cfg, passages_for_answer=5, passage_detail_max=5))
    assert len(out) == 5 and max(sum(p.arxiv_id == x for p in out) for x in "abcd") <= 2


def test_select_evidence_fills_to_minimum(cfg: AppConfig) -> None:
    ps = [_p("a", 0.3), _p("b", 0.2), _p("c", 0.01), _p("d", 0.001)]
    counts: dict[str, int] = {}
    out = select_evidence(ps, _agent(cfg), counts)  # nothing reaches 0.5: the best 3 are kept anyway
    assert _ids(out) == [("a", 0.3), ("b", 0.2), ("c", 0.01)]
    assert counts["passages.relevant"] == 0 and counts["passages.filled"] == 3


def test_bm25_filters_sql() -> None:
    cond, params = Filters().bm25_sql(3)
    assert cond == "p.state <> 'discovered'" and params == []
    cond, params = Filters(year_from=2022, year_to=2024, categories=("cs.LG", "math"), restrict_ids=("x",)).bm25_sql(3)
    assert "p.v1_date >= $3::timestamptz" in cond and "p.v1_date < $4::timestamptz" in cond
    assert "p.categories @@@ pdb.term_set($5::text[])" in cond and "p.archives @@@ pdb.term_set($5::text[])" in cond
    assert "p.arxiv_id = ANY($6::text[])" in cond
    assert params == [datetime(2022, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC), ["cs.LG", "math"], ["x"]]


def test_filters_sql() -> None:
    cond, params = Filters().sql(3)
    assert cond == "p.state <> 'discovered'" and params == []
    cond, params = Filters(year_from=2022, year_to=2024, categories=("cs.LG", "math")).sql(3)
    assert "make_date($3::int, 1, 1)" in cond and "make_date($4::int + 1, 1, 1)" in cond
    assert "ANY($5::text[])" in cond and "split_part(c, '.', 1)" in cond
    assert params == [2022, 2024, ["cs.LG", "math"]]


def test_search_endpoint_needs_models() -> None:
    app = FastAPI()
    app.include_router(search_router.router)
    r = TestClient(app).get("/search", params={"q": "lora"})
    assert r.status_code == 503
    assert TestClient(app).get("/search", params={"q": "x"}).status_code == 422  # min length 2


def test_min_kept_overrides_floor() -> None:
    hits = [
        _hit("a", 0.03, 0.0),
        _hit("b", 0.02, 0.0),
        _hit("c", 0.01, 0.0),
        _hit("d", 0.004, 0.0),
        _hit("e", 0.5, 0.0),
    ]
    out = apply_prior(hits, prior_boost_max=0.05, min_score=0.05, min_kept=3)
    assert [h.arxiv_id for h in out] == ["e", "a", "b"]  # e passes the floor; a, b kept as top 3


def test_name_match() -> None:
    assert title_name("QLoRA: Efficient Finetuning of Quantized LLMs") == "QLoRA"
    assert title_name("Attention Is All You Need") is None
    assert name_matches("QLoRA: Efficient Finetuning", ["How does QLoRA extend LoRA?"])
    assert not name_matches("LoRA: Low-Rank Adaptation", ["qlora and dora"])  # whole terms only
    assert name_matches("LoRA: Low-Rank Adaptation", ["How does QLoRA extend LoRA?"])
    assert name_matches("Speculative Decoding: Exploiting Speculative Execution", ["what is speculative decoding"])
    assert not name_matches(
        "FlashAttention-2: Faster Attention", ["flashattention"]
    )  # FlashAttention-2 != FlashAttention


def test_keep_pinned() -> None:
    hits = [_hit("a", 0.9, 0.0), _hit("b", 0.8, 0.0), _hit("c", 0.7, 0.0), _hit("p", 0.01, 0.0)]
    for h in hits:
        h.final_score = h.rerank_score
    selected = [h for h in hits if h.rerank_score >= 0.05]  # the pinned paper is below the floor
    out = keep_pinned(hits, selected, ["p"], limit=3)
    assert [h.arxiv_id for h in out] == ["a", "b", "p"]  # pinned kept, search fills the other 2 slots


def test_restrict_filter() -> None:
    cond, params = Filters(categories=("cs.LG",), restrict_ids=("2106.09685", "2305.14314")).sql(3)
    assert "p.arxiv_id = ANY($4::text[])" in cond
    assert params == [["cs.LG"], ["2106.09685", "2305.14314"]]


def test_strip_generic_and_keyword_gate() -> None:
    from retrieval.search import gate_keyword_hits, strip_generic

    assert strip_generic("How does grouped-query attention reduce the memory cost of decoding?") == (
        "How does grouped-query attention the memory of decoding"
    )  # stopwords like "how" are dropped later by websearch_to_tsquery
    assert strip_generic("new results") == "new results"  # nothing left: keep the query as it was
    assert gate_keyword_hits(["a", "b", "c"], ["c", "x", "a"]) == ["a", "c"]
