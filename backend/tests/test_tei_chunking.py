from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

import pytest

from core.config import AppConfig, ChunkingConfig
from ingest.chunking import chunk_document, document_text, embedding_input
from ingest.tei import ParsedDoc, Section, parse_tei, read_parsed, to_tei, write_parsed

FIXTURE = Path(__file__).parent / "fixtures" / "sample.tei.xml"


def words(texts: Sequence[str]) -> list[int]:
    return [len(t.split()) for t in texts]


def test_parse_tei_body_and_abstract() -> None:
    doc = parse_tei(FIXTURE.read_bytes())
    assert doc.title == "Low-Rank Adapters for Sparse Transformers"
    heads = [s.head for s in doc.sections]
    assert heads == ["Abstract", "1 Introduction", "2 Method", "2.1 Sparsity", "A Proofs"]  # no acknowledgements
    assert doc.sections[0].paragraphs == [
        ["We study low-rank adapters for sparse transformers.", "Our method reduces memory by 3x."]
    ]
    assert doc.sections[1].paragraphs[0][0] == "Large models are expensive to fine-tune [1]."
    assert "W' = W + BA" not in " ".join(s for sec in doc.sections for p in sec.paragraphs for s in p)


def test_parse_tei_bibliography() -> None:
    refs = parse_tei(FIXTURE.read_bytes()).references
    assert [r.position for r in refs] == [1, 2, 3, 4]
    assert refs[0].title == "LoRA: Low-Rank Adaptation of Large Language Models"
    assert refs[0].arxiv_id == "2106.09685" and refs[0].year == 2022  # idno, version stripped
    assert refs[1].arxiv_id == "1902.00751" and refs[1].year == 2019  # from the raw string
    assert refs[2].title == "Deep Learning" and refs[2].arxiv_id is None and refs[2].raw_text is None
    assert refs[3].title is None and refs[3].arxiv_id == "math/0303109"  # old style, subject class dropped


def test_tei_roundtrip_for_pymupdf(tmp_path: Path) -> None:
    doc = parse_tei(FIXTURE.read_bytes())
    again = parse_tei(to_tei(doc))
    assert [s.head for s in again.sections] == [s.head for s in doc.sections]
    assert [s.paragraphs for s in again.sections] == [s.paragraphs for s in doc.sections]
    assert [(r.title, r.arxiv_id, r.raw_text) for r in again.references] == [
        (r.title, r.arxiv_id, r.raw_text) for r in doc.references
    ]
    path = tmp_path / "x.tei.xml.gz"
    write_parsed(path, to_tei(doc))
    assert parse_tei(read_parsed(path)).title == doc.title


def _long_doc() -> ParsedDoc:
    sections = []
    for s in range(3):
        paras = [[f"Section {s} paragraph {p} sentence {i} " + "word " * (5 + (i * 7) % 11) for i in range(12)]
                 for p in range(4)]  # fmt: skip
        sections.append(Section(f"{s + 1} Part", [[x.strip() for x in para] for para in paras]))
    sections.append(Section("4 Short", [["Tiny closing remark here."]]))
    return ParsedDoc(title="T", sections=sections)


def test_chunk_invariants(cfg: AppConfig) -> None:
    c = cfg.chunking
    text, chunks = chunk_document(_long_doc(), words, c)
    assert chunks, "expected chunks"
    covered: set[int] = set()
    for i, ch in enumerate(chunks):
        assert ch.position == i
        assert ch.text == text[ch.char_start : ch.char_end]  # exact slice of the canonical text
        assert ch.token_count <= c.max_tokens
        covered.update(range(ch.char_start, ch.char_end))
    _, spans = document_text(_long_doc())
    assert all(s in covered for _, s, _ in spans), "every sentence is in some chunk"
    for a, b in pairwise(chunks):
        if a.section == b.section and b.char_start < a.char_end:
            overlap = text[b.char_start : a.char_end]
            assert len(overlap.split()) <= c.overlap_tokens
    assert all(ch.token_count >= c.min_tokens for ch in chunks[:-1])
    assert "Tiny closing remark" in chunks[-1].text  # merged, not dropped


def test_small_chunks_and_long_sentence() -> None:
    c = ChunkingConfig(target_tokens=20, max_tokens=30, overlap_tokens=5, min_tokens=8)
    doc = ParsedDoc(sections=[Section("A", [["x " * 70 + "end."], ["one two three.", "four five six seven eight."]])])
    text, chunks = chunk_document(doc, words, c)
    assert all(ch.token_count <= c.max_tokens for ch in chunks)
    assert all(ch.text == text[ch.char_start : ch.char_end] for ch in chunks)
    assert chunks[-1].text.endswith("seven eight.")


def test_embedding_input_header() -> None:
    _, chunks = chunk_document(parse_tei(FIXTURE.read_bytes()), words, ChunkingConfig(
        target_tokens=350, max_tokens=450, overlap_tokens=50, min_tokens=5))  # fmt: skip
    first = chunks[0]
    assert embedding_input("My Title", first).startswith(f"Title: My Title | Section: {first.section}\n")
    assert not first.text.startswith("Title:")


@pytest.mark.parametrize("n_sections", [0])
def test_empty_doc(n_sections: int, cfg: AppConfig) -> None:
    assert chunk_document(ParsedDoc(), words, cfg.chunking)[1] == []
