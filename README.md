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

`make ingest-metadata ARGS=--dry-run` streams the snapshot and prints the counts without writing anything.
`make score` recomputes scores on its own. The first full load takes a few hours on a laptop; later runs
only touch new or changed papers.

All data lives under `DATA_DIR` (default `./data`) and never goes in the repo.
