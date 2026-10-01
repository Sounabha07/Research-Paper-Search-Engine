"""arXiv BibTeX entries: @misc with eprint, archivePrefix, primaryClass."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_WORD = re.compile(r"[A-Za-z0-9]+")
_STOP = {"a", "an", "the", "on", "of", "for", "and", "in", "to", "with", "is", "are", "via"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("&", "\\&").replace("%", "\\%")


def cite_key(paper: dict[str, Any], taken: set[str]) -> str:
    authors = paper.get("authors") or []
    last_words = _WORD.findall(_ascii(authors[0].get("last", ""))) if authors else []
    last = last_words[0].lower() if last_words else "anon"
    words = [w.lower() for w in _WORD.findall(_ascii(paper.get("title", ""))) if w.lower() not in _STOP]
    base = f"{last}{paper.get('year') or ''}{words[0] if words else ''}"
    key, n = base, 0
    while key in taken:
        n += 1
        key = base + chr(ord("a") + n - 1) if n <= 26 else f"{base}{n}"
    taken.add(key)
    return key


def bibtex_entry(paper: dict[str, Any], key: str) -> str:
    authors = " and ".join(f"{a.get('last', '')}, {a.get('first', '')}".strip(", ") for a in paper.get("authors") or [])
    fields = [
        ("title", "{" + _escape(paper.get("title", "")) + "}"),
        ("author", _escape(authors) or "Unknown"),
        ("year", str(paper.get("year") or "")),
        ("eprint", paper["arxiv_id"]),
        ("archivePrefix", "arXiv"),
        ("primaryClass", paper.get("primary_category") or ""),
        ("url", f"https://arxiv.org/abs/{paper['arxiv_id']}"),
    ]
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
    return f"@misc{{{key},\n{body}\n}}"


def bibtex(papers: list[dict[str, Any]]) -> str:
    taken: set[str] = set()
    return "\n\n".join(bibtex_entry(p, cite_key(p, taken)) for p in papers) + "\n"
