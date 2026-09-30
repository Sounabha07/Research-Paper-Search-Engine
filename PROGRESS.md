# Progress

Milestones from MASTER_PROMPT.md section 21. Each one stops for approval before the next.

- [x] 1. **Skeleton**: Compose (postgres, redis, grobid, api, worker, fetcher, frontend), config loading, migrations for all tables, LLM client with model check, health endpoints, Makefile.
- [x] 2. **Metadata**: Kaggle stream + filter, landmark validation, tier 1 papers, metadata score, abstract embeddings + indexes, counts printed. `/admin` page.
- [x] 3. **Full text on demand**: fetcher service with global limiter and two queues, GROBID parsing with bibliography, chunking, chunk embeddings, PDF lifecycle and janitor, landmark prefetch, `POST /papers/{id}/fulltext`, `make warm`, reference resolution into citation edges and stubs, citation scores and PageRank. Report the reference resolution rate.
- [x] 4. **Search**: hybrid retrieval, rerank, `/search` endpoint, basic search UI returning papers and passages.
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
- Storage: Postgres on the Windows bind mount was the main cost of every bulk job (see the move below).
- The HNSW build outgrew maintenance_work_mem=1GB at ~740k rows (slower tail); index builds now use 2GB (below).

### Between milestones 2 and 3 (2026-09-29)
- Postgres data moved from the `DATA_DIR/postgres` bind mount to the Docker named volume `arxiv-engine_pgdata`
  (on the WSL2 filesystem, inside Docker's disk image). Everything else stays in DATA_DIR. Migrated by copying
  the data directory after a clean shutdown (no rebuild); counts identical before/after (820,325 papers,
  820,325 embeddings, 103 landmarks, same sum of meta_score). The old bind folder was then removed.
  - Random 8KB reads, 1GB file, VM page cache dropped first: bind mount 2,517/s cold, 3,232/s cached;
    named volume 5,072/s cold, 470,824/s cached. The bind mount never benefits from the VM page cache.
  - Same search queries as milestone 2: vector 591/524 ms -> 390/299 ms cold, 1-2 ms warm; keyword
    995 ms -> 336 ms cold, 6 ms warm.
  - Backups still go to `DATA_DIR/backups` (bind mount). Backup after the move:
    `data/backups/arxiv-20260929-172426.dump` (1.9 GB, pg_dump custom format, verified with pg_restore --list).
  - Caution: `docker volume rm`, `docker compose down -v`, or a Docker Desktop reset/purge deletes the database.
    Restore with `make restore FILE=<dump>` after `make migrate`.
- Index builds set `maintenance_work_mem = 2GB` for their own session only (`ingest/indexes.py`); the server
  default stays 1GB. Postgres `shm_size` raised to 3gb because parallel HNSW builds hold the graph in shared memory.
- Postgres `stop_grace_period: 180s` so it always shuts down cleanly.
- Postgres healthcheck has `start_period: 600s` so WAL recovery after an unclean stop doesn't block the stack.
- Foundational papers (older papers cited by >= 20 full-text papers) need citation edges: milestone 3 / nightly.
- `make landmarks` (full-text prefetch) and `make warm` arrive in milestone 3.

### Milestone 3 (done 2026-09-30, awaiting approval)
- Test run: 103 landmarks + 25 recent papers, all 128 `ready`, all parsed by GROBID (0 PyMuPDF fallbacks).
  - Fetch time per paper (download only, excluding the 3.1s rate-limit spacing): median 1.4s, mean 4.9s,
    p90 12.5s, max 83s (48 MB PDF). PDF size median 1.1 MB.
  - GROBID parse time per paper: median 4.6s, mean 7.0s, p90 12.9s, max 58.6s (first call, JVM warm-up).
  - Chunks per paper: median 49, mean 66, p90 138, min 12, max 503; 50-350 tokens per chunk.
  - References: 6,614. Resolved to a corpus paper 42.6%, stub 10.4%, arXiv id not in DB 0.4% (the arXiv API
    returned nothing for those ids), unresolved 46.6%. By method: arXiv id 1,970, exact title 687, trigram 4.
  - Unresolved sample: mostly works not on arXiv (books, journals, pre-2000: 488) or arXiv papers older than the
    5-year window that the reference names only by title. Only 55 unresolved refs have a >= 0.8 trigram match
    in the DB at any year, so the rules are not missing many.
  - Citation graph: 3,499 edges, 142 stubs; PageRank for 1,612 nodes. Most cited: Attention Is All You Need (39).
- Semantics chosen where the spec is open:
  - `fetch:done:{id}` is published when the paper reaches `ready` (or `abstract_only`), not when the PDF is
    downloaded, because waiting runs need chunks, not a PDF.
  - Referenced arXiv ids missing from the DB are filled via the arXiv API (6.2b) as state `discovered`: they
    are graph nodes with metadata but not tier 1 (not scored, not embedded, excluded from search).
    Promotion to tier 1 as foundational papers happens in the nightly job (milestone 8).
  - Title matching (exact and trigram) needs >= 10 normalized characters; shorter titles ("Adam") only resolve
    by arXiv id.
  - `parsed/` always holds TEI; the PyMuPDF fallback writes its result as TEI too, so re-chunking reads one format.
  - Chunk text is an exact slice of one canonical document text (headings + paragraphs), for highlighting.
- Bugs found in the live run and fixed:
  - Deadlock between two papers resolving references at once (shared cited-paper rows). Graph writers now take a
    transaction-level advisory lock; the chunk job also retries on deadlock/serialization errors.
  - Citation percentiles: NULL PageRank sorted as highest, and percent_rank gave the only member of a
    (category, year) group 0, so Attention Is All You Need scored 0.30. Now percentiles are over papers with
    citation data only, using cume_dist. With 128 full-text papers, groups are tiny and scores are noisy.
- Memory: the Docker VM (8 GB) ran out once when GROBID (~3.1-3.4 GB, JVM sized from the VM), Postgres, the
  worker and two extra one-off containers ran together; the Docker engine hung and was restarted. Postgres
  recovered in 0s. A 3 GB cap on GROBID starved it (all 5 test papers fell back to PyMuPDF), so it was reverted
  and those papers re-parsed with GROBID. The host was also at 38.5/41.4 GB committed memory.
- New commands: `make landmarks`, `make warm TOPIC="..." [N=30]`, `make fetch IDS="..."`, `make fulltext-report`,
  `POST /papers/{id}/fulltext`. `make score` now includes PageRank.

### Between milestones 3 and 4 (2026-09-30)
- GROBID JVM heap set to 2.5 GB via `JAVA_OPTS=-Xmx2560m` (docker-compose.yml). Checked in the image, not guessed:
  its start script `/opt/grobid/grobid-service/bin/grobid-service` passes
  `$DEFAULT_JVM_OPTS $JAVA_OPTS $GROBID_SERVICE_OPTS` to java, and the image already sets `GROBID_SERVICE_OPTS`
  (required `--add-opens` flags), so the heap goes in `JAVA_OPTS`. Verified `-Xmx2560m` on the running java
  process with the `--add-opens` flags intact. No container memory cap.
- GROBID parse concurrency lowered from 4 (spec) to 2 (`ingest/grobid.py`).
- `agent.fetch_wait_s` raised from 60 to 90 (config.yaml).
- 20-paper test: 20/20 parsed by GROBID, 0 PyMuPDF fallbacks, done in 2.5 min.
  Peak Docker VM memory 7,379 MB of 7,792 MB (95%; 413 MB left), from 4,944 MB idle. Per-container peaks:
  grobid 4,658 MB, postgres 1,220 MB, worker 1,097 MB. The heap cap does not bound GROBID: the java process
  holds ~3.9 GB resident at idle (about 1.4 GB outside the heap: native CRF models, metaspace, threads), and
  pdfalto subprocesses add more while parsing.
- Parse timing now measures time inside GROBID separately from waiting for a free slot (`grobid_s` vs
  `parse_s`); the earlier 20-paper median of 19 s included queueing. A follow-up 10-paper run: inside GROBID
  median ~6.5 s (2.8-14.8 s), 10/10 GROBID. 158 full-text papers in total, all GROBID.
- Observed: the arXiv query API (metadata for referenced ids, spec 6.2b) returns HTTP 429 without Retry-After
  even at one request per 3.1 s (22 times in 15 min); PDF downloads were not throttled. The fetcher backs off
  exponentially and gives up on a metadata batch after 5 attempts (logged). Metadata jobs share the bulk queue,
  so they delay bulk PDF fetches; urgent fetches are unaffected.

### Milestone 4 (done 2026-09-30, awaiting approval)
- `retrieval/search.py` implements spec 11 in full: per-query keyword (websearch_to_tsquery, ts_rank_cd) and
  vector (HNSW, `hnsw.ef_search=100`, `iterative_scan=relaxed_order`, re-sorted) top-100 over title+abstract
  with filters, RRF (k=60), queries merged by summed RRF, top 50 reranked by bge-reranker-base on
  (question, title + ". " + abstract), + `prior_boost_max * score` (score clamped to [0,1]); then passages:
  keyword + vector over the chunks of `ready` papers for the question and each query, RRF, top 50 reranked,
  <= 3 per paper, 25 total; papers without full text contribute their abstract (always kept).
- `GET /search?q=&year_from=&year_to=&categories=&limit=&passages=` returns papers, passages, per-stage timings,
  and the reranker device. `/search` page in the frontend (filters, badges, passages, "Get full text").
- Models: sentence-transformers 6.1 CrossEncoder applies Sigmoid by default, so rerank scores are in [0,1] and
  the 0.05 prior is on the same scale. Both models load in the API at startup, on CUDA (fp16).
- Tried and reverted: relaxing the keyword query ("at least two informative words") when the strict AND query
  matched nothing (it matched 0 for 2 of 5 test questions). On the 5 test questions it never improved a key
  paper's rank and pushed FlashAttention/FlashAttention-2 out of the reranked 50 for the attention question
  (#1/#3 without it). Back to the spec's strict websearch_to_tsquery; migration 0002 and its table removed.
- 5-question test (warm = second pass; ms): embed 15-74, keyword 7-41, vector 3-16, fusion <0.5,
  rerank 460-750 (50 papers + up to 50 passages), total 490-810. First pass after an API restart: vector
  110-220, keyword 17-127, total 630-1,120. Right after a Docker restart (cold Postgres cache): total 1.2-3.0 s.
  Reranker on cuda:0. Top-10 identical across passes.
- Recall gaps seen in the test: "Fast Inference from Transformers via Speculative Decoding" (landmark) not in the
  top 50 for the speculative-decoding question; DDPM and Latent Diffusion not in the top 50 for diffusion vs GANs.
  For the sensitivity-conjecture question the reranker scores are low overall (best 0.30) but Huang's proof is #2.
  Tuning waits for the milestone 8 eval (recall@10, ablations).
- Memory: with the search models in the API, the idle stack uses 7.2 of 7.8 GB in the Docker VM (API 1.46 GB,
  GROBID 3.06 GB, Postgres 0.97 GB, worker 0.66 GB). Docker hung once during this milestone (host had 1.3 GB
  free) and was restarted; GROBID was stopped during the search test for headroom and restarted afterwards.
  Needs a decision before milestone 5 (the answer pipeline adds the reranker to the worker).

