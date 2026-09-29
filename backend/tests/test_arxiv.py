from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from core.arxiv import (
    ArxivClient,
    FetchError,
    RateLimiter,
    file_stem,
    find_ids,
    normalize_id,
    parse_atom,
    retry_after_seconds,
)
from core.config import AppConfig
from ingest.grobid import FORM, GrobidClient, GrobidError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2106.09685", "2106.09685"),
        ("arXiv:2106.09685v3", "2106.09685"),
        ("1501.00001", "1501.00001"),
        ("hep-th/9901001v2", "hep-th/9901001"),
        ("math.AG/0101001", "math/0101001"),
        ("not an id", None),
    ],
)
def test_normalize_id(raw: str, expected: str | None) -> None:
    assert normalize_id(raw) == expected


def test_find_ids_in_raw_reference() -> None:
    text = "See arXiv:2305.14314v1 and arXiv preprint 1706.03762, also cond-mat.stat-mech/0101001? and math.DG/0303109."
    assert find_ids(text) == ["2305.14314", "1706.03762", "math/0303109"]
    assert find_ids("pages 1234.5678901 in 2019") == []
    assert file_stem("math/0211159") == "math_0211159"


def test_retry_after_parsing() -> None:
    assert retry_after_seconds("120") == 120.0
    now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds("Tue, 29 Sep 2026 12:00:30 GMT", now) == 30.0
    assert retry_after_seconds(None) is None and retry_after_seconds("soon") is None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


async def test_rate_limiter_spaces_requests() -> None:
    clock = Clock()
    limiter = RateLimiter(3.1, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    clock.now += 1.0
    await limiter.wait()
    assert clock.sleeps == [pytest.approx(2.1)]


def _client(cfg: AppConfig, handler: httpx.MockTransport, clock: Clock) -> ArxivClient:
    http = httpx.AsyncClient(transport=handler)
    return ArxivClient(cfg, RateLimiter(3.1, clock=clock, sleep=clock.sleep), client=http, sleep=clock.sleep)


async def test_pdf_honors_retry_after_then_succeeds(cfg: AppConfig) -> None:
    calls: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "17"})
        return httpx.Response(200, content=b"%PDF-1.5 body", headers={"content-type": "application/pdf"})

    clock = Clock()
    result = await _client(cfg, httpx.MockTransport(handle), clock).fetch_pdf("2106.09685")
    assert result.content.startswith(b"%PDF")
    assert str(calls[0].url) == "https://export.arxiv.org/pdf/2106.09685"
    assert calls[0].headers["user-agent"] == cfg.arxiv.user_agent
    assert 17.0 in clock.sleeps  # Retry-After honored
    assert all(s >= 0 for s in clock.sleeps)


async def test_pdf_404_is_permanent_and_html_is_rejected(cfg: AppConfig) -> None:
    client = _client(cfg, httpx.MockTransport(lambda r: httpx.Response(404)), Clock())
    with pytest.raises(FetchError) as ei:
        await client.fetch_pdf("9999.99999")
    assert ei.value.permanent
    html = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}))
    with pytest.raises(FetchError):
        await _client(cfg, html, Clock()).fetch_pdf("2106.09685")


async def test_pdf_gives_up_after_max_attempts(cfg: AppConfig) -> None:
    calls = 0

    def handle(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    with pytest.raises(FetchError):
        await _client(cfg, httpx.MockTransport(handle), Clock()).fetch_pdf("2106.09685")
    assert calls == ArxivClient.MAX_ATTEMPTS


ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1902.00751v2</id>
    <published>2019-02-02T12:00:00Z</published>
    <title>Parameter-Efficient Transfer
      Learning for NLP</title>
    <summary>  Fine-tuning large models is inefficient.  </summary>
    <author><name>Neil Houlsby</name></author>
    <author><name>Andrei Giurgiu</name></author>
    <arxiv:comment>ICML 2019</arxiv:comment>
    <arxiv:primary_category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
</feed>"""


def test_parse_atom() -> None:
    [e] = parse_atom(ATOM_SAMPLE)
    assert e.arxiv_id == "1902.00751"
    assert e.title == "Parameter-Efficient Transfer Learning for NLP"
    assert e.abstract == "Fine-tuning large models is inefficient."
    assert e.categories == ["cs.LG", "cs.CL"] and e.primary_category == "cs.LG"
    assert e.published == datetime(2019, 2, 2, 12, tzinfo=UTC)
    assert e.authors[0] == {"first": "Neil", "last": "Houlsby"}
    assert e.comments == "ICML 2019" and e.journal_ref is None


async def test_query_ids_request(cfg: AppConfig) -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, text=ATOM_SAMPLE)

    entries = await _client(cfg, httpx.MockTransport(handle), Clock()).query_ids(["1902.00751", "1706.03762"])
    assert len(entries) == 1
    assert seen[0].url.params["id_list"] == "1902.00751,1706.03762"
    assert seen[0].url.params["max_results"] == "100"
    with pytest.raises(ValueError):
        await _client(cfg, httpx.MockTransport(handle), Clock()).query_ids([])


async def test_grobid_form_and_busy_retry() -> None:
    requests: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b'<?xml version="1.0"?><TEI xmlns="http://www.tei-c.org/ns/1.0"/>')

    async def no_sleep(_: float) -> None:
        return None

    g = GrobidClient(
        "http://grobid:8070", client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), sleep=no_sleep
    )
    tei = await g.process_fulltext(b"%PDF", "x.pdf")
    assert b"<TEI" in tei
    req = requests[-1]
    assert str(req.url) == "http://grobid:8070/api/processFulltextDocument"
    body = req.read().decode(errors="replace")
    assert 'name="input"; filename="x.pdf"' in body
    for k, v in FORM.items():
        assert f'name="{k}"\r\n\r\n{v}' in body
    bad = GrobidClient(
        "http://g", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    with pytest.raises(GrobidError):
        await bad.process_fulltext(b"%PDF", "x.pdf")
