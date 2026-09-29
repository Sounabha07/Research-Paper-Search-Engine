"""Metadata pass (spec 6.1, 7, 9): stream Kaggle, validate landmarks, load tier 1 papers.

Usage: python -m ingest.metadata [--skip-download] [--dry-run]
  --dry-run   stream and print counts only, write nothing
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import asyncpg
import orjson

from core.config import AppConfig, config_path, get_config, get_settings
from core.db import raw_connection
from core.logging import setup_logging
from ingest import indexes, kaggle
from ingest.landmarks import Landmark, load_landmarks, titles_match
from ingest.signals import SignalExtractor
from ingest.text import collapse_ws, normalize_title

log = logging.getLogger(__name__)

BATCH_SIZE = 5000
# Below this many papers at start, treat the load as an initial bulk load (drop/rebuild indexes).
BULK_LOAD_MAX_EXISTING = 100_000


@dataclass(frozen=True)
class PaperRow:
    arxiv_id: str
    title: str
    title_norm: str
    authors: list[dict[str, str]]
    abstract: str
    categories: list[str]
    primary_category: str | None
    v1_date: datetime | None
    license: str | None
    journal_ref: str | None
    comments: str | None
    venue_accepted: bool
    venue_name: str | None
    code_available: bool
    is_landmark: bool
    badges: dict[str, Any]

    def as_record(self) -> tuple[Any, ...]:
        return (
            self.arxiv_id, self.title, self.title_norm, orjson.dumps(self.authors).decode(), self.abstract,
            self.categories, self.primary_category, self.v1_date, self.license, self.journal_ref,
            self.comments, self.venue_accepted, self.venue_name, self.code_available, self.is_landmark,
            orjson.dumps(self.badges).decode(),
        )  # fmt: skip


COLUMNS = [
    "arxiv_id", "title", "title_norm", "authors", "abstract", "categories", "primary_category", "v1_date",
    "license", "journal_ref", "comments", "venue_accepted", "venue_name", "code_available", "is_landmark", "badges",
]  # fmt: skip


def parse_v1_date(rec: dict[str, Any]) -> datetime | None:
    """Submission date = versions[0].created (spec 6.1). Never update_date."""
    versions = rec.get("versions") or []
    if not versions:
        return None
    try:
        return parsedate_to_datetime(versions[0]["created"]).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        return None


def parse_authors(rec: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for parts in rec.get("authors_parsed") or []:
        last, first, suffix = ([*parts, "", "", ""])[:3]
        last = collapse_ws(f"{last} {suffix}")
        if last or first:
            out.append({"first": collapse_ws(first), "last": last})
    return out


def window_cutoff(years_back: int, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    try:
        return now.replace(year=now.year - years_back)
    except ValueError:  # Feb 29
        return now.replace(year=now.year - years_back, day=28)


def has_prefix(categories: Iterable[str], prefixes: Iterable[str]) -> bool:
    prefixes = tuple(prefixes)
    return any(c.startswith(prefixes) for c in categories)


@dataclass
class Counts:
    lines: int = 0
    parse_errors: int = 0
    no_v1_date: int = 0
    cs_math_any_date: int = 0
    in_window: int = 0
    landmarks_listed: int = 0
    landmarks_validated: int = 0
    landmarks_outside_window: int = 0
    landmark_mismatches: list[dict[str, str]] = field(default_factory=list)
    landmarks_missing: list[str] = field(default_factory=list)
    tier1: int = 0
    tier1_by_archive: Counter[str] = field(default_factory=Counter)
    tier1_by_year: Counter[int] = field(default_factory=Counter)
    top_primary: Counter[str] = field(default_factory=Counter)
    venue_accepted: int = 0
    code_available: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


class MetadataPass:
    def __init__(self, cfg: AppConfig, landmarks: list[Landmark], now: datetime | None = None) -> None:
        self.cfg = cfg
        self.prefixes = tuple(cfg.corpus.categories_prefixes)
        self.cutoff = window_cutoff(cfg.corpus.years_back, now)
        self.landmarks = {lm.arxiv_id: lm for lm in landmarks}
        self.seen_landmarks: set[str] = set()
        self.signals = SignalExtractor(cfg)
        self.counts = Counts(landmarks_listed=len(landmarks))

    def process(self, rec: dict[str, Any] | None) -> PaperRow | None:
        c = self.counts
        c.lines += 1
        if rec is None or "id" not in rec:
            c.parse_errors += 1
            return None
        arxiv_id = str(rec["id"])
        categories = (rec.get("categories") or "").split()
        title = collapse_ws(rec.get("title") or "")
        v1 = parse_v1_date(rec)
        if v1 is None:
            c.no_v1_date += 1

        is_cs_math = has_prefix(categories, self.prefixes)
        if is_cs_math:
            c.cs_math_any_date += 1
        in_window = is_cs_math and v1 is not None and v1 >= self.cutoff
        if in_window:
            c.in_window += 1

        is_landmark = False
        listed = self.landmarks.get(arxiv_id)
        if listed is not None:
            self.seen_landmarks.add(arxiv_id)
            if titles_match(listed.title, title):
                is_landmark = True
                c.landmarks_validated += 1
                if not in_window:
                    c.landmarks_outside_window += 1
            else:
                mismatch = {"id": arxiv_id, "listed": listed.title, "actual": title}
                c.landmark_mismatches.append(mismatch)
                log.warning("landmark title mismatch, skipped as landmark", extra=mismatch)

        if not (in_window or is_landmark):
            return None
        return self._row(rec, arxiv_id, title, categories, v1, is_landmark)

    def _row(
        self,
        rec: dict[str, Any],
        arxiv_id: str,
        title: str,
        categories: list[str],
        v1: datetime | None,
        is_landmark: bool,
    ) -> PaperRow:
        abstract = collapse_ws(rec.get("abstract") or "")
        comments = collapse_ws(rec["comments"]) if rec.get("comments") else None
        journal_ref = collapse_ws(rec["journal-ref"]) if rec.get("journal-ref") else None
        sig = self.signals.extract(title, abstract, comments, journal_ref)
        primary = categories[0] if categories else None

        c = self.counts
        c.tier1 += 1
        for archive in {cat.split(".")[0] for cat in categories if cat.startswith(self.prefixes)}:
            c.tier1_by_archive[archive] += 1
        if v1 is not None:
            c.tier1_by_year[v1.year] += 1
        if primary:
            c.top_primary[primary] += 1
        c.venue_accepted += sig.venue_accepted
        c.code_available += sig.code_available

        return PaperRow(
            arxiv_id=arxiv_id,
            title=title,
            title_norm=normalize_title(title),
            authors=parse_authors(rec),
            abstract=abstract,
            categories=categories,
            primary_category=primary,
            v1_date=v1,
            license=rec.get("license") or None,
            journal_ref=journal_ref,
            comments=comments,
            venue_accepted=sig.venue_accepted,
            venue_name=sig.venue_name,
            code_available=sig.code_available,
            is_landmark=is_landmark,
            badges={
                "venue": sig.venue_name,
                "code_available": sig.code_available,
                "landmark": is_landmark,
                "is_survey": sig.is_survey,
                "is_benchmark": sig.is_benchmark,
            },
        )

    def finish(self) -> None:
        missing = sorted(set(self.landmarks) - self.seen_landmarks)
        self.counts.landmarks_missing = missing
        for arxiv_id in missing:
            log.warning("landmark id not found in kaggle metadata, skipped", extra={"id": arxiv_id})


# ---- database load --------------------------------------------------------------

_STAGE_DDL = """
CREATE TEMP TABLE IF NOT EXISTS papers_stage (
    arxiv_id text, title text, title_norm text, authors jsonb, abstract text, categories text[],
    primary_category text, v1_date timestamptz, license text, journal_ref text, comments text,
    venue_accepted boolean, venue_name text, code_available boolean, is_landmark boolean, badges jsonb
) ON COMMIT DELETE ROWS
"""

_CHANGED = " OR ".join(f"papers.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in COLUMNS if c not in ("arxiv_id", "badges"))

_UPSERT = f"""
INSERT INTO papers ({", ".join(COLUMNS)}, state)
SELECT {", ".join(COLUMNS)}, 'metadata_done' FROM papers_stage
ON CONFLICT (arxiv_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in ("arxiv_id", "badges"))},
    badges = papers.badges || EXCLUDED.badges,
    abstract_embedding = CASE
        WHEN papers.title IS DISTINCT FROM EXCLUDED.title OR papers.abstract IS DISTINCT FROM EXCLUDED.abstract
        THEN NULL ELSE papers.abstract_embedding END,
    state = CASE WHEN papers.state = 'discovered' THEN 'metadata_done' ELSE papers.state END
WHERE {_CHANGED} OR NOT (papers.badges @> EXCLUDED.badges)
RETURNING (xmax = 0) AS inserted
"""


async def write_batch(conn: asyncpg.Connection, rows: list[PaperRow], counts: Counts) -> None:
    async with conn.transaction():
        await conn.copy_records_to_table("papers_stage", records=[r.as_record() for r in rows], columns=COLUMNS)
        result = await conn.fetch(_UPSERT)
    inserted = sum(1 for r in result if r["inserted"])
    counts.inserted += inserted
    counts.updated += len(result) - inserted
    counts.unchanged += len(rows) - len(result)


def format_counts(c: Counts, cutoff: datetime, dry_run: bool) -> str:
    lines = [
        "",
        "=== Metadata pass: counts ===",
        f"lines scanned                    {c.lines:>10,}",
        f"parse errors                     {c.parse_errors:>10,}",
        f"missing v1 date                  {c.no_v1_date:>10,}",
        f"cs./math. papers (any date)      {c.cs_math_any_date:>10,}",
        f"in window (v1 >= {cutoff:%Y-%m-%d})     {c.in_window:>10,}",
        f"landmarks listed                 {c.landmarks_listed:>10,}",
        f"  validated                      {c.landmarks_validated:>10,}",
        f"  outside window (added)         {c.landmarks_outside_window:>10,}",
        f"  title mismatch (skipped)       {len(c.landmark_mismatches):>10,}",
        f"  not in kaggle (skipped)        {len(c.landmarks_missing):>10,}",
        f"TIER 1 TOTAL                     {c.tier1:>10,}",
        f"  with any cs.* category         {c.tier1_by_archive['cs']:>10,}",
        f"  with any math.* category       {c.tier1_by_archive['math']:>10,}",
        f"  venue accepted                 {c.venue_accepted:>10,}",
        f"  code available                 {c.code_available:>10,}",
        "  by v1 year: " + ", ".join(f"{y}: {n:,}" for y, n in sorted(c.tier1_by_year.items())),
        "  top primary categories: " + ", ".join(f"{k} {v:,}" for k, v in c.top_primary.most_common(10)),
    ]
    for m in c.landmark_mismatches:
        lines.append(f"  MISMATCH {m['id']}: listed {m['listed']!r}, kaggle has {m['actual']!r}")
    for arxiv_id in c.landmarks_missing:
        lines.append(f"  MISSING  {arxiv_id}")
    if dry_run:
        lines.append("dry run: nothing written")
    else:
        lines += [
            f"db inserted                      {c.inserted:>10,}",
            f"db updated                       {c.updated:>10,}",
            f"db unchanged                     {c.unchanged:>10,}",
        ]
    return "\n".join(lines)


async def run(zip_path: Path, cfg: AppConfig, dry_run: bool) -> Counts:
    mp = MetadataPass(cfg, load_landmarks(config_path().parent / cfg.corpus.landmarks_file))
    conn = None if dry_run else await raw_connection()
    dropped: list[str] = []
    try:
        if conn is not None:
            await conn.execute(_STAGE_DDL)
            if await conn.fetchval("SELECT count(*) FROM papers") < BULK_LOAD_MAX_EXISTING:
                dropped = await indexes.drop_secondary(conn)
        batch: list[PaperRow] = []
        started = time.monotonic()
        for rec in kaggle.iter_records(zip_path):
            row = mp.process(rec)
            if row is not None and conn is not None:
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    await write_batch(conn, batch, mp.counts)
                    batch.clear()
            if mp.counts.lines % 250_000 == 0:
                log.info(
                    "metadata progress",
                    extra={"lines": mp.counts.lines, "tier1": mp.counts.tier1, "s": int(time.monotonic() - started)},
                )
        if batch and conn is not None:
            await write_batch(conn, batch, mp.counts)
        mp.finish()
    finally:
        if conn is not None:
            print("ensuring papers indexes ...", flush=True)
            await indexes.ensure(conn)
            if dropped:
                await conn.execute("VACUUM (ANALYZE) papers")
            await conn.close()
    print(format_counts(mp.counts, mp.cutoff, dry_run), flush=True)
    return mp.counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-download", action="store_true", help="use the existing zip in DATA_DIR/raw")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    args = parser.parse_args()

    setup_logging()
    cfg = get_config()
    raw_dir = get_settings().data_dir / "raw"
    if args.skip_download:
        zip_path = raw_dir / kaggle.ZIP_NAME
        if not kaggle.is_valid_zip(zip_path):
            raise SystemExit(f"No valid {zip_path}; run without --skip-download")
    else:
        zip_path = kaggle.download(raw_dir)

    await run(zip_path, cfg, args.dry_run)
    if not args.dry_run:
        from ingest.scores import recompute_scores

        print(await recompute_scores(cfg), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
