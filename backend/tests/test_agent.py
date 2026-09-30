from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from agent import prompts
from agent.bibtex import bibtex
from agent.run import RunState, clean_summary, step_read, step_verify
from agent.text import DraftSentence, number_citations, parse_answer, render_with_numbers, strip_tags
from core.config import AppConfig
from retrieval.search import Passage

ALLOWED = {"c:1", "c:2", "a:2106.09685"}


def test_parse_answer_sections_and_tags() -> None:
    text = """## How LoRA works

LoRA freezes the pretrained weights. [[c:1]] It trains low-rank matrices [[c:2]][[a:2106.09685]].

## Limits
Rank matters. Small ranks can underfit. [[c:2]] This sentence has no tag.
"""
    out = parse_answer(text, ALLOWED)
    assert [(d.section, d.text, d.tags) for d in out] == [
        ("How LoRA works", "LoRA freezes the pretrained weights.", ["c:1"]),
        ("How LoRA works", "It trains low-rank matrices.", ["c:2", "a:2106.09685"]),  # punctuation after tags
        ("Limits", "Rank matters.", []),  # untagged: only the last sentence before a tag group gets it
        ("Limits", "Small ranks can underfit.", ["c:2"]),
        ("Limits", "This sentence has no tag.", []),
    ]
    assert [d.valid for d in out] == [True, True, False, True, False]


def test_parse_answer_strips_unknown_tags() -> None:
    [d] = parse_answer("Claim here. [[c:999]]", ALLOWED)
    assert not d.valid and d.invalid_tags == ["c:999"]
    [d] = parse_answer("Claim here. [[c:999]][[c:1]]", ALLOWED)
    assert d.tags == ["c:1"] and d.invalid_tags == ["c:999"]


def test_numbering_is_stable() -> None:
    first = number_citations({}, ["b", "a", "b"])
    assert first == {"b": 1, "a": 2}
    assert number_citations(first, ["c", "a"]) == {"b": 1, "a": 2, "c": 3}


def test_render_with_numbers() -> None:
    text = "LoRA is efficient. [[c:1]][[a:2106.09685]]"
    out = render_with_numbers(text, {"c:1": "2106.09685", "a:2106.09685": "2106.09685"}, {"2106.09685": 4})
    assert out == "LoRA is efficient. [4]"
    assert strip_tags(text) == "LoRA is efficient."


def test_bibtex() -> None:
    papers = [
        {"arxiv_id": "2106.09685", "title": "LoRA: Low-Rank Adaptation of Large {Language} Models", "year": 2021,
         "authors": [{"first": "Edward", "last": "Hu"}, {"first": "Yelong", "last": "Shen"}],
         "primary_category": "cs.CL"},
        {"arxiv_id": "2106.00001", "title": "LoRA again", "year": 2021, "authors": [{"first": "A", "last": "Hu"}],
         "primary_category": "cs.LG"},
    ]  # fmt: skip
    out = bibtex(papers)
    assert "@misc{hu2021lora," in out and "@misc{hu2021loraa," in out
    assert "author = {Hu, Edward and Shen, Yelong}" in out
    assert "eprint = {2106.09685}" in out and "archivePrefix = {arXiv}" in out and "primaryClass = {cs.CL}" in out
    assert "\\{Language\\}" in out


class FakeLLM:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    async def generate(self, prompt: str, *, model: str, purpose: str, schema: Any = None, **kw: Any) -> Any:
        self.calls.append((purpose, prompt))
        value = self.responses[purpose]
        value = value(prompt) if callable(value) else value
        return SimpleNamespace(parsed=value if schema else None, text=value if isinstance(value, str) else "")


def _state(cfg: AppConfig, llm: FakeLLM) -> RunState:
    st = RunState.__new__(RunState)
    st.deps = SimpleNamespace(cfg=cfg, llm=llm)  # type: ignore[assignment]
    st.run_id = None  # type: ignore[assignment]
    st.deadline = time.monotonic() + 60
    st.timed_out = False
    st.seq = 0
    st.steps = []  # type: ignore[attr-defined]

    async def step(type_: str, payload: dict[str, Any]) -> None:
        st.steps.append((type_, payload))  # type: ignore[attr-defined]

    st.step = step  # type: ignore[method-assign]
    return st


async def test_verify_applies_verdicts(cfg: AppConfig) -> None:
    drafts = [
        DraftSentence("S", "A is true.", ["c:1"]),
        DraftSentence("S", "B is huge.", ["c:2"]),
        DraftSentence("S", "C is false.", ["c:1"]),
        DraftSentence("S", "No tag here."),
        DraftSentence("S", "Bad tag.", [], ["c:999"]),
        DraftSentence("S", "D.", ["c:2"]),
    ]
    verdicts = prompts.VerifyOut(results=[
        prompts.Verdict(index=0, verdict="supported"),
        prompts.Verdict(index=1, verdict="partial", rewrite="B is large. [[c:2]][[c:999]]"),
        prompts.Verdict(index=2, verdict="unsupported"),
        # index 3 (sentence "D.") has no verdict -> removed
    ])  # fmt: skip
    llm = FakeLLM({"verify": verdicts})
    st = _state(cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"verify_batch_size": 5})}), llm)
    finals = await step_verify(st, drafts, {"c:1": "source one", "c:2": "source two"})
    got = [(f.seq, f.verdict, f.text) for f in finals]
    assert got == [
        (0, "supported", "A is true. [[c:1]]"),
        (1, "partial_rewritten", "B is large. [[c:2]]"),  # rewrite keeps only the original tags
        (2, "unsupported_removed", "C is false. [[c:1]]"),
        (3, "unsupported_removed", "No tag here."),
        (4, "unsupported_removed", "Bad tag."),
        (5, "unsupported_removed", "D. [[c:2]]"),
    ]
    assert len(llm.calls) == 1  # untagged sentences are not sent to the verifier
    assert "source two" in llm.calls[0][1]


async def test_read_drops_notes_with_unknown_sources(cfg: AppConfig) -> None:
    notes = prompts.NotesOut(notes=[
        prompts.Note(claim="x", source_id="c:1"), prompts.Note(claim="y", source_id="c:42"),
        prompts.Note(claim="z", source_id=" a:2106.09685 "),
    ])  # fmt: skip
    st = _state(cfg, FakeLLM({"read": notes}))
    passages = [
        Passage("2106.09685", "chunk", 1, "Intro", "text", 0, 4, 0.0, 0.9),
        Passage("2106.09685", "abstract", None, "Abstract", "abs", None, None, 0.0, 0.8),
    ]
    out = await step_read(st, "q", passages, {"2106.09685": ("LoRA", 2021)})
    assert [(n.claim, n.source_id) for n in out] == [("x", "c:1"), ("z", "a:2106.09685")]
    assert st.steps[-1] == ("read", {"notes": 2, "dropped_invalid_source": 1})  # type: ignore[attr-defined]


@pytest.mark.parametrize("n", [3])
def test_rewrite_prompt_mentions_limit(n: int) -> None:
    assert f"1 to {n} short search queries" in prompts.rewrite_prompt("q", "", n)


def test_clean_summary() -> None:
    assert clean_summary("**Updated Summary:**\n\nThe thread covers LoRA [1].") == "The thread covers LoRA [1]."
    assert clean_summary("Summary: LoRA [1].") == "Summary: LoRA [1]."  # content on the same line is kept
