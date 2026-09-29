# Progress

Milestones from MASTER_PROMPT.md section 21. Each one stops for approval before the next.

- [x] 1. **Skeleton**: Compose (postgres, redis, grobid, api, worker, fetcher, frontend), config loading, migrations for all tables, LLM client with model check, health endpoints, Makefile.
- [x] 2. **Metadata**: Kaggle stream + filter, landmark validation, tier 1 papers, metadata score, abstract embeddings + indexes, counts printed. `/admin` page.
- [ ] 3. **Full text on demand**: fetcher service with global limiter and two queues, GROBID parsing with bibliography, chunking, chunk embeddings, PDF lifecycle and janitor, landmark prefetch, `POST /papers/{id}/fulltext`, `make warm`, reference resolution into citation edges and stubs, citation scores and PageRank. Report the reference resolution rate.
- [ ] 4. **Search**: hybrid retrieval, rerank, `/search` endpoint, basic search UI returning papers and passages.
- [ ] 5. **Quick answers**: quick mode end to end with verification, threads with follow-ups and running summary, stable citation numbering, chips with hover and click-through to the reader, source list, BibTeX export.
- [ ] 6. **Deep mode**: FETCH step with on-demand full text, CHAIN step, pinned papers, live progress over SSE, timeouts, cancel, resume, follow-ups.
- [ ] 7. **Exploration**: paper page, summaries, citation graph, consensus view, collections with BibTeX.
- [ ] 8. **Feeds, nightly jobs, eval, README**: digests, OAI-PMH nightly, eval suite with ablations, README with architecture diagram, setup, and eval results table (leave a placeholder for the demo video).

## Notes

### Milestone 1 (done 2026-09-29)
- Verified live: all 7 services up, `/health/ready` all green, Gemini model check passed for
  `gemini-3.5-flash` and `gemini-3.5-flash-lite`, GPU visible in the worker container.
- `llm_prices` filled in: flash 1.50 / 9.00, flash-lite 0.30 / 2.50 USD per 1M tokens (output includes thinking).
- Repo moved to `C:\search engine`, out of OneDrive; stack re-verified green there. Approved and pushed.
- Postgres and Redis are not published on host ports (host port 5432 was unavailable). Use `docker compose exec postgres psql -U arxiv arxiv`.
- Redis persists to `DATA_DIR/redis` (not listed in spec section 4, but keeps all data under DATA_DIR).
- HNSW indexes exist from migration 0001. Milestone 2 may drop and rebuild them around the bulk load for speed.
- The fetcher currently only runs the rate limiter and a heartbeat. Queue consumption comes in milestone 3.

### Milestone 2 (done 2026-09-29, awaiting approval)
- Kaggle snapshot 2026-09-26: 3,182,867 records, 0 parse errors. Tier 1 = 820,325 papers
  (820,255 cs./math. with v1 >= 2021-09-29, plus 70 older landmarks). cs.* 622,590, math.* 268,011.
- Landmarks: 103/103 validated against Kaggle titles, 0 mismatches, 0 missing.
- Signals: venue accepted 117,403 (14.3%), code available 66,757 (8.1%). meta_score avg 0.199, median 0.130.
- Abstract embeddings: 820,325/820,325 (bge-small, CUDA fp16). Sanity check: vector search uses the HNSW index
  and returns on-topic papers (LoRA landmark in top 5 for a LoRA query).
- kaggle 2.2.4 takes the dataset as a positional arg, but the spec's `-d` form still works (tested live);
  `KAGGLE_API_TOKEN` is supported.
- Bulk mode (`ingest/indexes.py`): jobs that write a large share of `papers` drop the secondary indexes and
  rebuild them once; every job ends by recreating any missing index. Without this, the first full score
  update ran >56 min and was cancelled.
- `/admin/stats` is cached 30s and single-flight (its counts scan the whole table).
- Timings on this laptop: download 10 min, load 29 min, score 17 min, embeddings ~2.5 h (~130/s; GPU encode
  ~400/s, the rest is Postgres writes), index rebuild ~23 min, VACUUM FULL 22 min.
- Storage: Postgres on the Windows bind mount does ~3.4k random 8KB reads/s, the main cost of every bulk job.
  Putting DATA_DIR on the WSL2 filesystem would avoid it (user's decision).
- The HNSW build outgrows maintenance_work_mem=1GB at ~740k rows (slower tail). Fine now; revisit as the
  corpus grows.
- Postgres healthcheck has `start_period: 600s` so WAL recovery after an unclean stop doesn't block the stack.
- Foundational papers (older papers cited by >= 20 full-text papers) need citation edges: milestone 3 / nightly.
- `make landmarks` (full-text prefetch) and `make warm` arrive in milestone 3.
