"""Landmark list loading and validation against Kaggle metadata (spec 7).

The hand-compiled titles are often shortened ("Batch Normalization", "GShard"), so a
match accepts: equal normalized titles, the list title being a prefix of the real one,
all list-title tokens appearing in the real title, or a high overall similarity.
Anything else is a mismatch: logged and skipped, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from ingest.text import normalize_title, title_tokens

MIN_PREFIX_CHARS = 6
MIN_RATIO = 0.9


@dataclass(frozen=True)
class Landmark:
    arxiv_id: str
    title: str


def load_landmarks(path: Path) -> list[Landmark]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [Landmark(str(p["id"]), str(p["title"])) for p in data["papers"]]


def titles_match(listed: str, actual: str) -> bool:
    a, b = normalize_title(listed), normalize_title(actual)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= MIN_PREFIX_CHARS and b.startswith(a):
        return True
    listed_tokens = set(title_tokens(listed))
    if listed_tokens and listed_tokens <= set(title_tokens(actual)):
        return True
    return SequenceMatcher(None, a, b).ratio() >= MIN_RATIO
