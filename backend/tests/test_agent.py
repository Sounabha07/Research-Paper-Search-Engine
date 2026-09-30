from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from agent import prompts
from agent.bibtex import bibtex
from agent.run import RunState, clean_summary, step_read, step_verify
from agent.text import DraftSentence, number_citations, parse_answer, render_with_numbers, strip_tags
from agent.verify import SourceText, find_span, missing_names, names_in
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


SOURCES = {
    "c:1": SourceText("Paper One", "Alpha method reaches 90% accuracy on the benchmark with little compute."),
    "c:2": SourceText("QLoRA: Efficient Finetuning", "QLoRA fine-tunes a 65B model on a single 48GB GPU."),
}


async def test_verify_applies_verdicts_and_spans(cfg: AppConfig) -> None:
    drafts = [
        DraftSentence("S", "Alpha reaches 90% accuracy.", ["c:1"]),
        DraftSentence("S", "QLoRA fits a 65B model on a small GPU.", ["c:2"]),
        DraftSentence("S", "Alpha is the best method ever.", ["c:1"]),
        DraftSentence("S", "No tag here."),
        DraftSentence("S", "Bad tag.", [], ["c:999"]),
        DraftSentence("S", "D.", ["c:2"]),
        DraftSentence("S", "Alpha reaches high accuracy.", ["c:1"]),
        DraftSentence("S", "TLinFormer fine-tunes a 65B model on one GPU.", ["c:2"]),
    ]
    verdicts = prompts.VerifyOut(results=[
        prompts.Verdict(index=0, verdict="supported", span="Alpha method   reaches 90% accuracy"),  # whitespace ok
        prompts.Verdict(index=1, verdict="partial", span="fine-tunes a 65B model on a single 48GB GPU",
                        rewrite="QLoRA fits a 65B model on a single 48GB GPU. [[c:2]][[c:999]]"),
        prompts.Verdict(index=2, verdict="unsupported"),
        # index 3 (sentence "D.") has no verdict -> removed
        prompts.Verdict(index=4, verdict="supported", span="Alpha is the best method"),  # not in the source
        prompts.Verdict(index=5, verdict="supported", span="QLoRA fine-tunes a 65B model"),  # names not in source
    ])  # fmt: skip
    llm = FakeLLM({"verify": verdicts})
    st = _state(cfg.model_copy(update={"agent": cfg.agent.model_copy(update={"verify_batch_size": 10})}), llm)
    finals = await step_verify(st, drafts, SOURCES)
    got = [(f.seq, f.verdict, f.text, f.reason) for f in finals]
    assert got == [
        (0, "supported", "Alpha reaches 90% accuracy. [[c:1]]", None),
        (1, "partial_rewritten", "QLoRA fits a 65B model on a single 48GB GPU. [[c:2]]", None),  # keeps original tags
        (2, "unsupported_removed", "Alpha is the best method ever. [[c:1]]", "unsupported"),
        (3, "unsupported_removed", "No tag here.", "no source tag"),
        (4, "unsupported_removed", "Bad tag.", "invalid source tag"),
        (5, "unsupported_removed", "D. [[c:2]]", "no verdict"),
        (6, "unsupported_removed", "Alpha reaches high accuracy. [[c:1]]", "no valid span"),
        (7, "unsupported_removed", "TLinFormer fine-tunes a 65B model on one GPU. [[c:2]]",
         "names not in source: TLinFormer"),
    ]  # fmt: skip
    assert finals[0].span_source == "c:1" and finals[1].span_source == "c:2"
    assert len(llm.calls) == 1  # untagged sentences are not sent to the verifier
    assert "single 48GB GPU" in llm.calls[0][1]


def test_span_and_name_checks() -> None:
    assert find_span("Alpha method reaches 90% accuracy.", SOURCES, ["c:1"]) == "c:1"
    assert find_span("reaches 90%", SOURCES, ["c:1"]) is None  # too short to mean anything
    assert find_span("Alpha method reaches 90% accuracy", SOURCES, ["c:2"]) is None  # not the cited source
    assert find_span("“Alpha method reaches 90% accuracy”"[1:-1], SOURCES, ["c:1"]) == "c:1"
    assert names_in("LoRA and QLoRA beat GPT-2 and BERT-large on the Transformer") == [
        "LoRA",
        "QLoRA",
        "GPT-2",
        "BERT-large",
    ]
    assert missing_names("QLoRA uses a 48GB GPU", SOURCES, ["c:2"]) == []  # "GPU" next to digits still matches
    assert missing_names("LoRA uses a 48GB GPU", SOURCES, ["c:2"]) == ["LoRA"]  # QLoRA is not LoRA


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
    kind, payload = st.steps[-1]  # type: ignore[attr-defined]
    assert kind == "read" and payload["notes"] == 2 and payload["dropped_invalid_source"] == 1
    assert payload["items"] == [{"claim": "x", "source_id": "c:1"}, {"claim": "z", "source_id": "a:2106.09685"}]


@pytest.mark.parametrize("n", [3])
def test_rewrite_prompt_mentions_limit(n: int) -> None:
    assert f"1 to {n} short search queries" in prompts.rewrite_prompt("q", "", n)


def test_clean_summary() -> None:
    assert clean_summary("**Updated Summary:**\n\nThe thread covers LoRA [1].") == "The thread covers LoRA [1]."
    assert clean_summary("Summary: LoRA [1].") == "Summary: LoRA [1]."  # content on the same line is kept
