"""PyMuPDF fallback parser (spec 6.6): text with font-size heading heuristics.

Used only after GROBID fails twice. References are split from the "References" section
as raw strings; only their arXiv ids and years are extracted (no titles).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pymupdf

from core.arxiv import find_ids
from ingest.tei import ParsedDoc, Reference, Section, split_sentences

HEADING_SIZE_RATIO = 1.15
MAX_HEADING_CHARS = 120
_REFS_HEAD = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography)\s*$", re.IGNORECASE)
_ABSTRACT_HEAD = re.compile(r"^\s*abstract[.:]?\s*$", re.IGNORECASE)
_NUMBERED_HEAD = re.compile(r"^(\d+(\.\d+)*|[IVX]+|[A-Z])\.?\s+[A-Z]")
_REF_START = re.compile(r"^\s*(\[\d+\]|\d+\.)\s+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_PAGE_NUMBER = re.compile(r"^\s*\d{1,4}\s*$")


def _block_text(block: dict[str, Any]) -> tuple[str, float, bool]:
    lines, sizes, bold = [], Counter[float](), True
    for line in block.get("lines", []):
        spans = line.get("spans", [])
        lines.append("".join(s["text"] for s in spans))
        for s in spans:
            if s["text"].strip():
                sizes[round(s["size"], 1)] += len(s["text"])
                bold = bold and bool(s["flags"] & 16)
    text = re.sub(r"-\n(?=[a-z])", "", "\n".join(lines))
    size = sizes.most_common(1)[0][0] if sizes else 0.0
    return " ".join(text.split()), size, bold and bool(sizes)


def _is_heading(text: str, size: float, bold: bool, body_size: float) -> bool:
    if not text or len(text) > MAX_HEADING_CHARS or text.endswith((".", ",", ";")):
        return False
    if size >= body_size * HEADING_SIZE_RATIO:
        return True
    return bold and bool(_NUMBERED_HEAD.match(text) or _REFS_HEAD.match(text) or _ABSTRACT_HEAD.match(text))


def parse_pdf(pdf: bytes) -> ParsedDoc:
    blocks: list[tuple[str, float, bool]] = []
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") == 0:
                    text, size, bold = _block_text(block)
                    if text and not _PAGE_NUMBER.match(text):
                        blocks.append((text, size, bold))
    if not blocks:
        raise ValueError("no extractable text (scanned PDF?)")

    size_chars = Counter[float]()
    for text, size, _ in blocks:
        size_chars[size] += len(text)
    body_size = size_chars.most_common(1)[0][0]

    parsed = ParsedDoc(title=max(blocks[:15], key=lambda b: b[1])[0][:300])
    current = Section("Body", [])
    in_refs = False
    refs: list[str] = []
    for text, size, bold in blocks:
        if _is_heading(text, size, bold, body_size):
            if current.paragraphs:
                parsed.sections.append(current)
            in_refs = bool(_REFS_HEAD.match(text))
            current = Section("Abstract" if _ABSTRACT_HEAD.match(text) else text, [])
            continue
        if in_refs:
            if _REF_START.match(text) or not refs:
                refs.append(text)
            else:
                refs[-1] += " " + text
        elif size >= body_size * 0.85:  # skip footnotes and captions set in small type
            sentences = split_sentences(text)
            if sentences:
                current.paragraphs.append(sentences)
    if current.paragraphs and not in_refs:
        parsed.sections.append(current)

    for i, raw in enumerate(refs, start=1):
        ids = find_ids(raw)
        year = _YEAR.search(raw)
        parsed.references.append(Reference(i, None, int(year.group(0)) if year else None, ids[0] if ids else None, raw))
    return parsed
