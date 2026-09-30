"""Sentence-aligned chunking (spec 5 `chunking`, 6.8).

Every chunk's text is an exact slice `document_text[char_start:char_end]` of one canonical
text built from the parsed paper, so the reader can highlight a passage by offsets.
Token counts use the embedding model's tokenizer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from core.config import ChunkingConfig
from ingest.tei import ParsedDoc

PARAGRAPH_SEP = "\n\n"
SENTENCE_SEP = " "

# Counts tokens for a batch of texts (no special tokens).
TokenCounter = Callable[[Sequence[str]], list[int]]


@dataclass(frozen=True)
class Span:
    section: str
    start: int
    end: int
    tokens: int


@dataclass(frozen=True)
class Chunk:
    section: str
    position: int
    text: str
    token_count: int
    char_start: int
    char_end: int


def document_text(doc: ParsedDoc) -> tuple[str, list[tuple[str, int, int]]]:
    """Canonical text (headings + paragraphs) and the (section, start, end) span of every sentence."""
    text, spans, _ = document_layout(doc)
    return text, spans


def document_layout(doc: ParsedDoc) -> tuple[str, list[tuple[str, int, int]], list[tuple[str, int, int]]]:
    """Like document_text, plus the (heading, start, end) span of every section heading (for the reader)."""
    parts: list[str] = []
    spans: list[tuple[str, int, int]] = []
    heads: list[tuple[str, int, int]] = []
    pos = 0

    def add(s: str) -> int:
        nonlocal pos
        parts.append(s)
        start = pos
        pos += len(s)
        return start

    for section in doc.sections:
        if parts:
            add(PARAGRAPH_SEP)
        head_start = add(section.head)
        heads.append((section.head, head_start, head_start + len(section.head)))
        for para in section.paragraphs:
            add(PARAGRAPH_SEP)
            for i, sentence in enumerate(para):
                if i:
                    add(SENTENCE_SEP)
                start = add(sentence)
                spans.append((section.head, start, start + len(sentence)))
    return "".join(parts), spans, heads


def _split_long(text: str, span: tuple[str, int, int], tokens: int, max_tokens: int) -> list[tuple[str, int, int, int]]:
    """Split one over-long sentence into word-aligned pieces of roughly equal token counts."""
    section, start, end = span
    words = text[start:end].split(" ")
    n_pieces = -(-tokens // max_tokens) + 1  # ceil, plus slack for uneven word lengths
    per = max(1, -(-len(words) // n_pieces))
    out, offset = [], start
    for i in range(0, len(words), per):
        piece = " ".join(words[i : i + per])
        out.append((section, offset, offset + len(piece), -(-tokens * len(piece) // max(1, end - start))))
        offset += len(piece) + 1
    return out


def chunk_document(doc: ParsedDoc, count_tokens: TokenCounter, cfg: ChunkingConfig) -> tuple[str, list[Chunk]]:
    text, raw_spans = document_text(doc)
    if not raw_spans:
        return text, []
    counts = count_tokens([text[s:e] for _, s, e in raw_spans])
    spans: list[Span] = []
    for (section, s, e), n in zip(raw_spans, counts, strict=True):
        if n > cfg.max_tokens:
            spans += [Span(sec, a, b, t) for sec, a, b, t in _split_long(text, (section, s, e), n, cfg.max_tokens)]
        else:
            spans.append(Span(section, s, e, n))

    groups: list[list[Span]] = []
    current: list[Span] = []
    tokens = 0
    fresh = 0  # spans in `current` that are not overlap carried from the previous chunk
    for span in spans:
        section_change = bool(current) and span.section != current[-1].section
        too_big = tokens + span.tokens > cfg.target_tokens and tokens >= cfg.min_tokens
        over_max = tokens + span.tokens > cfg.max_tokens
        if fresh and (too_big or over_max or (section_change and tokens >= cfg.min_tokens)):
            groups.append(current)
            current = [] if section_change else _overlap(current, cfg.overlap_tokens)
            tokens = sum(s.tokens for s in current)
            fresh = 0
            if tokens + span.tokens > cfg.max_tokens:
                current, tokens = [], 0
        elif not fresh and over_max:
            current, tokens = [], 0  # drop overlap that cannot fit next to this span
        current.append(span)
        tokens += span.tokens
        fresh += 1
    if fresh:
        new = current[len(current) - fresh :]
        new_tokens = sum(s.tokens for s in new)
        if groups and new_tokens < cfg.min_tokens and sum(s.tokens for s in groups[-1]) + new_tokens <= cfg.max_tokens:
            groups[-1] += new
        else:
            groups.append(current)

    chunks = [
        Chunk(
            section=g[0].section,
            position=i,
            text=text[g[0].start : g[-1].end],
            token_count=sum(s.tokens for s in g),
            char_start=g[0].start,
            char_end=g[-1].end,
        )
        for i, g in enumerate(groups)
    ]
    return text, chunks


def _overlap(group: list[Span], overlap_tokens: int) -> list[Span]:
    """Trailing sentences of the previous chunk, up to overlap_tokens, never the whole chunk."""
    out: list[Span] = []
    total = 0
    for span in reversed(group[1:]):
        if total + span.tokens > overlap_tokens:
            break
        out.insert(0, span)
        total += span.tokens
    return out


def embedding_input(title: str, chunk: Chunk) -> str:
    """Spec 6.8: chunks are embedded with a header; the stored text has none."""
    return f"Title: {title} | Section: {chunk.section}\n{chunk.text}"
