"""False-alarm check: re-verify sentences that earlier answers kept, under the current verifier.

  python /app/eval/verifier_real_sentences.py THREAD_ID [THREAD_ID ...] [--no-names]

Every sentence here passed the verifier used when it was written; a removal now is either a
genuinely unsupported sentence the old verifier missed, or a false alarm of the stricter checks.
The removed ones are printed with their reason so they can be judged by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

from agent.run import verify_sentences
from agent.text import DraftSentence, strip_tags
from agent.verify import SourceText
from core.config import get_config
from core.db import raw_connection
from core.llm import LLMClient
from core.logging import setup_logging


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("threads", nargs="+")
    parser.add_argument("--no-names", action="store_true")
    args = parser.parse_args()
    setup_logging("WARNING")
    cfg = get_config()
    conn = await raw_connection()
    try:
        rows = await conn.fetch(
            "SELECT s.section, s.text, s.sources FROM answer_sentences s JOIN research_runs r ON r.id = s.run_id"
            " WHERE r.thread_id = ANY($1::uuid[]) AND s.verdict <> 'unsupported_removed' ORDER BY r.turn, s.seq",
            args.threads,
        )
        drafts, ids = [], set()
        for r in rows:
            tags = [x["id"] for x in json.loads(r["sources"])]
            drafts.append(DraftSentence(r["section"], strip_tags(r["text"]), tags))
            ids.update(tags)
        chunks = [int(i[2:]) for i in ids if i.startswith("c:")]
        papers = [i[2:] for i in ids if i.startswith("a:")]
        sources = {
            f"c:{r['id']}": SourceText(r["title"], r["text"])
            for r in await conn.fetch(
                "SELECT c.id, c.text, p.title FROM chunks c JOIN papers p USING (arxiv_id) WHERE c.id = ANY($1::bigint[])",
                chunks,
            )
        }
        sources |= {
            f"a:{r['arxiv_id']}": SourceText(r["title"], f"{r['title']}. {r['abstract']}")
            for r in await conn.fetch("SELECT arxiv_id, title, abstract FROM papers WHERE arxiv_id = ANY($1)", papers)
        }
    finally:
        await conn.close()
    finals = await verify_sentences(LLMClient(cfg), cfg, drafts, sources, None, True, not args.no_names)
    verdicts = Counter(f.verdict for f in finals)
    print(f"{len(finals)} previously kept sentences -> {dict(verdicts)}")
    for f in finals:
        if f.verdict == "unsupported_removed":
            print(f"  removed ({f.reason}): {strip_tags(f.text)[:160]}")


if __name__ == "__main__":
    asyncio.run(main())
