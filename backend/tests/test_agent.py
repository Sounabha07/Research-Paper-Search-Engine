from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from agent import prompts
from agent.bibtex import bibtex
from agent.prompts import Note
from agent.run import RunState, author_names, clean_summary, format_notes, source_status, step_read, step_verify
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
    recheck = prompts.VerifyOut(results=[
        prompts.Verdict(index=0, verdict="supported", span="fine-tunes a 65B model on a single 48GB GPU")
    ])  # fmt: skip
    llm = FakeLLM(
        {"verify": lambda p: recheck if "Sentence 0: QLoRA fits a 65B model on a single 48GB" in p else verdicts}
    )
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
    assert len(llm.calls) == 2  # one batch + the re-check of the rewrite; untagged sentences are not sent
    assert "single 48GB GPU" in llm.calls[0][1]


async def test_rewrite_that_fails_recheck_is_removed(cfg: AppConfig) -> None:
    drafts = [DraftSentence("S", "QLoRA fits a 65B model on a laptop GPU.", ["c:2"])]
    first = prompts.VerifyOut(results=[prompts.Verdict(
        index=0, verdict="partial", span="fine-tunes a 65B model on a single 48GB GPU",
        rewrite="QLoRA fits a 65B model on a single 48GB GPU in one hour.",
    )])  # fmt: skip
    second = prompts.VerifyOut(results=[prompts.Verdict(index=0, verdict="partial", rewrite="x")])
    llm = FakeLLM({"verify": lambda p: second if "in one hour" in p else first})
    finals = await step_verify(_state(cfg, llm), drafts, SOURCES)
    assert [(f.verdict, f.reason) for f in finals] == [("unsupported_removed", "rewrite not supported")]


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


def test_source_status() -> None:
    def row(
        landmark: bool = False, foundational: bool = False, venue: str | None = None, jref: str | None = None
    ) -> dict[str, Any]:
        return {"is_landmark": landmark, "is_foundational": foundational, "venue_accepted": venue is not None,
                "venue_name": venue, "journal_ref": jref}  # fmt: skip

    assert source_status(row(), 2022) == "preprint (2022)"
    assert source_status(row(venue="NeurIPS 2023"), 2023) == "peer-reviewed: NeurIPS 2023"
    assert source_status(row(jref="Ann. of Math. 193 (2021)"), 2020) == "peer-reviewed: Ann. of Math. 193 (2021)"
    assert source_status(row(landmark=True, venue="NeurIPS"), 2017) == "landmark, peer-reviewed: NeurIPS"
    # landmark and foundational papers are never preprints, even with no venue in the metadata (QLoRA: none)
    assert source_status(row(landmark=True), 2023) == "landmark"
    assert source_status(row(foundational=True), 2015) == "foundational"


def test_spurious_preprint_attribution_is_not_kept(cfg: AppConfig) -> None:
    from agent.run import SPURIOUS_ATTRIBUTION, _apply_verdict

    sources = {"c:1": SourceText("QLoRA", "QLoRA is the first method that enables finetuning 33B models on one GPU.",
                                 "landmark", "Dettmers")}  # fmt: skip
    d = DraftSentence("S", "QLoRA is the first method that enables finetuning 33B models on one GPU.", ["c:1"])
    v = prompts.Verdict(index=0, verdict="partial", span="QLoRA is the first method that enables finetuning 33B",
                        rewrite="A 2024 preprint by Dettmers claims QLoRA is the first method to do so.")  # fmt: skip
    f = _apply_verdict(0, d, v, sources, True, True)
    assert f.text == d.with_tags() and f.reason == SPURIOUS_ATTRIBUTION  # original kept for the re-check
    preprint = {"c:1": SourceText("X", sources["c:1"].text, "preprint (2024)", "Dettmers")}
    assert _apply_verdict(0, d, v, preprint, True, True).reason is None  # a real preprint: rewrite kept


def test_format_notes_carry_status_and_author() -> None:
    info = {"a:2212.13609": SourceText("The Sunflower Conjecture Proven", "...", "preprint (2022)", "Fukuyama")}
    notes = [Note(claim="The conjecture holds.", source_id="a:2212.13609"), Note(claim="x", source_id="c:9")]
    text = format_notes(notes, info)
    assert (
        '- [[a:2212.13609]] (Fukuyama; preprint (2022); "The Sunflower Conjecture Proven") The conjecture holds.'
        in text
    )
    assert "- [[c:9]] x" in text


def test_author_names_and_attribution_pass_name_check() -> None:
    assert author_names('[{"first": "J", "last": "McDonald"}, {"first": "A", "last": "Lee"}]') == "McDonald, Lee"
    sources = {"c:1": SourceText("Some Paper", "The method is robust to paraphrasing.", "preprint (2023)", "McDonald")}
    assert missing_names("McDonald et al. claim the method is robust to paraphrasing.", sources, ["c:1"]) == []


def test_venue_from_status_passes_name_check() -> None:
    sources = {"c:1": SourceText("Some Paper", "The method is fast.", "peer-reviewed: NeurIPS 2025", "Hu")}
    assert missing_names("A NeurIPS 2025 paper by Hu reports the method is fast.", sources, ["c:1"]) == []


def test_rewrite_drops_echoed_single_bracket_tags() -> None:
    from agent.run import _rewrite

    d = DraftSentence("S", "x", ["a:2508.20407"])
    assert (
        _rewrite("A preprint claims y. [a:2508.20407] [[a:2508.20407]]", d) == "A preprint claims y. [[a:2508.20407]]"
    )


def test_altered_math_in_rewrite() -> None:
    from agent.verify import altered_math

    sources = {"a:1": SourceText("T", "a family includes a $k$-sunflower if $|F| > ( c k^4 )^{m}$")}
    original = r"A family of $O(w^3 \log k)^k$ sets contains a sunflower."
    assert altered_math(r"A preprint claims a family of $O(w^3 \log k)^k$ sets does.", original, sources, ["a:1"]) == []
    assert altered_math("It needs $|F| > (ck^4)^m$.", original, sources, ["a:1"]) == []  # in the source
    assert altered_math("A family of $O(w^3 \",'log k)^k$ sets.", original, sources, ["a:1"]) == [
        "$O(w^3 \",'log k)^k$"
    ]


def test_span_ignores_math_delimiters() -> None:
    sources = {"a:1": SourceText("T", r"approximates the term $\exp(q_{ic}k_{jc})$ with a Taylor polynomial.")}
    assert find_span(r"approximates the term \exp(q_{ic}k_{jc}) with a Taylor polynomial", sources, ["a:1"]) == "a:1"


def test_mostly_untagged() -> None:
    from agent.run import mostly_untagged

    allowed = {"c:1", "c:2"}
    assert mostly_untagged("One claim. Two claims. Three [[c:1]].", allowed)
    assert not mostly_untagged("One [[c:1]]. Two [[c:2]]. Three.", allowed)
    assert mostly_untagged("", allowed)


async def test_spurious_attribution_original_passes_recheck(cfg: AppConfig) -> None:
    sources = {"c:1": SourceText("QLoRA", "QLoRA reduces memory enough to finetune a 65B model on a single 48GB GPU.",
                                 "landmark", "Dettmers")}  # fmt: skip
    d = DraftSentence("S", "QLoRA finetunes a 65B model on a single 48GB GPU.", ["c:1"])
    first = prompts.VerifyOut(results=[prompts.Verdict(
        index=0, verdict="partial", span="finetune a 65B model on a single 48GB GPU",
        rewrite="A 2024 preprint by Dettmers reports that QLoRA finetunes a 65B model on a single 48GB GPU.",
    )])  # fmt: skip
    ok = prompts.VerifyOut(
        results=[prompts.Verdict(index=0, verdict="supported", span="finetune a 65B model on a single 48GB GPU")]
    )
    calls: list[str] = []

    def respond(prompt: str) -> prompts.VerifyOut:
        calls.append(prompt)
        return first if len(calls) == 1 else ok

    finals = await step_verify(_state(cfg, FakeLLM({"verify": respond})), [d], sources)
    assert [(f.verdict, f.text, f.reason) for f in finals] == [("supported", d.with_tags(), None)]
    assert "NOT a preprint" in calls[0]
