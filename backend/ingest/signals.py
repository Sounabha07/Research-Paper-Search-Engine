"""Per-paper metadata signals (spec 9): venue acceptance, code availability, survey/benchmark flags."""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.config import AppConfig

# A venue mention counts as accepted only if a context word is within this many characters.
VENUE_CONTEXT_WINDOW = 80
_YEAR_AFTER = re.compile(r"\s*'?((?:19|20)?\d{2})\b")
_CODE_URL = re.compile(r"\b(?:github|gitlab)\.com/[\w.-]+", re.IGNORECASE)
_SURVEY = re.compile(r"\b(?:survey|review|overview)\b", re.IGNORECASE)
_BENCHMARK = re.compile(r"\b(?:benchmark|dataset)s?\b", re.IGNORECASE)
_LEADING_JOURNAL = re.compile(r"^[^\d(]+")


@dataclass(frozen=True)
class Signals:
    venue_accepted: bool
    venue_name: str | None
    code_available: bool
    is_survey: bool
    is_benchmark: bool


class SignalExtractor:
    def __init__(self, cfg: AppConfig) -> None:
        self._venue = re.compile(r"\b(?:" + "|".join(f"(?:{p})" for p in cfg.venue_patterns) + r")\b", re.IGNORECASE)
        self._context = re.compile(r"\b(?:" + cfg.venue_context_words + r")\b", re.IGNORECASE)

    def _venue_with_year(self, text: str, m: re.Match[str]) -> str:
        name = m.group(0)
        y = _YEAR_AFTER.match(text, m.end())
        return f"{name} {y.group(1)}" if y else name

    def venue_from_comments(self, comments: str) -> str | None:
        contexts = [c.start() for c in self._context.finditer(comments)]
        if not contexts:
            return None
        for m in self._venue.finditer(comments):
            if any(abs(m.start() - c) <= VENUE_CONTEXT_WINDOW for c in contexts):
                return self._venue_with_year(comments, m)
        return None

    def venue_from_journal_ref(self, journal_ref: str) -> str:
        m = self._venue.search(journal_ref)
        if m:
            return self._venue_with_year(journal_ref, m)
        lead = _LEADING_JOURNAL.match(journal_ref)
        name = (lead.group(0) if lead else journal_ref).strip(" ,.;:")
        return (name or journal_ref.strip())[:120]

    def extract(self, title: str, abstract: str, comments: str | None, journal_ref: str | None) -> Signals:
        comments = comments or ""
        venue: str | None = None
        if journal_ref and journal_ref.strip():
            venue = self.venue_from_journal_ref(journal_ref)
        elif comments:
            venue = self.venue_from_comments(comments)
        text = f"{title}\n{abstract}"
        return Signals(
            venue_accepted=venue is not None,
            venue_name=venue,
            code_available=bool(_CODE_URL.search(abstract) or _CODE_URL.search(comments)),
            is_survey=bool(_SURVEY.search(text)),
            is_benchmark=bool(_BENCHMARK.search(text)),
        )
