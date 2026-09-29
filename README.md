# arXiv Research Engine

A local research tool. You ask a question; it finds relevant arXiv papers (CS + math, last 5 years),
reads them, and writes an answer where every sentence cites the exact paper and passage it came from.

Status: under construction, milestone by milestone. See [PROGRESS.md](PROGRESS.md). The full spec is
[MASTER_PROMPT.md](MASTER_PROMPT.md). A complete README with architecture and eval results comes in milestone 8.

## Quick start

Requirements: Docker Desktop (WSL2 backend on Windows) with NVIDIA GPU support, and GNU make.

```bash
cp .env.example .env   # then fill in the values
make up                # build, start infra, migrate, start everything
make status            # health of every service
```

- Frontend: http://localhost:3000 (admin dashboard at `/admin`)
- API: http://localhost:8000 (`/health`, `/health/ready`, `/admin/stats`, `/docs`)

## Loading the corpus metadata

```bash
make ingest-metadata   # download/refresh the Kaggle arXiv snapshot, load tier 1, print counts, score
make embed             # abstract embeddings on the GPU, then build the vector index
```

## Full text on demand

```bash
make landmarks                          # queue the validated landmark papers
make warm TOPIC="speculative decoding"  # queue the top 30 papers for a topic
make fetch IDS="2106.09685"             # queue specific papers
make fulltext-report                    # fetch/parse timings, chunks, reference resolution
make score                              # PageRank + scores from the citation edges
```

All arXiv traffic goes through the single `fetcher` service (one request every 3.1 s). The API equivalent of
the "Get full text" button is `POST /papers/{arxiv_id}/fulltext`.

`make ingest-metadata ARGS=--dry-run` streams the snapshot and prints the counts without writing anything.
`make score` recomputes scores on its own. The first full load takes a few hours on a laptop; later runs
only touch new or changed papers.

All data lives under `DATA_DIR` (default `./data`) and never goes in the repo, except the Postgres data, which
is in the Docker named volume `arxiv-engine_pgdata` for I/O speed. `docker compose down -v` deletes it; back it up
with `make backup` (written to `DATA_DIR/backups`).
