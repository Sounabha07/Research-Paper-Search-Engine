# Progress

Milestones from MASTER_PROMPT.md section 21. Each one stops for approval before the next.

- [x] 1. **Skeleton**: Compose (postgres, redis, grobid, api, worker, fetcher, frontend), config loading, migrations for all tables, LLM client with model check, health endpoints, Makefile.
- [ ] 2. **Metadata**: Kaggle stream + filter, landmark validation, tier 1 papers, metadata score, abstract embeddings + indexes, counts printed. `/admin` page.
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
- `llm_prices` in config.yaml is null, so `llm_calls.cost_usd` is logged as null until real prices are filled in.
- Postgres and Redis are not published on host ports (host port 5432 was unavailable). Use `docker compose exec postgres psql -U arxiv arxiv`.
- Redis persists to `DATA_DIR/redis` (not listed in spec section 4, but keeps all data under DATA_DIR).
- HNSW indexes exist from migration 0001. Milestone 2 may drop and rebuild them around the bulk load for speed.
- The fetcher currently only runs the rate limiter and a heartbeat. Queue consumption comes in milestone 3.
