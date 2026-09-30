"""Adversarial verifier test: does VERIFY catch altered sentences?

Runs inside the worker image with eval/ mounted (make eval-verifier). For each case in
verifier_adversarial.yaml, the original sentence (should pass) and the altered sentence (should be
caught) are verified against the source texts read from the database. "Caught" = removed, or rewritten
(the rewrite states only what the source supports).

  python /app/eval/verifier_adversarial.py [--legacy | --no-names] [--out results.json]
    --legacy     the verifier before the exact-span requirement (no span, no name check)
    --no-names   exact-span check only, without the name check
    --no-status  do not pass source status (preprint / venue / landmark) to VERIFY
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from agent.text import DraftSentence
from core.config import get_config
from core.db import raw_connection
from core.llm import LLMClient
from core.logging import setup_logging

CASES = Path(__file__).with_name("verifier_adversarial.yaml")


async def source_texts(ids: set[str]) -> dict[str, tuple[str, str]]:
    """id -> (paper title, source text); abstracts are prefixed with the title, as in the pipeline."""
    conn = await raw_connection()
    try:
        chunks = [int(i[2:]) for i in ids if i.startswith("c:")]
        papers = [i[2:] for i in ids if i.startswith("a:")]
        rows = await conn.fetch(
            "SELECT c.id, c.text, p.title FROM chunks c JOIN papers p USING (arxiv_id) WHERE c.id = ANY($1::bigint[])",
            chunks,
        )
        out = {f"c:{r['id']}": (r["title"], r["text"]) for r in rows}
        rows = await conn.fetch("SELECT arxiv_id, title, abstract FROM papers WHERE arxiv_id = ANY($1)", papers)
        out |= {f"a:{r['arxiv_id']}": (r["title"], f"{r['title']}. {r['abstract']}") for r in rows}
        return out
    finally:
        await conn.close()


async def paper_meta(ids: set[str]) -> dict[str, tuple[str, str]]:
    """source id -> (status, author last names), as the pipeline passes them to WRITE and VERIFY."""
    from agent.run import author_names, source_status

    conn = await raw_connection()
    try:
        rows = await conn.fetch(
            "SELECT c.id::text AS cid, p.arxiv_id, p.is_landmark, p.is_foundational, p.venue_accepted, p.venue_name,"
            " p.journal_ref,"
            " extract(year FROM p.v1_date)::int AS year, p.authors FROM papers p LEFT JOIN chunks c"
            " ON c.arxiv_id = p.arxiv_id AND c.id = ANY($1::bigint[]) WHERE p.arxiv_id = ANY($2) OR c.id IS NOT NULL",
            [int(i[2:]) for i in ids if i.startswith("c:")], [i[2:] for i in ids if i.startswith("a:")],
        )  # fmt: skip
    finally:
        await conn.close()
    out = {}
    for r in rows:
        meta = (source_status(r, r["year"]),
                author_names(r["authors"]))  # fmt: skip
        if r["cid"]:
            out[f"c:{r['cid']}"] = meta
        out[f"a:{r['arxiv_id']}"] = meta
    return out


async def verify(
    drafts: list[DraftSentence], texts: dict[str, tuple[str, str]], legacy: bool, names: bool,
    meta: dict[str, tuple[str, str]] | None = None,
) -> list[Any]:  # fmt: skip
    cfg = get_config()
    llm = LLMClient(cfg)  # no cache, no DB logging: fresh verdicts every run
    from agent import run

    if hasattr(run, "verify_sentences"):
        from agent.verify import SourceText

        meta = meta or {}
        sources = {
            k: SourceText(title=t, text=x, status=meta.get(k, ("", ""))[0], authors=meta.get(k, ("", ""))[1])
            for k, (t, x) in texts.items()
        }
        return list(
            await run.verify_sentences(
                llm, cfg, drafts, sources, run_id=None, require_span=not legacy, name_check=names and not legacy
            )
        )
    # the verifier before the exact-span requirement existed (kept so the baseline can be re-run from git history)
    st = run.RunState.__new__(run.RunState)
    st.deps = SimpleNamespace(cfg=cfg, llm=llm)  # type: ignore[assignment]
    st.run_id = None  # type: ignore[assignment]

    async def step(*_: Any) -> None:
        return None

    st.step = step  # type: ignore[method-assign]
    return list(await run.step_verify(st, drafts, {k: x for k, (_, x) in texts.items()}))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--no-names", action="store_true")
    parser.add_argument("--no-status", action="store_true", help="do not pass source status (preprint/venue)")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    setup_logging("WARNING")
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
    ids = {s for c in cases for s in c["sources"] + c.get("altered_sources", [])}
    texts = await source_texts(ids)
    meta = {} if args.legacy or args.no_status else await paper_meta(ids)
    missing = ids - set(texts)
    if missing:
        raise SystemExit(f"source texts missing from the database: {sorted(missing)}")

    originals = [DraftSentence("eval", c["original"], list(c["sources"])) for c in cases]
    altered = [DraftSentence("eval", c["altered"], list(c.get("altered_sources", c["sources"]))) for c in cases]
    names = not args.no_names
    started = time.monotonic()
    # separate calls so a batch never holds both versions of the same sentence
    orig_res, alt_res = await asyncio.gather(
        verify(originals, texts, args.legacy, names, meta), verify(altered, texts, args.legacy, names, meta)
    )
    elapsed = time.monotonic() - started

    rows = []
    for c, o, a in zip(cases, orig_res, alt_res, strict=True):
        rows.append({
            "id": c["id"], "type": c["type"], "original": o.verdict, "altered": a.verdict,
            "caught": a.verdict != "supported", "false_alarm": o.verdict == "unsupported_removed",
            "expected": c.get("expect"),
            "as_expected": c.get("expect") != "partial" or a.verdict == "partial_rewritten",
            "original_reason": getattr(o, "reason", None), "altered_reason": getattr(a, "reason", None),
            "rewrite": a.text if a.verdict == "partial_rewritten" else None,
        })  # fmt: skip
    mode = "legacy (no span check)" if args.legacy else "exact span" + ("" if args.no_names else " + name check")
    print(f"=== Verifier adversarial test: {mode} ({elapsed:.1f}s) ===")
    for r in rows:
        reason = f"  ({r['altered_reason']})" if r["altered_reason"] else ""
        status = "CAUGHT" if r["caught"] else "missed"
        print(
            f"  #{r['id']:<2} {r['type']:<22} original: {r['original']:<20} altered: {r['altered']:<20} {status}{reason}"
        )
        if r["rewrite"]:
            print(f"      rewrite: {r['rewrite']}")
    caught = sum(r["caught"] for r in rows)
    alarms = sum(r["false_alarm"] for r in rows)
    print(f"caught {caught}/{len(rows)} altered sentences; originals wrongly removed {alarms}/{len(rows)}")
    for r in rows:
        if r["false_alarm"]:
            print(f"  false alarm #{r['id']}: {r['original_reason']}")
    by_type: dict[str, list[bool]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r["caught"])
    print("by type: " + ", ".join(f"{t} {sum(v)}/{len(v)}" for t, v in by_type.items()))
    expected = [r for r in rows if r["expected"]]
    if expected:
        print(
            f"expected verdict (partial, rewritten with attribution): {sum(r['as_expected'] for r in expected)}/{len(expected)}"
        )
    if args.out:
        Path(args.out).write_text(json.dumps({"mode": mode, "rows": rows}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
