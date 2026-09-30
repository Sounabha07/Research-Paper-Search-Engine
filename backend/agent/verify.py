"""Code-side checks on VERIFY verdicts.

- Exact span: for "supported" and "partial" the verifier must copy the supporting excerpt from a cited
  source. The span must be a substring of that source after normalization (Unicode NFKC, quotes and dashes
  unified, whitespace collapsed) and at least MIN_SPAN_CHARS long. No valid span = unsupported.
- Names (optional): method/model/dataset names in the sentence (tokens with two or more capitals, or a
  capital and a digit: "QLoRA", "TLinFormer", "GPT-2") must appear in a cited source or its paper title.
  This catches claims attributed to the wrong paper, where the content itself is in the source.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MIN_SPAN_CHARS = 20
# curly quotes, hyphen/dash variants, minus sign, multiplication sign -> ASCII
_QUOTES = str.maketrans({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2010: "-", 0x2011: "-",
                         0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2212: "-", 0x00D7: "x"})  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*")


@dataclass(frozen=True)
class SourceText:
    title: str
    text: str
    status: str = ""  # source_status(): "preprint (2022)", "peer-reviewed: <venue>", "landmark"
    authors: str = ""  # author last names, so attributions like "Fukuyama claims ..." pass the name check


def normalize(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).translate(_QUOTES).split())


def _no_math_delims(s: str) -> str:
    """Spans often drop the $...$ around LaTeX that the abstract has; compare without them."""
    return " ".join(s.replace("$", " ").split())


def find_span(span: str | None, sources: dict[str, SourceText], tags: list[str]) -> str | None:
    """The id of the cited source that contains the span, or None if the span is missing or invalid."""
    if not span:
        return None
    needle = _no_math_delims(normalize(span)).rstrip(".").strip()
    if len(needle) < MIN_SPAN_CHARS:
        return None
    for tag in tags:
        src = sources.get(tag)
        if src and needle in _no_math_delims(normalize(src.text)):
            return tag
    return None


MATH = re.compile(r"\$[^$]+\$")


def _math_key(s: str) -> str:
    return re.sub(r"[\s{}]", "", unicodedata.normalize("NFKC", s))


def altered_math(rewrite: str, original: str, sources: dict[str, SourceText], tags: list[str]) -> list[str]:
    r"""Math ($...$) in a rewrite that is neither in the original sentence nor in a cited source: the verifier
    sometimes garbles LaTeX when it rewrites (e.g. "\log" -> "\",'log")."""
    known = {_math_key(m) for m in MATH.findall(original)}
    haystack = _math_key(" ".join(sources[t].text for t in tags if t in sources))
    return [m for m in MATH.findall(rewrite) if _math_key(m) not in known and _math_key(m)[1:-1] not in haystack]


def names_in(sentence: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(sentence):
        caps = sum(c.isupper() for c in tok)
        if caps >= 2 or (caps >= 1 and any(c.isdigit() for c in tok)):
            out.append(tok)
    return list(dict.fromkeys(out))


def missing_names(sentence: str, sources: dict[str, SourceText], tags: list[str]) -> list[str]:
    """Names in the sentence that none of its cited sources mention (text, paper title, authors, or venue)."""
    haystack = normalize(
        " ".join(
            f"{sources[t].title} {sources[t].authors} {sources[t].status} {sources[t].text}"
            for t in tags
            if t in sources
        )
    ).lower()
    missing = []
    for name in names_in(sentence):
        pattern = r"(?<![a-z])" + re.escape(normalize(name).lower()) + r"(?![a-z])"
        if not re.search(pattern, haystack):
            missing.append(name)
    return missing
