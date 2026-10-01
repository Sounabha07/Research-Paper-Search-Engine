"""Answer text handling (spec 12, 12c): split WRITE output into tagged sentences, validate tags.

Every sentence must end with source tags like [[c:123]] or [[a:2106.09685]]. Sentences come from
segmenting on tag groups: the text before a tag group is a sentence (if it holds several sentences,
all but the last are untagged). Tags naming a source that was not provided are stripped, and a
sentence left without tags is invalid (removed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.arxiv import NEW_ID, OLD_ID, normalize_id
from ingest.tei import split_sentences

TAG = re.compile(r"\[\[\s*([ca])\s*:\s*([^\]\s]+)\s*\]\]")
TAG_GROUP = re.compile(r"(?:\s*\[\[\s*[ca]\s*:\s*[^\]\s]+\s*\]\])+")
HEADING = re.compile(r"^\s{0,3}#{1,4}\s+(.*\S)\s*$")
LEADING_PUNCT = re.compile(r"^[.!?;:,]+")


def source_id(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


@dataclass
class DraftSentence:
    section: str
    text: str  # without tags
    tags: list[str] = field(default_factory=list)  # canonical source ids ("c:123", "a:2106.09685")
    invalid_tags: list[str] = field(default_factory=list)
    repaired_tags: list[str] = field(default_factory=list)  # e.g. "c:2604.00479->a:2604.00479"

    @property
    def valid(self) -> bool:
        return bool(self.tags)

    def with_tags(self) -> str:
        return self.text + " " + "".join(f"[[{t}]]" for t in self.tags) if self.tags else self.text


def repair_tag(tag: str, allowed: set[str]) -> str | None:
    """A chunk-style tag holding an arXiv id ("c:2604.00479", the writer's most common tag error) names that
    paper's abstract when the abstract is among the passages: return "a:<id>", else None. Garbled ids stay invalid,
    and the repaired sentence still goes through VERIFY."""
    kind, _, ident = tag.partition(":")
    if kind != "c" or not (NEW_ID.fullmatch(ident) or OLD_ID.fullmatch(ident)):
        return None  # chunk ids are plain integers; anything else that is not an arXiv id stays invalid
    fixed = source_id("a", normalize_id(ident) or ident)
    return fixed if fixed in allowed else None


def _tags_in(group: str) -> list[str]:
    return list(dict.fromkeys(source_id(k, i) for k, i in TAG.findall(group)))


def parse_answer(text: str, allowed: set[str]) -> list[DraftSentence]:
    """Split an answer into sentences with their tags. Tags not in `allowed` are stripped."""
    out: list[DraftSentence] = []
    section = ""
    for block in re.split(r"\n\s*\n|\n(?=\s{0,3}#)", text.strip()):
        lines = block.strip().splitlines()
        if not lines:
            continue
        m = HEADING.match(lines[0])
        if m:
            section = m.group(1).strip("# ").strip()
            lines = lines[1:]
        para = " ".join(line.strip() for line in lines).strip()
        if para:
            out += _parse_paragraph(para, section, allowed)
    return out


def _parse_paragraph(para: str, section: str, allowed: set[str]) -> list[DraftSentence]:
    out: list[DraftSentence] = []
    pos = 0
    pending_punct = ""
    for m in TAG_GROUP.finditer(para):
        body = para[pos : m.start()]
        pos = m.end()
        # tags placed before the final punctuation ("claim [[c:1]].") leave it at the start of the next body
        rest = para[pos:]
        punct = LEADING_PUNCT.match(rest)
        if punct:
            pending_punct = punct.group(0)
            pos += len(pending_punct)
        pieces = split_sentences(body)
        if not pieces:
            continue
        for untagged in pieces[:-1]:
            out.append(DraftSentence(section, untagged))
        last = pieces[-1] + pending_punct
        pending_punct = ""
        tags = _tags_in(m.group(0))
        good, invalid, repaired = [], [], []
        for t in tags:
            fixed = t if t in allowed else repair_tag(t, allowed)
            if fixed is None:
                invalid.append(t)
            elif fixed not in good:
                good.append(fixed)
                if fixed != t:
                    repaired.append(f"{t}->{fixed}")
        out.append(DraftSentence(section, last.strip(), good, invalid, repaired))
    for untagged in split_sentences(para[pos:]):
        if LEADING_PUNCT.sub("", untagged).strip():
            out.append(DraftSentence(section, untagged))
    return out


# Source status labels are instructions for WRITE and VERIFY, not wording; the writer sometimes copies them anyway
# ("Gu's landmark paper", "a peer-reviewed paper by Lee", "the Su et al. landmark papers").
_STATUS_LABEL = re.compile(
    r"\b(?:landmark|foundational|peer[- ]reviewed)\s+"
    r"(?=(?:papers?|works?|stud(?:y|ies)|articles?|publications?)\b)",
    re.IGNORECASE,
)


def drop_status_labels(text: str) -> str:
    """Remove status labels used as adjectives ("landmark paper" -> "paper"). "preprint" attributions stay."""
    return _STATUS_LABEL.sub("", text)


def strip_tags(text: str) -> str:
    return " ".join(TAG_GROUP.sub("", text).split())


def number_citations(existing: dict[str, int], cited_in_order: list[str]) -> dict[str, int]:
    """Spec 12c: per-thread numbering; a paper keeps its number, new papers get the next ones."""
    numbers = dict(existing)
    nxt = max(numbers.values(), default=0) + 1
    for arxiv_id in cited_in_order:
        if arxiv_id not in numbers:
            numbers[arxiv_id] = nxt
            nxt += 1
    return numbers


def render_with_numbers(text: str, tag_to_paper: dict[str, str], numbers: dict[str, int]) -> str:
    """Replace [[c:..]]/[[a:..]] tags with [n] paper numbers (for summaries and conversation context)."""

    def repl(m: re.Match[str]) -> str:
        ns = sorted({numbers[p] for t in _tags_in(m.group(0)) if (p := tag_to_paper.get(t)) and p in numbers})
        return " " + "".join(f"[{n}]" for n in ns) if ns else ""

    return TAG_GROUP.sub(repl, text)
