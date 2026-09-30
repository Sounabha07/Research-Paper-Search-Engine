"""Prompts and structured-output schemas for the answer pipeline (spec 12, 12b)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---- schemas ---------------------------------------------------------------------------------


class RewriteOut(BaseModel):
    standalone_question: str
    queries: list[str] = Field(min_length=1)


class Note(BaseModel):
    claim: str
    source_id: str


class NotesOut(BaseModel):
    notes: list[Note]


class Verdict(BaseModel):
    index: int
    verdict: Literal["supported", "partial", "unsupported"]
    rewrite: str | None = None


class VerifyOut(BaseModel):
    results: list[Verdict]


class Stance(BaseModel):
    source_id: str
    stance: Literal["supports", "contradicts", "unrelated"]


class ConsensusClaim(BaseModel):
    claim: str
    items: list[Stance]


class ConsensusOut(BaseModel):
    claims: list[ConsensusClaim]


# ---- builders -------------------------------------------------------------------------------


def rewrite_prompt(question: str, context: str, max_queries: int) -> str:
    return f"""You prepare literature searches over arXiv computer science and mathematics papers.

{context or "(This is the first question in the conversation.)"}

New question: {question}

Return JSON with:
- "standalone_question": the new question rewritten to be fully self-contained. Resolve pronouns and references
  ("it", "that method", "the second paper") using the conversation. If it is already self-contained, repeat it.
- "queries": 1 to {max_queries} short search queries (3 to 8 words, keyword style, no question words), each
  covering a distinct aspect. Use exactly one query when the question has a single topic."""


def read_prompt(question: str, passages: str, max_notes: int) -> str:
    return f"""Extract evidence notes for answering a research question from the passages below.

Question: {question}

Passages (each starts with its source id in brackets):
{passages}

Return JSON {{"notes": [{{"claim": ..., "source_id": ...}}]}} with at most {max_notes} notes.
Be thorough: take notes from every passage that bears on the question, usually several notes per relevant
passage (methods, mechanisms, numbers, comparisons, limitations). A good answer needs 15 or more notes when
the passages are relevant.
Rules:
- Each claim is one atomic, specific statement that the passage itself states (not background knowledge).
- source_id is the passage's id exactly as shown, e.g. "c:1234" or "a:2106.09685".
- Prefer claims that help answer the question; skip passages that are irrelevant.
- Keep numbers, names of methods and datasets exact."""


def write_prompt(question: str, notes: str, context: str) -> str:
    return f"""Write an answer to the question using ONLY the evidence notes below.

{context}

Question: {question}

Evidence notes (source tag, then claim):
{notes}

Format and rules:
- Short sections, each starting with a markdown heading line "## <heading>" (1 to 4 sections), 150 to 400 words.
- EVERY sentence ends with one or more source tags copied exactly from the notes, placed after the sentence's
  final punctuation, for example: "LoRA freezes the pretrained weights. [[c:1234]]" or
  "Both methods reduce memory. [[a:2106.09685]][[c:88]]".
- Only state what the cited notes support. Do not use outside knowledge. Leave out anything the notes do not cover.
- No sentence without a tag: no introductions, transitions, or conclusions that lack evidence.
- Refer to papers by method or author names from the notes, not by the tags."""


def verify_prompt(items: str) -> str:
    return f"""Check each sentence of a research answer against the sources it cites.

{items}

For each sentence return {{"index": ..., "verdict": ..., "rewrite": ...}}:
- "supported": every claim in the sentence is stated or directly implied by its cited sources. rewrite = null.
- "partial": part of the sentence is supported but part is not, or it overstates the sources. rewrite = the
  sentence rewritten to say only what the sources support, ending with the same source tags.
- "unsupported": the sources do not support the sentence's main claim. rewrite = null.
Judge only against the given source texts, not your own knowledge. Return JSON {{"results": [...]}}."""


def consensus_prompt(answer: str, passages: str, n_claims: int) -> str:
    return f"""Here is an answer to a research question and the passages it was based on.

Answer:
{answer}

Passages (each starts with its source id in brackets):
{passages}

Pick up to {n_claims} central claims of the answer. For each claim, classify EVERY passage as "supports",
"contradicts", or "unrelated" to that claim. Return JSON
{{"claims": [{{"claim": ..., "items": [{{"source_id": ..., "stance": ...}}]}}]}}."""


def summary_prompt(previous: str, turns: str) -> str:
    return f"""Update the running summary of a research conversation.

Previous summary:
{previous or "(none yet)"}

Turns to fold into the summary:
{turns}

Write the updated summary in at most 300 words. Keep the paper numbers in square brackets (e.g. [3]) exactly as
they appear, and keep the key questions asked and conclusions reached. Plain text, no headings."""
