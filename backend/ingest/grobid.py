"""GROBID client."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

MAX_CONCURRENT = 2  # limits GROBID memory on an 8GB Docker VM
BUSY_RETRIES = 6  # GROBID answers 503 when its own pool is full; wait, don't count as a failure
BUSY_WAIT_S = 5.0
FORM = {
    "consolidateHeader": "0",
    "consolidateCitations": "0",
    "segmentSentences": "1",
    "includeRawCitations": "1",
}


class GrobidError(RuntimeError):
    pass


@dataclass(frozen=True)
class GrobidResult:
    tei: bytes
    request_s: float  # time inside GROBID only, excluding waits for a free slot


class GrobidClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_s: float = 300.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/api/processFulltextDocument"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._sem = asyncio.Semaphore(MAX_CONCURRENT)
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def process_fulltext(self, pdf: bytes, filename: str) -> GrobidResult:
        """One attempt (the caller decides on retries/fallback)."""
        for _ in range(BUSY_RETRIES + 1):
            async with self._sem:
                started = time.monotonic()
                try:
                    resp = await self._client.post(
                        self._url, files={"input": (filename, pdf, "application/pdf")}, data=FORM
                    )
                except httpx.HTTPError as e:
                    raise GrobidError(f"request failed: {type(e).__name__}: {e}") from e
                request_s = time.monotonic() - started
            if resp.status_code == 503:
                await self._sleep(BUSY_WAIT_S)
                continue
            if resp.status_code != 200:
                raise GrobidError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            if b"<TEI" not in resp.content[:2000]:
                raise GrobidError("response is not TEI")
            return GrobidResult(resp.content, request_s)
        raise GrobidError("GROBID stayed busy (503)")
