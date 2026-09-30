from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from agent.summary import summary_input
from app.routers.collections import ItemIn, bibtex_order
from retrieval.graph import assemble, common_references


def _paper(arxiv_id: str, cites: int = 0, state: str = "ready") -> dict[str, object]:
    return {"arxiv_id": arxiv_id, "title": f"T {arxiv_id}", "year": 2020, "fulltext_citations": cites, "state": state}


def test_assemble_graph_nodes_edges_and_stubs() -> None:
    g = assemble(
        ["b"],
        [("b", "a"), ("c", "b"), ("b", "stub:7"), ("b", "missing"), ("b", "b")],
        [_paper("a", 5), _paper("b", 2), _paper("c", 0, state="abstract_only")],
        [{"id": 7, "title": "Old book", "year": 1990, "cited_by_count": 3}],
    )
    assert [n["id"] for n in g["nodes"]] == ["b", "a", "c", "stub:7"]  # seed first
    assert g["nodes"][0]["seed"] and not g["nodes"][1]["seed"]
    stub = g["nodes"][3]
    assert stub["stub"] and stub["citations"] == 3 and not stub["full_text"]
    assert not g["nodes"][2]["full_text"]
    # edges to unknown nodes and self-loops are dropped
    assert g["edges"] == [{"source": "b", "target": "a"}, {"source": "b", "target": "stub:7"},
                          {"source": "c", "target": "b"}]  # fmt: skip


def test_common_references() -> None:
    edges = [("s1", "x"), ("s2", "x"), ("s3", "x"), ("s1", "y"), ("s2", "y"), ("s1", "z"), ("s1", "s2")]
    assert common_references(edges, ["s1", "s2", "s3"]) == ["x", "y"]  # z cited once; seeds excluded


def test_summary_input_prefers_key_sections_in_document_order() -> None:
    chunks = [
        ("Introduction", "I" * 100),
        ("Related Work", "R" * 100),
        ("Method", "M" * 100),
        ("Experiments", "E" * 100),
        ("Conclusion", "C" * 100),
    ]
    text = summary_input("Title", "Abs", chunks, max_chars=len("Title: Title\n\nAbstract: Abs") + 4 * 115)
    assert "[Related Work]" not in text  # lowest priority, no room left
    assert (
        text.index("[Introduction]") < text.index("[Method]") < text.index("[Experiments]") < text.index("[Conclusion]")
    )
    assert summary_input("Title", "Abs", []) == "Title: Title\n\nAbstract: Abs"


def test_collection_bibtex_order() -> None:
    r1, r2 = uuid.uuid4(), uuid.uuid4()
    items: list[dict[str, Any]] = [
        {"item_type": "paper", "arxiv_id": "p1", "run_id": None},
        {"item_type": "run", "arxiv_id": None, "run_id": r1},
        {"item_type": "run", "arxiv_id": None, "run_id": r2},
    ]
    assert bibtex_order(items, {r1: ["p2", "p1", "p3"], r2: ["p3"]}) == ["p1", "p2", "p3"]


def test_collection_item_validation() -> None:
    ItemIn(item_type="paper", arxiv_id="2106.09685")
    ItemIn(item_type="run", run_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        ItemIn(item_type="paper", run_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        ItemIn(item_type="run", arxiv_id="2106.09685")
