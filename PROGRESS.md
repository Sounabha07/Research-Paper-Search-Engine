# Progress

Milestones from MASTER_PROMPT.md section 21. Each one stops for approval before the next.

- [x] 1. **Skeleton**: Compose (postgres, redis, grobid, api, worker, fetcher, frontend), config loading, migrations for all tables, LLM client with model check, health endpoints, Makefile.
- [x] 2. **Metadata**: Kaggle stream + filter, landmark validation, tier 1 papers, metadata score, abstract embeddings + indexes, counts printed. `/admin` page.
- [x] 3. **Full text on demand**: fetcher service with global limiter and two queues, GROBID parsing with bibliography, chunking, chunk embeddings, PDF lifecycle and janitor, landmark prefetch, `POST /papers/{id}/fulltext`, `make warm`, reference resolution into citation edges and stubs, citation scores and PageRank. Report the reference resolution rate.
- [x] 4. **Search**: hybrid retrieval, rerank, `/search` endpoint, basic search UI returning papers and passages.
- [x] 5. **Quick answers**: quick mode end to end with verification, threads with follow-ups and running summary, stable citation numbering, chips with hover and click-through to the reader, source list, BibTeX export.
- [x] 6. **Deep mode**: FETCH step with on-demand full text, CHAIN step, pinned papers, live progress over SSE, timeouts, cancel, resume, follow-ups.
- [x] 7. **Exploration**: paper page, summaries, citation graph, consensus view, collections with BibTeX.
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

### Between milestones 4 and 5 (2026-09-30)
- Memory: local models now live only in the worker (embedder + reranker, one shared GPU lock with the
  pipeline). `GET /search` enqueues an arq `search_job` and waits for the result (~50 ms queue overhead;
  worker `poll_delay` 0.05 s, `max_jobs` 16). The API no longer loads torch (1.46 GB -> 92 MB).
  - Docker VM (7,792 MB), idle -> peak while parsing 10 papers and running 10 searches at once:
    models in API, shared_buffers 2 GB: 7,191 MB (92%) idle, 7,379 MB (95%) peak;
    models in worker, shared_buffers 2 GB: 5,815 MB (74%, before GROBID's first parse) idle, 7,297 MB (94%) peak;
    models in worker, shared_buffers 1 GB: 6,282 MB (80%) idle, 6,595 MB (85%) peak.
  - Peak was above 90% with 2 GB, so Postgres `shared_buffers` is now 1 GB (spec: 2 GB).
- Search: `retrieval.rerank_min_score = 0.1` (papers below are dropped; an answer may use fewer than
  `papers_for_answer`). Survivors of 12 for the 5 test questions: 12, 12, 12, 1, 12.
  The sensitivity-conjecture question keeps only 1 paper: Huang's proof (1907.00847) scores 0.094 and is dropped.
- Search: anchor channel (`retrieval.anchor_k = 5`): vector search + title keyword match (OR over the question's
  words) over landmark/foundational papers only (partial index `papers_anchor`), RRF-fused; its top 5 always join
  the rerank candidates. On the 5 test questions the top 10 did not change: anchors joined the candidates
  (3-5 per question) but rerank scores saturate near 1.0, so e.g. the speculative-decoding landmark (2211.17192,
  rerank 0.946) lands at #30 and Latent Diffusion (2112.10752, 0.308) at #45.
- arXiv catalog (`arxiv_catalog`, migration 0002): every Kaggle record (id, title, title_norm, year,
  primary_category), trigram index on title_norm. Filled by `make ingest-metadata` in the same stream, or
  `make catalog`. 3,182,867 records, 3,182,775 unique (the Kaggle file repeats 92 ids), 1.37 GB with indexes,
  17 min to load. Reference resolution now also tries exact title and trigram + year against the catalog after the
  corpus; the arXiv API is only called for ids in neither table.
  - Re-resolved the 178 full-text papers (8,475 references; `make resolve-refs`, 13 min):
    resolved to any arXiv paper 42.8% -> 62.0% (corpus 42.8% -> 45.1%, catalog 0% -> 16.9%),
    stubs 8.3% -> 4.6% (77 stubs removed), unresolved 48.4% -> 33.2%, ids needing the API 42 -> 14.
    (Milestone 3 reported 42.6% on its 128 papers.) Citation edges 3,392 -> 5,617.

### Milestone 5 (done 2026-09-30, approved)
- Quick mode end to end in the worker (`research_run_job`, `agent/run.py`): REWRITE (flash-lite, JSON) ->
  FIND_PAPERS + PASSAGES (section 11 search with thread carryover and evidence reuse) -> READ (flash-lite, JSON,
  notes validated against the passages) -> WRITE (flash, temp 0.3; tags [[c:id]]/[[a:id]]) -> VERIFY (flash-lite,
  temp 0, batches of 5; partial -> rewritten once keeping the original tags; unsupported, untagged, or
  invalid-tag sentences removed) -> CONSENSUS (flash-lite) -> DONE. Every step writes `run_steps` and publishes to
  `run:{id}`. Every LLM call is logged to `llm_calls` with the run id; run cost/tokens are summed from them.
- Threads: follow-ups get the last 3 turns verbatim + the running summary (updated with flash-lite once a thread
  has more than 3 turns); papers cited earlier join the candidates with +0.05 after rerank; chunks cited earlier
  join the passage candidates; stable per-thread citation numbers in `thread_citations`.
- API: POST /threads, POST /threads/{id}/runs (409 while a run is active), GET /threads (recent),
  GET /threads/{id}, GET /runs/{id}, GET /threads/{id}/bibtex, GET /papers/{id}, GET /papers/{id}/content,
  GET /chunks/{id}. Deep mode returns 400 until milestone 6.
- UI: `/` ask box (Quick; Deep disabled), `/threads/[id]` (trace, answer with [n] chips, hover popover with the
  passage and section, click-through to the reader, removed-claims count, cost, time, source list, follow-ups,
  BibTeX export), `/papers/[id]` minimal reader with the cited passage highlighted and scrolled into view.
  Fixed: reader highlight drifted on papers with non-BMP math characters (server offsets are code points,
  JS strings are UTF-16); the reader now slices by code points.
- Test answers (thread A: 4 turns on LoRA/QLoRA; thread B: fast attention):
  | answer | time | cost | sentences | removed | rewritten |
  | LoRA (turn 1) | 23.4 s | $0.0326 | 7 | 0 | 0 |
  | QLoRA follow-up (turn 2) | 21.9 s | $0.0287 | 10 | 0 | 0 |
  | single consumer GPU (turn 3) | 33.6 s | $0.0296 | 8 | 0 | 0 |
  | limitations (turn 4) | 21.7 s | $0.0268 | 9 | 0 | 0 |
  | fast attention | 28.8 s | $0.0350 | 16 | 0 | 1 |
  Average ~$0.031 per answer; WRITE (gemini-3.5-flash) is ~80% of the cost: ~2,500-3,500 output tokens per
  answer, mostly thinking tokens, even at thinking_level "low". Summary produced after turn 4; numbers stable.
- Open issues:
  - All test runs exceeded `quick_timeout_s = 20` (22-34 s) and were marked `timed_out`; WRITE alone takes
    ~15-25 s, and gemini-3.5-flash returned 503 "high demand" several times (one early run took 101 s with two
    retries). Because the deadline had passed, CONSENSUS was skipped on every run. Needs a decision (raise the
    quick budget, or keep it and accept no consensus in quick mode).
  - The verifier removed 0 of 50 sentences and rewrote 1. Removal is covered by unit tests; whether flash-lite is
    too lenient will show in the milestone 8 citation-precision sample.
  - Reranker quality: for "How does QLoRA extend LoRA...", the QLoRA paper itself (2305.14314) reranks #20
    (0.563) below derivative papers, so turn 2 cites derivatives; it is cited in turn 3.
  - READ initially returned only 3 notes; the prompt now asks for thorough extraction (6-18 notes afterwards).

### Between milestones 5 and 6 (2026-09-30)
Verifier
- VERIFY now returns, per sentence, an exact span copied from the cited source. Code checks it
  (`agent/verify.py`): NFKC, curly quotes/dashes folded, whitespace collapsed, at least 20 characters, and it must be a
  substring of one of the sources that sentence cites. No valid span -> unsupported (removed). A partial rewrite
  keeps its tags and must also come with a valid span. The span is stored with the sentence (`span`, `span_source`).
- Name check (on by default): named terms in the sentence (tokens with 2+ capitals, or a capital and a digit,
  e.g. QLoRA, GPT-4, NF4) must appear in the cited source text or its paper title; otherwise unsupported.
  Added because the span check alone does not catch claims moved onto the wrong paper when both papers say similar things.
- Adversarial test, `eval/verifier_adversarial.yaml` + `eval/verifier_adversarial.py` (`make eval-verifier`):
  10 real sentences from the M5 answers, each with an altered version (3 wrong number, 3 claim moved to the wrong
  paper, 2 facts from different experiments merged, 2 unsupported extensions).
  | verifier | altered caught | originals wrongly removed |
  | before (M5 verdict only, `--legacy`) | 8/10 (3 runs, stable) | 0/10 |
  | exact span only (`--no-names`) | 8/10 (2 runs) | 0/10 |
  | exact span + name check (default) | 10/10 (2 runs) | 0/10 and 1/10 |
  Both earlier versions missed #5 and #6 (wrong paper). The one false alarm was #2, where no valid span was returned.
- On all 50 real sentences from the M5 answers (`eval/verifier_real_sentences.py`): span + names kept 50/50;
  span only kept 48, rewrote 1, and removed 1 (a correct FlashAttention "2-4x" sentence).

Speed
- Thinking levels: gemini-3.5-flash and flash-lite both accept minimal, low, medium, and high; minimal returns no
  thought tokens. WRITE now uses `models.write_thinking_level: minimal`; the other calls stay at low.
  WRITE on the same 5 prompts (`eval/bench_write.py`):
  | thinking | mean time | mean output tokens | mean cost |
  | low | 17.0 s | 2,629 | $0.0249 |
  | minimal | ~8.4 s (3.8 / 5.9 / 7.8 / 16.0 s; the 5th ran on flash-lite after 503s, 2.4 s) | ~560 | ~$0.0063 |
- CONSENSUS runs in parallel with VERIFY (both work from the draft; consensus is dropped if the deadline passes).
- quick_timeout_s = 45. The 5 questions rerun end to end: 16-29 s each, all done, $0.013-0.022 per answer,
  removed claims 0 / 5 / 0 / 1 / 3 (the stricter verifier removes more).
- 503 fallback: after 2 retries on 503 (`llm_client.fallback_after_503_retries`), that call goes to gemini-3.5-flash-lite
  and is logged ("llm model unavailable, falling back for this call"). Extension (not in the request): a 429 daily-quota
  error on gemini-3.5-flash also falls back to flash-lite for that call. The API key is on the free tier:
  gemini-3.5-flash allows 20 requests per day, and that limit was reached during this work, so later runs wrote with
  flash-lite. flash-lite allows 15 requests per minute; the client now honors the server's retryDelay.

Ranking
- Name-match boost: if a paper title's part before a colon matches (case-insensitive) a term from the question or the
  rewritten queries, `retrieval.name_match_boost` (0.1) is added after rerank; counted in `papers.name_matched`.
- rerank_min_score = 0.05; the top `retrieval.min_papers_kept` (3) papers are always kept.
- Reranker A/B (`eval/ab_reranker.py`, 7 questions: the 5 test questions + QLoRA + speculative decoding):
  | model | median rerank latency | GPU weights | GPU peak |
  | bge-reranker-base | 352 ms | 546 MB | 1,426 MB |
  | BAAI/bge-reranker-v2-m3 | 902 ms | 1,084 MB | 2,654 MB |
  v2-m3 puts the paper the question is about at the top where base did not: Huang's sensitivity proof #1 at 0.760
  (base 0.094); QLoRA #1 at 0.719 (base 0.002, #20); the speculative-decoding landmark #6 (base #8). Other lists
  were similar. Switched to v2-m3: clearly better on "about X" questions, at +550 ms and +1.2 GB GPU peak, which fits.
  Worker RAM ~1.0 GB after the switch.
- Also fixed: TEI parsing and chunking ran on the event loop and blocked the worker during deep-mode fetches
  (progress stalled; FETCH overran its 90 s wait to 111 s). They now run in threads.

### Milestone 6 (done 2026-09-30, approved)
- Deep mode: REWRITE -> FIND_PAPERS -> FETCH -> CHAIN -> PASSAGES -> READ -> WRITE -> VERIFY || CONSENSUS.
  - FETCH: selected papers without full text are enqueued as urgent; the run waits on `fetch:done:{id}` up to
    fetch_wait_s (90 s), bounded by the run deadline, and streams progress. Papers still missing use their abstract.
  - CHAIN: in-corpus references of the top 5 papers (up to 100) are reranked; up to 3 scoring at least the lowest
    selected paper are added (`via_chain`), shown as "found via references of [n]".
- Pinned papers (`POST /threads/{id}/pin`, `DELETE /threads/{id}/pin/{arxiv_id}`) are always selected.
  "Ask about this paper" in the reader starts a thread limited to that paper and its in-corpus references;
  follow-ups inherit the scope.
- Live progress: `GET /runs/{id}/events` (SSE with heartbeats); the UI falls back to polling.
- Cancel: `POST /runs/{id}/cancel` sets a flag checked between steps and during the fetch wait.
- Resume: a per-run Redis lock; on worker start, `recover()` re-enqueues queued runs and resumes running ones from
  their saved steps (verify/consensus/error rows are redone). A resumed run gets at least `agent.resume_grace_s`
  (60 s) more, because restart plus model loading takes ~90 s.
- Mode toggle (Quick/Deep) on the ask box and for follow-ups.
- Fetcher: failed metadata batches are retried at the back of the bulk queue, up to 3 rounds; arXiv API queries
  give up after 2 attempts.
- Tests (topics with no cached full text):
  | question | status | total | cost | FETCH | selected: full text / abstract | CHAIN added |
  | watermarking text generated by LLMs | timed_out | 127.5 s | $0.0104 | 12 requested, 7 ready, 5 pending, waited 111.2 s (before the event-loop fix) | 7 / 8 | 3 |
  | sunflower conjecture | done | 122.6 s | $0.0377 | 12/12 ready in 83.1 s | 12 / 1 | 1 |
  - CHAIN, run 1 (100 candidates): 2401.13927 via 2402.14904; 2306.17439 via 2502.02787; 2308.00221 via 2402.14904.
    Run 2 (23 candidates): 2401.01800 via 2408.04165.
  - Run 1 wrote on flash-lite (the daily flash quota was used up). Run 2 removed 2 claims.
  - Cancel during FETCH: stopped within 2 s. Resume: worker restarted mid-FETCH; the run continued from step 3
    with no duplicate steps. Before the grace budget it was already past its deadline, so FETCH/CHAIN were skipped
    (timed_out, 167 s). The "max retries 1 exceeded" log line is arq refusing to rerun the original job; the
    `resume:` job is the one that continues.
- Open issues:
  - Deep runs take 120-130 s, and the answers treat preprints as settled: the sunflower answer says the conjecture
    "was proven", citing preprint 2212.13609.
  - Free-tier Gemini quota (20 flash requests per day) limits testing; quality numbers after the limit is hit are flash-lite.

### Between milestones 6 and 7 (2026-09-30)
Checks: `make lint test` green after the Docker restart (ruff, mypy, 104 tests); milestone 6 pushed (1cbee52).

Docker hang = out of memory
- Docker's VM log shows a global OOM at 11:57:17 UTC, the moment Docker stopped answering: the kernel killed GROBID's
  java (anon RSS 3.66 GB, well above its 2.5 GB heap: off-heap memory adds ~1 GB).
- VM memory (7.61 GiB, 2 GiB swap), sampled every ~3 s:
  | state | used | available (min) | swap free | GROBID | worker | postgres |
  | idle | 4.60 GiB | 3.01 GiB | 0 (swap already full) | 2.0 GiB | 1.1 GiB | 1.0 GiB |
  | deep run (8 fetches) | 7.06 GiB | 0.55 GiB | ~0 | 3.56 GiB peak | 1.57 GiB peak | 1.0 GiB |
  A deep run with fetches brings the VM within ~0.5 GiB of another OOM; GROBID's growth during parsing is most of it.
  Options (not applied, need a decision): raise the WSL VM memory (host has 16 GB), lower GROBID -Xmx, or give
  GROBID a container memory limit so a spike kills only GROBID (it restarts) instead of freezing Docker.

Preprints
- Each source's status is passed to WRITE (with first author and title on every note) and to VERIFY:
  `source_status()` = "peer-reviewed: <venue or journal_ref>", "landmark" (optionally with venue), or "preprint (year)".
  WRITE rule: preprint claims must be attributed and never stated as settled fact; one preprint cannot resolve an
  open problem. VERIFY rule: a preprint claim stated as fact is "partial" and is rewritten with attribution.
  Note: most of the corpus has no venue, so most sources are preprints and answers now attribute most claims.
- Name check also accepts author last names and the venue from the status (so "Fukuyama claims ...", "a NeurIPS
  paper by Hu ..." pass).
- Additions to VERIFY found while testing (not in the request):
  - A rewrite is checked once more and kept only if the re-check says "supported" with a valid span. Without it, a
    wrong-paper sentence was "fixed" into another wrong claim (case 4: TaylorShift's method credited to TLinFormer).
  - Rewrites whose $...$ math is not in the original sentence or a cited source are removed ("rewrite altered
    math"): flash-lite garbles LaTeX in JSON rewrites ("\log" came back as `",'log`). Echoed "[c:12]" tags are stripped.
- Adversarial test now has 12 cases (+2 preprint_as_fact: the sunflower preprint and Unigram-Watermark stated as fact).
  Final prompt, 3 runs: caught 12, 11, 11 of 12 (#7 or #12 missed once each); originals wrongly removed 0, 0, 2;
  preprint cases rewritten with attribution 0/2, 2/2, 1/2 (the others: removed once, missed once). flash-lite is not
  deterministic here; the earlier variant scored 12/12 with 0 false alarms twice but removed 7 of 50 real sentences.
- The 50 real sentences from the M5 answers: 27 supported, 20 rewritten (mostly adding preprint attribution), 3 removed.
- Sunflower question re-run (deep): now "A 2022 preprint by Fukuyama claims that the sunflower conjecture has been
  proven." and "A 2026 preprint by Ge claims ... proved for families of bounded VC-dimension." 4 sentences removed
  (2 by the math guard). Known gap: "|F| > ck^4 m" (source: (ck^4)^m) passed; math is not compared exactly because
  full-text chunks from GROBID are plain text without LaTeX.

Deep mode
- `agent.deep_max_fetch: 8` (only the top 8 selected papers are fetched; FETCH records `beyond_max_fetch`) and
  `deep_timeout_s: 180`.
- Uncached deep question ("How does randomized smoothing provide certified robustness against adversarial
  examples?"): done in 133.6 s, $0.0168. FETCH: 8 requested, 2 full text within the 90 s wait (99.3 s), 6 finished
  later; 4 more selected papers beyond the top 8 used abstracts. CHAIN added 2. Final: 14 papers, 2 full text / 12
  abstract. PDFs downloaded in <30 s; GROBID took 6-18 s per paper, but parse jobs took ~50 s and chunking 45-70 s
  each, with the VM out of swap and 0.55 GiB free (see above).

Models
- Every answer shows which model wrote it (`written_by`, from `llm_calls`, so it is right after a fallback or a
  resume) and `write_fallback`; the UI marks a fallback in amber. The 503 and daily-quota 429 fallbacks stay.
- The randomized-smoothing run still fell back on a 429 daily-quota error (free tier), so billing was not active for
  this key yet at 15:17 UTC.

### Milestone 7 (done 2026-09-30, approved)
- Paper summaries: `GET /papers/{id}/summary` (llm_light, JSON: problem, method, datasets, results), cached in
  `paper_summaries`. Input: title + abstract + up to ~24k chars of chunks picked by section priority
  (introduction, conclusion, method, experiments), in document order. A summary made from the abstract is
  regenerated once the paper has full text; `?refresh=true` forces it. 6 summaries: 5.3-7.5 s, ~4.4k input /
  ~240 output tokens, ~$0.0019 each; cached: 0.65 s. Known issue: flash-lite still lists models (RoBERTa, GPT-3)
  under "datasets" despite the prompt saying not to.
- Citation graph: `GET /papers/{id}/graph?hops=1|2` (references and cited-by, stubs included and never expanded,
  40 neighbors per node and direction, highest-cited first, 200 nodes max) and `GET /runs/{id}/graph` (the run's
  papers, citations among them, and up to 15 references shared by two or more of them). LoRA: 47 nodes, 91 edges
  in 0.4 s. UI: canvas force layout, node size = in-corpus citations, color = year (stubs gray), ring = this
  paper / papers used for the answer, hover tooltip, click = select + expand (references and citing papers), legend
  says citations are counted within this corpus. Deviation from the spec: `react-force-graph-2d` could not be added.
  The npm registry kept failing on `next`'s metadata (pnpm 12 re-verifies the whole lockfile on `pnpm add`; 4
  attempts), so the graph uses a small built-in layout (`components/citation-graph.tsx`), which is enough for a few
  hundred nodes. No new dependencies.
- `GET /papers/{id}/references`: parsed bibliography with resolution (in-corpus links) and in-corpus citers.
- Paper page: summary card, Save to collection, tabs Full text / References (n) / Cited by (n) / Citation graph.
- Runs: tabs Answer / Citation graph; consensus bars under the answer (per central claim: papers supporting,
  contradicting, unrelated; a paper counts as contradicting if any of its passages does); Save to collection.
- Collections: `GET/POST /collections`, `GET/PATCH/DELETE /collections/{id}`, `POST /collections/{id}/items`
  (paper or run, duplicates ignored), `DELETE /collections/{id}/items/{item_id}`, `GET /collections/{id}/bibtex`
  (saved papers, then every paper cited by saved answers, deduplicated). `GET /collections?arxiv_id=|run_id=` marks
  collections that already hold the item. Pages `/collections` and `/collections/[id]` (rename, describe, remove,
  delete, export). Nav link added. Test collection "Certified robustness" (1 answer + 1 paper -> 7 BibTeX entries).
- Fix: the `fulltext_citations` badge was only refreshed by `make score`, so 1,380 papers showed stale counts (LoRA:
  badge 3, actual 6). `refresh_counts` now updates the badge with the column; existing rows backfilled.
- Memory: the frontend image build OOM-killed GROBID (4th kill today, 16:05 UTC). Builds were done with GROBID
  stopped (`docker compose stop grobid`, then `start`). The memory decision from before milestone 7 still stands.
- Tests: 116 (graph assembly, shared references, summary input selection, collection BibTeX order, item validation).

### Between milestones 7 and 8 (2026-09-30)
Decisions: the canvas graph stays; the Gemini key stays on the free tier permanently (no billing).

Memory
- `.wslconfig` memory=10GB, swap=4GB: the Docker VM now sees 9.71 GiB (10,182,848 kB) + 4 GiB swap.
- GROBID: `mem_limit: 4.5g`, `restart: unless-stopped`, still `-Xmx2560m`.
- After the WSL restart the api and frontend ports were not forwarded to the host (curl got no response); recreating
  the two containers fixed it.
- 10-paper test (uncached 2025 cs.LG/CL/CV papers): 10/10 GROBID, 0 PyMuPDF fallbacks, 1 min 54 s. GROBID not
  killed (0 restarts). Idle before: 5.53 GiB used, 4.19 GiB available, no swap. Peak: 7.88 of 9.71 GiB used
  (1.83 GiB available), swap 0.09 GiB; GROBID 4,541 MiB, worker 1,661 MiB, postgres 1,338 MiB.
  Note: GROBID peaked at 4,541 and (next test) 4,601 MiB, right at its 4,608 MiB limit; it was not killed, but there
  is almost no headroom. If it gets killed during fetches, it restarts and the paper falls back to PyMuPDF.
- Deep mode full text:
  | question | full text of top 8 | FETCH wait | total |
  | randomized smoothing (re-run; all 8 cached by the earlier run) | 8/8 | 0 s | 51.3 s |
  | membership inference attacks (uncached, before the fixes below) | 4/8 | 91.0 s | 132.0 s |
  | neural radiance fields (uncached, after fix 1) | 7/8 | 124.3 s (overran) | 152.8 s |
  | GNN over-smoothing (uncached, after fixes 1+2) | 8/8 | 55.8 s | 82.9 s |
  Peak VM memory in the last run: 7.28 of 9.71 GiB, GROBID 3.8 GiB, no GROBID restart.
- Fixes found by these runs (not in the request):
  1. A paper became `ready` (and FETCH was notified) only after its references were resolved, which holds the
     global citation-graph lock, so papers finishing together waited on each other: chunking jobs took 25-74 s
     though parsing and embedding were done. Now a paper is marked ready and waiters are notified right after its
     chunks are embedded; references are resolved afterwards. A Redis set `refs:pending` keeps papers whose
     resolution has not finished; worker startup re-enqueues them (`resolve_refs_job`).
  2. Up to 8 resolutions waiting on that lock each held one of the worker's 6 DB connections, which stalled the
     run's own queries (the 90 s FETCH wait ran to 124 s). Resolution now queues on an in-process lock before it
     takes a connection.

Free tier
- WRITE model comparison (`eval/compare_write_models.py`, same notes, current prompt, current VERIFY). The flash
  daily quota ran out after the first question, so flash's saved answers from the earlier test runs were scored for
  the other questions (older prompt without source status); the diffusion run had been written by flash-lite
  (fallback), so that row compares two flash-lite drafts.
  | question | flash: kept/sentences, removed, words, papers | flash-lite: kept/sentences, removed, words, papers |
  | attention (flash, current prompt) | 13/19, 6, 303, 7 | 16/16, 0, 359, 11 |
  | attention (flash saved answer) | 17/19, 2, 374, 11 | |
  | sensitivity conjecture | 12/13, 1, 242, 2 | 10/11, 1, 254, 2 |
  | diffusion vs GANs (both flash-lite) | 15/15, 0, 365, 10 | 8/15, 7, 210, 7 |
  | speculative decoding | 13/16, 3, 304, 11 | 11/12, 1, 241, 10 |
  | LoRA | 12/12, 0, 235, 11 | 11/11, 0, 237, 8 |
  Means over the 5 rows: flash 13.8 kept sentences, 304 words, 9.0 papers, 1.2 removed; flash-lite 11.2, 260, 7.6,
  1.8. Judgment: close. Same content and structure from the same notes, both attribute the preprint proof claim;
  flash is somewhat fuller (+2.6 sentences, +1.4 papers). flash-lite's weakness is reliability: one of 7 flash-lite
  drafts came back with no source tags at all (everything removed) and one kept only 8 of 15 sentences.
  Decision: `models.write_model: gemini-3.5-flash-lite` (default), flash stays selectable. WRITE now writes once more
  when a draft is mostly untagged (fewer than half of its sentences carry a valid tag).
- Response cache bug (found here): an answer from the fallback model was cached under the requested model and a
  cache hit reported the requested model, so `written_by` and evaluations could label flash-lite text as flash.
  Now only the requested model's own answers are cached, structured results report the model that answered, and the
  cache key prefix is `llmcache:v2:` (old entries ignored).
- Proactive rate limit (`core/ratelimit.py`): a sliding 60 s window per model in Redis, shared by the api, worker and
  eval scripts, taken before every API request (cache hits excluded): `llm_client.rpm_limits` flash-lite 14/min,
  flash 9/min. A per-model daily counter (Pacific-time day) is kept; a 429 daily-quota error now logs the quota value
  from the error and the day's count. The flash-lite daily limit was not hit today (the most flash-lite calls logged
  in a Pacific day so far: ~90 in the DB, plus uncounted eval calls).

Preprint wording
- Attribution is now required only for strong claims: proofs / theorems / guarantees ("proven", "guaranteed",
  "provably"), resolving open problems, state-of-the-art / best / first results, and claims about the world beyond
  the paper's own method. Describing what a paper proposes or how its method works needs no hedge (WRITE and VERIFY).
- Span matching ignores `$` math delimiters (spans often drop them; this caused a false "no valid span").
- Adversarial test, 3 runs: caught 11, 11, 11 of 12 (case 7, 33B/65B merged, missed every time); originals wrongly
  removed 0, 0, 1; preprint cases rewritten with attribution 2/2, 2/2, 0/2 (the third run removed both instead).
  Method-description originals (TaylorShift, element-wise attention) are no longer rewritten.
- 50 real sentences: 44 supported, 3 rewritten, 3 removed (previous rule: 27 / 20 / 3). One rewrite attributes QLoRA's
  "first method ..." to "a 2024 preprint": QLoRA has no venue in the metadata, so it counts as a preprint.

### Milestone 8 (in progress 2026-09-30: built; full eval waits for approval of the question list)
Nightly job (spec 16)
- Worker cron 02:00 UTC `nightly_job` queues an OAI-PMH harvest (`cs`, `math`) at the front of the fetcher's bulk
  queue; the fetcher (the only arXiv client) pages through ListRecords at 1 request / 3.1 s, handles urgent PDF
  fetches between pages, converts records to the Kaggle shape (`ingest/oai.py`) and upserts them through the same
  metadata pass (window/category filter, signals). Then `post_harvest_job`: foundational flags (cs/math papers older
  than the window with >= 20 full-text citations; `discovered` ones join the pipeline), abstract embeddings, scores and
  PageRank. `make nightly` runs it now. `oai:last_harvest` keeps the repository date of the last successful harvest;
  the next `from` is one day earlier.
- Live test: BLOCKED. arXiv's OAI endpoint answers Identify, ListSets and ListMetadataFormats (200, XML), but every
  ListRecords request gets HTTP 406 with an empty body from its Varnish layer, whatever the Accept or Accept-Encoding
  header or `from` date (5 diagnostic requests, all spaced >= 3.1 s). Also: the repository reports its date as
  2026-09-17, 13 days behind this machine; a `from` later than that is refused, so `from` is now clamped to the
  repository date (read with Identify first). The harvest code is tested against a sample response
  (`tests/fixtures/oai_listrecords.xml`). The nightly job logs the failure and tries again the next night.
- `post_harvest_job` on its own: 104 s; 7 foundational papers; 0 abstracts pending; scores recomputed.

Feeds (spec 10, 14, 16)
- `GET/POST /feeds`, `GET/PATCH/DELETE /feeds/{id}`, `GET /feeds/{id}/digests`, `POST /feeds/{id}/digests`
  (build now). Weekly cron Monday 03:00 UTC builds a digest per feed and ISO week (`2026-W40`).
- Digest: papers submitted in the last 7 days; candidates by abstract-embedding similarity to the feed query (200) and
  keyword matches; the best 60 by similarity are scored with the cross-encoder (as in search); score = relevance +
  0.05 if a keyword matches; kept if >= 0.3, top 10; summarized with flash-lite (overview + one line per paper,
  worded as "a paper proposes/reports").
  Calibration on a test feed ("Efficient LLM inference"; keywords speculative decoding, KV cache, quantization):
  embedding similarity alone kept 10 papers, mostly off-topic quantization papers (image compression, speech);
  keyword-only inclusion was the main cause. With the reranker gate: 4 papers (NebulaSD 0.95, MILO 0.87, FlashLoop
  0.52, GHOST-Q 0.35). 22 s per digest.
- `/feeds` page: create, build now, latest digest with relevance and keyword matches, delete.

Eval (spec 17)
- `eval/questions.yaml`: 40 questions (30 quick, 10 deep) with expected papers, all verified in the corpus with an
  abstract embedding (state ready or signals_done); papers only known from reference lookups (`discovered`, no
  embedding: e.g. HNSW, MAML, Lottery Ticket, SimCLR) were left out.
- `eval/run_eval.py` (`make eval`):
  - Retrieval: section-11 retrieval with the question as the only query, so no Gemini calls; recall@10, MRR,
    passage hit@10, latency p50/p95.
  - Ablations, patched in the eval process only: `--no-keyword`, `--no-vector`, `--no-rerank`, `--ablations a,b,c`,
    `--chunk-size N` (re-chunks full-text papers from parsed/ into schema `eval_csN`, read via search_path),
    `--embedding M` (builds `eval_emb_*` abstract + chunk embeddings once, resumable; hours for the whole corpus).
  - `--answers`: the pipeline through the API (with REWRITE), response cache on (TTL raised to 30 days so reruns
    are free): latency p50/p95 per mode, cost, removed-claims rate, writer fallbacks, recall@10 of the pipeline's
    selection, and `results/citation_sample_<ts>.csv` of (sentence, cited passage) pairs for manual precision.
  - Writes `eval/results/<ts>.json` and `.md` (gitignored).
- Smoke test (4 questions, retrieval only; not the eval): baseline recall@10 0.50, no-keyword 0.75, no-vector 0.62,
  no-rerank 0.38. The Transformer question missed "Attention Is All You Need" at baseline. Full numbers after
  approval of the questions.

README: rewritten with architecture and pipeline diagrams (mermaid), setup, operations, free-tier notes, eval usage,
a results table to fill after the full eval, and a demo video placeholder.

Also: the frontend image build no longer needs GROBID stopped (10 GB VM, GROBID capped): built with GROBID running,
no kill.

### Milestone 8, continued (2026-09-30)
Questions
- Every question tagged `landmark` (an expected paper is landmark/foundational; checked against the DB), `named`
  (question contains the method/model name, a distinctive title phrase, or the named conjecture), `field` (cs/math;
  checked). 8 named-landmark ML questions (PagedAttention, LLM.int8(), ZeRO, DPO, DPR, ViT, GAT/GCN, PGD) replaced by
  5 math + 3 non-ML CS questions with recent (2022+), non-landmark expected papers, described without naming them:
  union-closed sets (2211.09055), moving sofa (2411.19826), sphere packing lower bound (2312.10026), bunkbed
  (2410.02545), Kakeya in 3D (2502.17655), negative-weight SSSP (2203.03456), universal optimality of Dijkstra
  (2311.11793), deterministic min-cut (2401.05627). Totals: 32 cs / 8 math, 23 landmark / 17 not, 27 named / 13 not.

Retrieval eval, 40 questions (no LLM calls; results/20260930T194204Z)
  | variant | recall@10 | MRR | passage hit@10 | p50 | p95 |
  | baseline | 0.588 | 0.577 | 0.650 | 2173 ms | 3375 ms |
  | no-keyword | 0.613 | 0.582 | 0.700 | 1983 ms | 2496 ms |
  | no-vector | 0.512 | 0.513 | 0.300 | 281 ms | 1534 ms |
  | no-rerank | 0.487 | 0.383 | 0.550 | 306 ms | 756 ms |
  By tag (recall@10 / MRR, baseline): landmark 0.65/0.70 (23) vs not 0.50/0.41 (17); named 0.63/0.58 (27) vs not
  0.50/0.58 (13); cs 0.64/0.66 (32) vs math 0.38/0.23 (8); quick 0.62/0.58 (30), deep 0.50/0.56 (10).
  no-vector: non-landmark 0.18/0.18. no-rerank: MRR drops most (0.577 -> 0.383).

Keyword channel investigation (no production change)
- No-keyword is better on exactly one question (#1 Transformer: baseline 0 -> 1.0); all other rows are identical.
- Cause: `websearch_to_tsquery` ANDs every question word, including generic ones ('transform' & 'replac' & 'recurr'
  & 'self-attent'; 'new' & 'upper' & 'bound' ...; 'reduc' & 'memori' & 'cost' & 'decod'), so it returns only 3-17
  papers that contain all of them: later papers paraphrasing the concept, never the expected original (target in
  none of the keyword lists for #1, #6, #40). For #1 its 17 hits (recurrent/Transformer hybrids) joined the
  candidates, the cross-encoder scored them 0.94-0.99, and "Attention Is All You Need" (0.741, present via the anchor
  channel) fell out of the top 12; without them it ranks 5.
- Tested fix (eval-only patch `--ablations kw-or`): OR between terms, ranked by ts_rank_cd: recall@10 0.613 (= no
  keyword), MRR 0.573, but p50 13.6 s / p95 25.0 s (ranking many OR matches over 820k abstracts). Not viable as is.
- Proposed (not applied; needs your decision): drop generic question words from the keyword query and keep AND on the
  rest, or cap the keyword channel's contribution to papers that also appear in the vector channel's top 200. Either
  should be tested with the same eval. Note the effect is 1 question in 40.
- The larger cause of misses is not keyword search: for #6 (GQA) and #40 (diagonal Ramsey) the expected paper is in
  the candidates (fused rank 7 and 15) but newer same-topic papers get cross-encoder scores ~0.99 and the citation
  prior adds only ~0.01-0.05.

Answer eval (results/20260930T200908Z): 6 of 40 completed, then 34 failed: gemini-3.5-flash-lite's free-tier daily
quota is 500 requests (from the 429: quotaValue 500); earlier eval scripts that day had used most of it (they make
their own client, so the shared counter saw only 167). Completed runs: quick median 16 s (max 51 s), deep 111 s,
$0.013 per answer, 1.8% claims removed, all written by flash-lite, no fallbacks.
- Each failed run spent 3-4 min retrying the daily-quota 429. Fixed: a daily-quota 429 with no fallback left raises
  `LLMQuotaExhausted` at once. The answer eval now stops after 2 failed runs in a row.
- Citation sample (55 pairs, all from the 6 runs, checked by hand): 55/55 claims supported by the cited passage, all
  numbers correct; 2 imprecise attributions (RoPE's mechanism called "the approach described by Li"; one paper's
  softmax/sigmoid choice stated as general); 2 sentences leaked the status label ("the Su et al. landmark papers")
  -> WRITE prompt now says the labels are never written into the answer; 5 background statements hedged as a
  preprint's claim (over-cautious, not wrong).

Preprint status bug
- QLoRA ("a 2024 preprint by Dettmers"): the data was right (the lookup returns "landmark"); the verifier model wrote
  the attribution anyway, inventing the year, and the re-check accepted it. Fixes: (1) a rewrite that adds "preprint"
  wording while none of the cited sources is a preprint is rejected and the original sentence goes to the re-check
  (kept as supported if it passes); (2) the VERIFY label says "NOT a preprint" for landmark/foundational/peer-reviewed
  sources; (3) real bug found on the way: source_status ignored is_foundational, so foundational papers without a
  venue were "preprint" -> now "foundational". Tests added.

Jobs
- Nightly OAI-PMH harvest disabled: `jobs.oai_harvest_enabled: false` (code kept; cron is a no-op).
- Weekly Kaggle snapshot job (`ingest/weekly.py`, cron Mondays 00:30 UTC, `make snapshot`): download (skipped when
  current); stop if the zip is unchanged; else process only records with update_date on or after the newest one of
  the last ingested snapshot, upsert through the metadata pass, upsert new records into arxiv_catalog, then
  post-harvest (foundational, embeddings, scores). First run (no previous state): 3,182,867 lines in 26 min, 0
  inserted / 0 updated (same snapshot as the DB), newest update_date 2026-09-25, foundational 8 (+1), 1,495 scores
  updated. Second run: "snapshot unchanged" in 10 s.
- GROBID mem_limit 5g.

README: results tables, tag breakdown, keyword finding, partial answer results, Known limitations section, weekly job.
