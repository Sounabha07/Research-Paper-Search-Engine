"""arXiv ids and the HTTP client used by the fetcher.

Only the fetcher process may call `ArxivClient`: it owns the single global rate limiter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from core.config import AppConfig

log = logging.getLogger(__name__)

PDF_URL = "https://export.arxiv.org/pdf/{arxiv_id}"
QUERY_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

# New style 2106.09685(v2), old style hep-th/9901001 or math.AG/0101001.
NEW_ID = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?!\d)")
OLD_ID = re.compile(r"(?<![\w.-])([a-z\-]+)(\.[A-Z]{2})?/(\d{7})(v\d+)?(?!\d)")


def normalize_id(raw: str) -> str | None:
    """Canonical arXiv id (no version, no 'arXiv:' prefix, old style without subject class)."""
    s = raw.strip().removeprefix("arXiv:").removeprefix("arxiv:").strip()
    m = NEW_ID.fullmatch(s) or NEW_ID.search(s)
    if m:
        return m.group(1)
    m = OLD_ID.search(s)
    if m:
        return f"{m.group(1)}/{m.group(3)}"
    return None


def find_ids(text: str) -> list[str]:
    """All arXiv ids mentioned in free text, canonical, in order, deduplicated."""
    found = [m.group(1) for m in NEW_ID.finditer(text)]
    found += [f"{m.group(1)}/{m.group(3)}" for m in OLD_ID.finditer(text)]
    return list(dict.fromkeys(found))


def file_stem(arxiv_id: str) -> str:
    """Filesystem-safe name: old-style ids contain a slash."""
    return arxiv_id.replace("/", "_")


class RateLimiter:
    """Enforces a minimum interval between any two requests, process-wide."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                remaining = self.min_interval_s - (self._clock() - self._last)
                if remaining > 0:
                    await self._sleep(remaining)
            self._last = self._clock()


class FetchError(RuntimeError):
    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(when.tzinfo)
    return max(0.0, (when - now).total_seconds())


@dataclass(frozen=True)
class PdfResult:
    content: bytes
    download_s: float


@dataclass(frozen=True)
class AtomEntry:
    arxiv_id: str
    title: str
    abstract: str
    authors: list[dict[str, str]]
    categories: list[str]
    primary_category: str | None
    published: datetime | None
    journal_ref: str | None
    comments: str | None


class ArxivClient:
    MAX_ATTEMPTS = 5
    BACKOFF_BASE_S = 5.0
    BACKOFF_MAX_S = 120.0
    MAX_RETRY_AFTER_S = 600.0

    def __init__(
        self,
        cfg: AppConfig,
        limiter: RateLimiter,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._limiter = limiter
        self._sleep = sleep
        self.last_request_s = 0.0  # duration of the last HTTP request, excluding rate-limit waits
        self._headers = {"User-Agent": cfg.arxiv.user_agent}  # sent on every request
        # Single connection, as arXiv's terms require.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=20.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, url: str, params: dict[str, str] | None = None, max_attempts: int = 0,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:  # fmt: skip
        max_attempts = max_attempts or self.MAX_ATTEMPTS
        for attempt in range(1, max_attempts + 1):
            await self._limiter.wait()
            started = time.monotonic()
            try:
                resp = await self._client.get(url, params=params, headers={**self._headers, **(headers or {})})
            except httpx.TransportError as e:
                delay = self._backoff(attempt)
                log.warning("arxiv transport error", extra={"url": url, "attempt": attempt, "error": str(e)})
            else:
                self.last_request_s = time.monotonic() - started
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 404:
                    raise FetchError(f"404 for {url}", permanent=True)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    body = " ".join(resp.text[:300].split())
                    raise FetchError(f"HTTP {resp.status_code} for {resp.request.url}: {body}")
                ra = retry_after_seconds(resp.headers.get("Retry-After"))
                delay = min(ra, self.MAX_RETRY_AFTER_S) if ra is not None else self._backoff(attempt)
                log.warning(
                    "arxiv throttled",
                    extra={"url": url, "status": resp.status_code, "attempt": attempt, "delay_s": round(delay, 1)},
                )
            if attempt < max_attempts:
                await self._sleep(delay)
        raise FetchError(f"gave up on {url} after {max_attempts} attempts")

    def _backoff(self, attempt: int) -> float:
        return float(min(self.BACKOFF_MAX_S, self.BACKOFF_BASE_S * 2 ** (attempt - 1)) * (0.75 + random.random() / 2))

    async def fetch_pdf(self, arxiv_id: str) -> PdfResult:
        resp = await self._get(PDF_URL.format(arxiv_id=arxiv_id))
        content = resp.content
        if not content.startswith(b"%PDF"):
            ctype = resp.headers.get("content-type", "")
            raise FetchError(f"not a PDF ({ctype}, {len(content)} bytes)", permanent=ctype.startswith("text/html"))
        return PdfResult(content, self.last_request_s)

    async def list_records(self, params: dict[str, str]) -> str:
        """One OAI-PMH request (ListRecords page, or Identify). 503 + Retry-After is honored by _get."""
        from ingest.oai import OAI_URL

        resp = await self._get(OAI_URL, params, headers={"Accept": "text/xml, application/xml"})
        return resp.text

    async def query_ids(self, ids: list[str]) -> list[AtomEntry]:
        """Metadata for up to 100 ids. Only for referenced ids missing from the DB."""
        if not 0 < len(ids) <= 100:
            raise ValueError("query_ids takes 1 to 100 ids")
        # low priority: few attempts, the caller re-queues, so urgent PDF fetches are not stuck behind backoff
        resp = await self._get(QUERY_URL, {"id_list": ",".join(ids), "max_results": "100"}, max_attempts=2)
        return parse_atom(resp.text)


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_atom(xml_text: str) -> list[AtomEntry]:
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        arxiv_id = normalize_id(_text(e.find(f"{ATOM}id")).rsplit("/abs/", 1)[-1])
        title = _text(e.find(f"{ATOM}title"))
        if arxiv_id is None or not title or title == "Error":
            continue
        published = None
        with_date = _text(e.find(f"{ATOM}published"))
        if with_date:
            published = datetime.fromisoformat(with_date.replace("Z", "+00:00"))
        authors = []
        for a in e.findall(f"{ATOM}author"):
            name = _text(a.find(f"{ATOM}name"))
            first, _, last = name.rpartition(" ")
            authors.append({"first": first, "last": last})
        primary = e.find(f"{ARXIV_NS}primary_category")
        cats = [c.get("term", "") for c in e.findall(f"{ATOM}category") if c.get("term")]
        primary_term = primary.get("term") if primary is not None else None
        if primary_term and primary_term in cats:
            cats.remove(primary_term)
        out.append(
            AtomEntry(
                arxiv_id=arxiv_id,
                title=title,
                abstract=_text(e.find(f"{ATOM}summary")),
                authors=authors,
                categories=([primary_term] if primary_term else []) + cats,
                primary_category=primary_term or (cats[0] if cats else None),
                published=published,
                journal_ref=_text(e.find(f"{ARXIV_NS}journal_ref")) or None,
                comments=_text(e.find(f"{ARXIV_NS}comment")) or None,
            )
        )
    return out
