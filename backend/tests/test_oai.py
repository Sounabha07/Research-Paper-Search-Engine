from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from core.config import AppConfig
from ingest.metadata import MetadataPass, parse_authors, parse_v1_date
from ingest.oai import parse_list_records

FIXTURE = Path(__file__).parent / "fixtures" / "oai_listrecords.xml"


def test_parse_list_records() -> None:
    page = parse_list_records(FIXTURE.read_text(encoding="utf-8"))
    assert page.error is None and page.deleted == 1
    assert page.token == "6064147|1001" and page.complete_size == 2345
    new, old = page.records
    assert new["id"] == "2609.20001"
    assert new["title"] == "Sparse Mixtures of Experts for Long-Context Retrieval"  # whitespace collapsed
    assert new["categories"] == "cs.CL cs.LG"
    assert new["license"].endswith("/by/4.0/") and old["license"] is None
    assert parse_v1_date(new) == datetime(2026, 9, 26, tzinfo=UTC)  # <created> is the v1 date
    assert parse_authors(new) == [{"first": "Jane A.", "last": "Doe"}, {"first": "John", "last": "Smith Jr"}]


def test_no_records_match_is_empty_not_failure() -> None:
    xml = (
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><responseDate>x</responseDate>'
        '<error code="noRecordsMatch">No records match</error></OAI-PMH>'
    )
    page = parse_list_records(xml)
    assert page.error == "noRecordsMatch" and page.records == [] and page.token is None


def test_oai_records_go_through_the_metadata_filter(cfg: AppConfig) -> None:
    page = parse_list_records(FIXTURE.read_text(encoding="utf-8"))
    mp = MetadataPass(cfg, [], now=datetime(2026, 9, 30, tzinfo=UTC))
    rows = [r for r in (mp.process(rec) for rec in page.records) if r is not None]
    assert [r.arxiv_id for r in rows] == ["2609.20001"]  # the 2010 paper is outside the window
    assert rows[0].primary_category == "cs.CL" and rows[0].venue_accepted and rows[0].code_available


def test_server_date_from_identify() -> None:
    from ingest.oai import server_date

    xml = (
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><responseDate>2026-09-17T20:41:20Z</responseDate>'
        "<Identify><granularity>YYYY-MM-DD</granularity></Identify></OAI-PMH>"
    )
    assert server_date(xml) == "2026-09-17"
