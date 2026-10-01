"""arXiv OAI-PMH incremental harvest: parsing and record conversion.

The fetcher (the only process that talks to arXiv) requests the pages; this module turns a ListRecords
response into Kaggle-shaped records, so the metadata pass (window/category filter, signals, upsert) is the
same code path as the bulk load.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

OAI_URL = "https://oaipmh.arxiv.org/oai"
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
ARXIV_NS = "{http://arxiv.org/OAI/arXiv/}"


@dataclass
class OaiPage:
    records: list[dict[str, Any]] = field(default_factory=list)
    deleted: int = 0
    token: str | None = None  # resumption token; None on the last page
    complete_size: int | None = None
    error: str | None = None  # OAI error code; "noRecordsMatch" means an empty result, not a failure


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def to_kaggle_record(meta: ET.Element) -> dict[str, Any]:
    """<arXiv> metadata -> the Kaggle JSONL shape read by ingest.metadata (versions[0].created is v1)."""
    created = _text(meta.find(f"{ARXIV_NS}created"))
    versions = []
    if created:
        v1 = datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=UTC)
        versions.append({"version": "v1", "created": format_datetime(v1, usegmt=True)})
    authors = []
    for a in meta.findall(f"{ARXIV_NS}authors/{ARXIV_NS}author"):
        authors.append([_text(a.find(f"{ARXIV_NS}{part}")) for part in ("keyname", "forenames", "suffix")])
    rec: dict[str, Any] = {
        "id": _text(meta.find(f"{ARXIV_NS}id")),
        "title": _text(meta.find(f"{ARXIV_NS}title")),
        "abstract": _text(meta.find(f"{ARXIV_NS}abstract")),
        "categories": _text(meta.find(f"{ARXIV_NS}categories")),
        "authors_parsed": authors,
        "versions": versions,
        "license": _text(meta.find(f"{ARXIV_NS}license")) or None,  # absent = arXiv default license
        "comments": _text(meta.find(f"{ARXIV_NS}comments")) or None,
        "journal-ref": _text(meta.find(f"{ARXIV_NS}journal-ref")) or None,
    }
    return rec


def server_date(identify_xml: str) -> str | None:
    """The repository's current date (YYYY-MM-DD) from an Identify response. `from` must not be later than this:
    the server answers a future `from` with HTTP 406."""
    root = ET.fromstring(identify_xml)
    rd = _text(root.find(f"{OAI_NS}responseDate"))
    return rd[:10] or None


def parse_list_records(xml_text: str) -> OaiPage:
    root = ET.fromstring(xml_text)
    page = OaiPage()
    err = root.find(f"{OAI_NS}error")
    if err is not None:
        page.error = err.get("code") or "unknown"
        return page
    lr = root.find(f"{OAI_NS}ListRecords")
    if lr is None:
        page.error = "noListRecords"
        return page
    for rec in lr.findall(f"{OAI_NS}record"):
        header = rec.find(f"{OAI_NS}header")
        if header is not None and header.get("status") == "deleted":
            page.deleted += 1
            continue
        meta = rec.find(f"{OAI_NS}metadata/{ARXIV_NS}arXiv")
        if meta is not None:
            page.records.append(to_kaggle_record(meta))
    tok = lr.find(f"{OAI_NS}resumptionToken")
    if tok is not None:
        page.token = (tok.text or "").strip() or None
        size = tok.get("completeListSize")
        page.complete_size = int(size) if size and size.isdigit() else None
    return page
