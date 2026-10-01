# arXiv Research Engine

A local research tool for arXiv computer science and mathematics papers (the last 5 years, plus landmark and
foundational papers). Ask a question; it finds the relevant papers, reads them, and writes an answer in which every
sentence cites the exact passage it came from. A verifier checks each sentence against its sources and removes or
rewrites what they do not support.

> Demo video: _placeholder_

The build log, decisions, and measurements for every milestone are in [PROGRESS.md](PROGRESS.md); the full spec is
[MASTER_PROMPT.md](MASTER_PROMPT.md).

## What it does

- **Answers with checked citations.** Quick mode answers from abstracts and cached full text in ~20-30 s; deep mode
  also fetches full text for the top papers (up to 8), follows their references, and takes 1-3 min. Each sentence
  carries `[n]` chips: hover shows the passage, click opens the reader at that passage.
- **Verification.** For every sentence the verifier must copy an exact supporting span from a cited source (checked
  in code); named methods, datasets, and authors must appear in the cited source; strong claims (proofs, "first",
  state of the art, resolving open problems) from preprints must be attributed ("a 2022 preprint by X claims ...").
  Unsupported sentences are removed and counted.
- **Threads.** Follow-ups keep context; citation numbers stay stable within a thread; papers can be pinned; "Ask about
  this paper" limits a thread to one paper and the papers it cites.
- **Exploration.** Paper pages with a structured summary, a reader of the parsed full text, references, cited-by,
  and a citation graph; consensus bars (which papers support or contradict the central claims); collections with
  BibTeX export; feeds with weekly digests of new papers.
- **Citations are our own.** No external citation API: citation edges come from the bibliographies of papers whose
  full text has been parsed here, so "cited by N" means "cited by N full-text papers in this corpus".

## Architecture

```mermaid
flowchart LR
    subgraph Browser
        UI[Next.js frontend<br/>ask, threads, reader, graph,<br/>collections, feeds, admin]
    end
    UI -- "/api" --> API[FastAPI api<br/>no local models]
    API -- "arq jobs" --> R[(Redis<br/>queues, pub/sub,<br/>LLM cache, rate limits)]
    R --> W[arq worker<br/>embedder + reranker on GPU<br/>answer pipeline, parsing, cron]
    R --> F[fetcher<br/>the only process that talks to arXiv<br/>1 request / 3.1 s]
    F -- "PDFs, ID lookups" --> AX[(arXiv)]
    KG[(Kaggle arXiv snapshot<br/>weekly)] --> W
    F -- "pdf_tmp/" --> W
    W -- "TEI" --> G[GROBID]
    W <--> PG[(Postgres 17 + pgvector<br/>papers, chunks, citation edges,<br/>threads, runs, collections, feeds)]
    API <--> PG
    W -- "WRITE / VERIFY / READ ..." --> GM[(Gemini API<br/>flash-lite, flash)]
    API -- "summaries" --> GM
```

**Answer pipeline** (fixed sequence, no agent loop; every step is saved and streamed over SSE):

```mermaid
flowchart LR
    RW[REWRITE<br/>standalone question<br/>+ 1-3 queries] --> FP[FIND_PAPERS<br/>keyword + vector + anchors,<br/>RRF, cross-encoder rerank]
    FP --> FE[FETCH<br/>deep: full text for top 8]
    FE --> CH[CHAIN<br/>deep: references of the top 5]
    CH --> PA[PASSAGES<br/>chunks + abstracts, reranked]
    PA --> RD[READ<br/>evidence notes]
    RD --> WR[WRITE<br/>tagged sentences]
    WR --> VE[VERIFY<br/>exact span, names,<br/>preprint attribution]
    WR --> CO[CONSENSUS<br/>supports / contradicts]
```

Retrieval (spec section 11): per query, Postgres full-text search and HNSW vector search over abstracts
(`BAAI/bge-small-en-v1.5`, halfvec), fused with reciprocal rank fusion, plus an anchor channel over landmark and
foundational papers; candidates are reranked with `BAAI/bge-reranker-v2-m3`, combined with a citation/metadata prior,
and filtered by a minimum score. Passages come from the chunks of full-text papers the same way; abstract-only papers
contribute their abstract.

Full text on demand: the fetcher downloads a PDF, GROBID parses it to TEI (PyMuPDF as fallback), the worker chunks
(~350 tokens, section-aware), embeds, marks the paper ready, then resolves its bibliography into citation edges
(arXiv id, exact title, trigram title match against the corpus and the full arXiv catalog). PDFs are deleted after
parsing; the TEI is kept.

## Setup

Requirements: Docker Desktop (WSL2 on Windows) with NVIDIA GPU support, GNU make, disk space for `DATA_DIR`,
and 10 GB of memory for the Docker VM (on Windows: `.wslconfig` with `memory=10GB`, `swap=4GB`).

```bash
cp .env.example .env        # GEMINI_API_KEY, Kaggle credentials, CONTACT_EMAIL, POSTGRES_PASSWORD, DATA_DIR
make up                     # build, start infra, run migrations, start everything
make status                 # health of every service
make ingest-metadata        # Kaggle arXiv snapshot -> tier 1 papers (~820k), metadata scores
make embed                  # abstract embeddings (GPU, ~2.5 h the first time)
make landmarks              # queue full text for the ~100 landmark papers
```

- Frontend: http://localhost:3000 (press `/` to focus the ask box)
- API: http://localhost:8000 (`/docs` for the OpenAPI page)

All settings live in [config.yaml](config.yaml) (models, retrieval parameters, timeouts, rate limits, prices).
Secrets are read only from environment variables (`.env`, never committed).

## Operations

| command | what it does |
|---|---|
| `make warm TOPIC="..."` | queue full text for the top papers on a topic |
| `make fetch IDS="2106.09685 ..."` | queue specific papers |
| `make fulltext-report` | fetch/parse timings, chunks, reference resolution rate |
| `make score` | PageRank and scores from the current citation edges |
| `make snapshot` | run the weekly job now: latest Kaggle snapshot, new and changed papers only, then foundational papers, embeddings, scores |
| `make nightly` | nightly OAI-PMH harvest; disabled (`jobs.oai_harvest_enabled: false`), see below |
| `make eval ARGS="..."` | eval suite (below) |
| `make eval-verifier` | adversarial verifier test |
| `make backup` / `make restore FILE=...` | `pg_dump` custom format to `DATA_DIR/backups/` |
| `make test` / `make lint` | pytest; ruff + mypy |

Scheduled in the worker (UTC): janitor every 30 min, weekly Kaggle snapshot Mondays at 00:30, feed digests Mondays
at 03:00.

**New papers.** The weekly job downloads the latest Kaggle arXiv snapshot (skipped when the local copy is current),
processes only records whose `update_date` is on or after the newest one already ingested, upserts them through the
same metadata pass as the initial load, and then runs the post-ingest steps (foundational papers, abstract
embeddings, citation scores, PageRank). The spec's nightly OAI-PMH harvest is implemented but disabled: arXiv's OAI
endpoint answers `ListRecords` with HTTP 406 (other verbs work), so papers newer than the latest snapshot appear with
up to a week's delay.

**Gemini free tier.** The key runs on the free tier: gemini-3.5-flash allows 20 requests per day, so WRITE uses
gemini-3.5-flash-lite by default (`models.write_model`; flash is selectable). A shared per-model rate limiter keeps
requests under the per-minute limits; if the main model returns 503 twice or its daily quota is used up, that call
falls back to flash-lite and the answer shows which model wrote it. Responses are cached in Redis for 30 days.

## Eval

`eval/questions.yaml` has 40 questions (30 quick, 10 deep) with the papers a good answer should find; every expected
paper was checked to be in the corpus. `make eval` runs retrieval for all of them with no LLM calls (the question is
the only query) and reports recall@10, MRR, passage hit@10, and latency; ablations switch off channels or change the
embedding model or chunk size. `make eval ARGS=--answers` runs the full answer pipeline through the API and adds run
latency, cost, removed-claims rate, and a CSV sample of (sentence, cited passage) pairs for manual precision checks.

```bash
make eval                                              # retrieval, baseline
make eval ARGS="--ablations baseline,no-keyword,no-vector,no-rerank"
make eval ARGS="--chunk-size 200"                      # re-chunks full-text papers into eval_cs200
make eval ARGS="--embedding BAAI/bge-base-en-v1.5"     # builds eval_emb_* once (hours)
make eval ARGS="--answers"                             # answer pipeline, cached LLM responses
```

### Results

Eval of 2026-10-01, 40 questions (32 cs, 8 math; 30 quick, 10 deep). Retrieval uses the question as the only query
(no LLM rewrite), so these numbers are retrieval alone.

| variant | recall@10 | MRR | passage hit@10 | latency p50 | latency p95 |
|---|---|---|---|---|---|
| **production** (keyword + vector + anchors, reranked) | **0.637** | **0.605** | **0.725** | 1.9-2.3 s | 2.6-3.5 s |
| no keyword | 0.613 | 0.582 | 0.700 | 1.8 s | 2.3 s |
| no vector | 0.525 | 0.533 | 0.325 | 0.3 s | 1.3 s |
| no rerank | 0.562 | 0.472 | 0.625 | 0.1 s | 0.2 s |

Recall@10 / MRR by question tag (production): landmark paper expected 0.70 / 0.70 (23 questions) vs not 0.56 / 0.47
(17); named in the question 0.70 / 0.62 (27) vs described only 0.50 / 0.58 (13); cs 0.70 / 0.70 (32) vs math
0.38 / 0.23 (8); quick 0.68 / 0.62 (30) vs deep 0.50 / 0.56 (10). The vector channel matters most for questions
without a landmark paper (0.26 recall without it); the reranker is the largest contributor to MRR.

**Keyword channel fix.** The first full eval had the keyword channel slightly hurting recall (0.588 with it, 0.613
without). Its query ANDs every question word, so generic words ("new", "reduce", "replace", "cost") limited it to a few
later papers that paraphrase the question, which then crowded out the original paper (e.g. "Attention Is All You
Need"). Tested on all 40 questions:

| keyword variant | recall@10 | MRR | passage hit@10 | p50 | p95 |
|---|---|---|---|---|---|
| all words (before) | 0.588 | 0.577 | 0.650 | 2.4 s | 3.8 s |
| generic question words removed | 0.613 | 0.602 | 0.675 | 1.6 s | 2.5 s |
| hits kept only if in the vector channel's top 200 | 0.613 | 0.581 | 0.700 | 1.7 s | 2.3 s |
| both (adopted) | 0.637 | 0.605 | 0.725 | 1.6 s | 2.2 s |
| any word, ranked by overlap | 0.613 | 0.573 | 0.700 | 13.6 s | 25.0 s |

Both changes together fix two questions (Transformer, Self-RAG) with no regressions on the other 38 and no latency
cost (the gate reuses the vector query, which now fetches 200 instead of 100). Settings:
`retrieval.keyword_drop_generic`, `retrieval.keyword_gate_k`.

| answers | quick p50 / max | deep | cost per answer | removed claims | citation precision (manual, 55 pairs) |
|---|---|---|---|---|---|
| 6 of 40 (free-tier daily quota reached) | 16 s / 51 s | 111 s (1 run) | $0.013 | 1.8% | 55/55 supported by the cited passage; 2 imprecise attributions |

The answer run stopped being meaningful after 6 questions: gemini-3.5-flash-lite's free tier allows 500 requests a
day and the full run needs about 350, on a day when earlier tests had already used most of the quota. The remaining
34 questions can be run after the daily reset (`make eval ARGS="--answers --ids 7,8,..."`); completed answers come
from the response cache.

Other measured results (details in PROGRESS.md): the adversarial verifier test catches 11 of 12 altered sentences
(wrong numbers, wrong paper, merged experiments, unsupported extensions, preprint claims stated as fact) with at most
1 of 12 originals wrongly removed; the reranker switch to bge-reranker-v2-m3 moved the paper a question is about to
rank 1 in the A/B set.

## Known limitations

- **Math is not compared exactly.** The verifier checks that a supporting span exists and that rewrites keep their
  `$...$` math, but it does not compare formulas in a sentence with the source: full-text passages come from PDFs
  as plain text without LaTeX. A wrong exponent can pass (seen: "|F| > ck^4 m" for a source's (ck^4)^m).
- **Claims combining two sources can slip through.** Each sentence is checked against the passages it cites, as a
  whole; a sentence that merges a fact from one source with a detail from another ("65B on a consumer GPU" from
  "33B on a consumer GPU" and "65B on a 48 GB GPU") is sometimes accepted (missed in every run of the adversarial
  test).
- **Preprint detection depends on arXiv metadata.** A paper counts as peer-reviewed only if its journal-ref or
  comments name a venue; many published papers never update that, so they are treated as preprints and their strong
  claims get attributed ("a 2023 preprint by ... reports"). Landmark and foundational papers are never treated as
  preprints. The verifier also sometimes hedges background statements that need no attribution.
- **Free-tier Gemini.** gemini-3.5-flash allows 20 requests a day and flash-lite 500, so answers are written by
  flash-lite, a busy day runs out (about 60 quick answers a day), and a model's 503/429 falls back to flash-lite
  (shown on the answer). The rate limiter keeps requests under the per-minute limits but cannot add daily quota.
- **Reranker saturation.** On a popular topic the cross-encoder gives dozens of newer papers that discuss the topic
  scores of ~0.99, the same as or above the paper the question is about, and the citation prior adds only ~0.01-0.05,
  so the original drops out of the top 10 even though it is among the candidates. Examples: "How does grouped-query
  attention reduce the memory cost of decoding?" (the GQA paper is a candidate at fused rank 7; "Grouped Value
  Attention", "GTA" and other 2025-26 papers score 0.92-0.998 above it) and "What are the new upper bounds for
  diagonal Ramsey numbers?" (the 2023 breakthrough is a candidate at fused rank 15; papers on multicolor, poset and
  small Ramsey numbers score 0.97-0.995). Not tuned for: a fix would trade recall on originals against recent work.
  Math recall is lower overall (0.38 recall@10 on 8 questions).
- **New papers arrive weekly**, from the Kaggle snapshot; arXiv's OAI-PMH `ListRecords` returns HTTP 406, so the
  nightly harvest is disabled.
- **Citation counts are within this corpus** (edges come only from papers whose full text was parsed here).

## Layout

```
backend/   FastAPI app, arq worker, fetcher, ingestion, retrieval, answer pipeline, tests
frontend/  Next.js app (App Router, Tailwind, shadcn/ui)
eval/      questions, eval runner, verifier and model comparisons, results/
seeds/     landmark papers (validated against the Kaggle metadata at load time)
```
