"""GROBID TEI: parse body text and bibliography; write TEI for the PyMuPDF fallback.

`parsed/{arxiv_id}.tei.xml.gz` always holds TEI, whichever parser produced it, so
re-chunking reads one format.
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from core.arxiv import find_ids, normalize_id

NS = "http://www.tei-c.org/ns/1.0"
T = f"{{{NS}}}"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")
_YEAR = re.compile(r"(19|20)\d{2}")
# Characters XML 1.0 does not allow; PyMuPDF text from math fonts contains some (e.g. \x02).
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
SKIP_BACK_TYPES = {"acknowledgement", "acknowledgment", "funding", "availability", "references"}


@dataclass
class Section:
    head: str
    paragraphs: list[list[str]]  # paragraphs of sentences


@dataclass
class Reference:
    position: int
    title: str | None
    year: int | None
    arxiv_id: str | None  # from <idno type="arXiv"> or the raw text
    raw_text: str | None


@dataclass
class ParsedDoc:
    title: str = ""
    sections: list[Section] = field(default_factory=list)  # "Abstract" first when present
    references: list[Reference] = field(default_factory=list)


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def split_sentences(text: str) -> list[str]:
    text = " ".join(text.split())
    return [s for s in _SENTENCE_SPLIT.split(text) if s] if text else []


def _paragraph(p: ET.Element) -> list[str]:
    sentences = [_text(s) for s in p.findall(f"{T}s")]
    sentences = [s for s in sentences if s]
    return sentences or split_sentences(_text(p))


def _head(div: ET.Element) -> str:
    head = div.find(f"{T}head")
    if head is None:
        return ""
    n = head.get("n")
    text = _text(head)
    return f"{n} {text}" if n and not text.startswith(n) else text


def _sections_from(container: ET.Element | None, default_head: str) -> list[Section]:
    if container is None:
        return []
    out: list[Section] = []

    def walk(el: ET.Element) -> None:
        for div in el.findall(f"{T}div"):
            if div.get("type") in SKIP_BACK_TYPES:
                continue  # skip the whole subtree
            paragraphs = [p for p in (_paragraph(p) for p in div.findall(f"{T}p")) if p]
            if paragraphs:
                out.append(Section(_head(div) or default_head, paragraphs))
            walk(div)

    walk(container)
    return out


def _year(bibl: ET.Element) -> int | None:
    for date in bibl.iter(f"{T}date"):
        m = _YEAR.search(date.get("when") or _text(date))
        if m:
            return int(m.group(0))
    return None


def _reference(position: int, bibl: ET.Element) -> Reference:
    title = _text(bibl.find(f"{T}analytic/{T}title")) or _text(bibl.find(f"{T}monogr/{T}title")) or None
    raw = next((_text(n) for n in bibl.iter(f"{T}note") if n.get("type") == "raw_reference"), "") or None
    arxiv_id = None
    for idno in bibl.iter(f"{T}idno"):
        if (idno.get("type") or "").lower() == "arxiv":
            arxiv_id = normalize_id(_text(idno))
            if arxiv_id:
                break
    if arxiv_id is None and raw:
        ids = find_ids(raw)
        arxiv_id = ids[0] if ids else None
    return Reference(position, title, _year(bibl), arxiv_id, raw)


def xml_safe(text: str | None) -> str | None:
    return _XML_INVALID.sub("", text) if text else text


def parse_tei(xml: bytes | str) -> ParsedDoc:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # TEI written by an earlier to_tei kept XML-invalid characters from PyMuPDF text: drop them and retry
        raw = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
        root = ET.fromstring(_XML_INVALID.sub("", raw).encode("utf-8"))
    doc = ParsedDoc(title=_text(root.find(f"{T}teiHeader/{T}fileDesc/{T}titleStmt/{T}title")))
    abstract = root.find(f"{T}teiHeader/{T}profileDesc/{T}abstract")
    if abstract is not None:
        paragraphs = [p for p in (_paragraph(p) for p in abstract.iter(f"{T}p")) if p]
        if paragraphs:
            doc.sections.append(Section("Abstract", paragraphs))
    text = root.find(f"{T}text")
    if text is not None:
        doc.sections += _sections_from(text.find(f"{T}body"), "Body")
        doc.sections += _sections_from(text.find(f"{T}back"), "Appendix")
        bibls = [b for lb in text.iter(f"{T}listBibl") for b in lb.findall(f"{T}biblStruct")]
        doc.references = [_reference(i + 1, b) for i, b in enumerate(bibls)]
    return doc


def to_tei(doc: ParsedDoc) -> bytes:
    """Serialize a ParsedDoc (PyMuPDF fallback) into the subset of TEI that parse_tei reads."""
    ET.register_namespace("", NS)
    root = ET.Element(f"{T}TEI")
    header = ET.SubElement(root, f"{T}teiHeader")
    title_stmt = ET.SubElement(ET.SubElement(header, f"{T}fileDesc"), f"{T}titleStmt")
    ET.SubElement(title_stmt, f"{T}title").text = xml_safe(doc.title)
    text = ET.SubElement(root, f"{T}text")
    body = ET.SubElement(text, f"{T}body")
    profile = ET.SubElement(header, f"{T}profileDesc")
    for section in doc.sections:
        if section.head == "Abstract":
            div = ET.SubElement(ET.SubElement(profile, f"{T}abstract"), f"{T}div")
        else:
            div = ET.SubElement(body, f"{T}div")
            ET.SubElement(div, f"{T}head").text = xml_safe(section.head)
        for para in section.paragraphs:
            p = ET.SubElement(div, f"{T}p")
            for sentence in para:
                ET.SubElement(p, f"{T}s").text = xml_safe(sentence)
    if doc.references:
        list_bibl = ET.SubElement(ET.SubElement(ET.SubElement(text, f"{T}back"), f"{T}div", type="references"),
                                  f"{T}listBibl")  # fmt: skip
        for ref in doc.references:
            bibl = ET.SubElement(list_bibl, f"{T}biblStruct")
            if ref.title:
                ET.SubElement(ET.SubElement(bibl, f"{T}analytic"), f"{T}title").text = xml_safe(ref.title)
            if ref.arxiv_id:
                ET.SubElement(bibl, f"{T}idno", type="arXiv").text = ref.arxiv_id
            if ref.raw_text:
                ET.SubElement(bibl, f"{T}note", type="raw_reference").text = xml_safe(ref.raw_text)
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def write_parsed(path: Path, tei: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wb") as f:
        f.write(tei)
    tmp.replace(path)


def read_parsed(path: Path) -> bytes:
    with gzip.open(path, "rb") as f:
        return f.read()
