"""Small text helpers shared by ingestion stages."""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)
_WS = re.compile(r"\s+")


def collapse_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and whitespace. Used for `papers.title_norm`."""
    s = unicodedata.normalize("NFKC", title).lower()
    return _NON_ALNUM.sub("", s)


def title_tokens(title: str) -> list[str]:
    s = unicodedata.normalize("NFKC", title).lower()
    return [t for t in _NON_ALNUM.split(s) if t]
