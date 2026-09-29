# Build: arXiv Research Engine

A local research tool. The user asks a question, the system finds relevant arXiv papers (CS + math, last 5 years), reads them, and writes an answer where every sentence cites the exact paper and passage it came from. Unsupported sentences are removed.

Repository: `https://github.com/Sounabha07/Research-Paper-Search-Engine.git`. If you are not already inside a clone of it, clone it and work there. Check what the repo already contains before creating files, keep any existing README or license and build on them. Use the layout in section 3 at the repo root. Make small, focused commits with clear messages as you go. At the end of each milestone, commit, then push to `main` only after I approve the milestone.

Build it milestone by milestone (see end). Stop after each milestone, show how to run and verify it, and wait for approval before continuing. Do not invent API parameters. Everything external you need is specified below. If something here fails against the live service, report it instead of guessing.

---

## 1. Constraints

- Single user, no auth, no accounts.
- Runs locally on a laptop: 16GB RAM, NVIDIA RTX 4050 (6GB VRAM). Host may be Windows with Docker Desktop on WSL2. Everything runs in Docker Compose.
- Not deployed anywhere. The repo is public on GitHub, data never goes in the repo.
- Target steady-state disk use: ~13GB, growing ~0.3GB per 1,000 papers fetched. PDFs are never kept after processing.

## 2. Stack

- Backend: Python 3.11, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, Pydantic v2
- Workers: `arq` on Redis (except the arXiv fetcher, see 6.3)
- DB: Postgres 17 with pgvector, image `pgvector/pgvector:pg17`
- Redis 7
- PDF parsing: GROBID, image `grobid/grobid:0.9.1-crf` (fall back to `0.9.0-crf` if the tag is missing), port 8070. PyMuPDF as fallback parser.
- Local models: `sentence-transformers` on CUDA (CPU fallback)
- LLM: Google Gemini via the `google-genai` Python SDK
- Frontend: Next.js (App Router, TypeScript), Tailwind, shadcn/ui, `react-force-graph-2d` for the citation graph
- Package management: `uv` for Python, `pnpm` for frontend

## 3. Repo layout

```
backend/
  app/            # FastAPI app, routers, schemas
  core/           # config, db, llm client, logging
  ingest/         # pipeline stages
  retrieval/      # search, fusion, rerank
  agent/          # answer pipeline, verifier
  workers/        # arq worker entrypoints, fetcher loop
  migrations/
  tests/
frontend/
seeds/
  landmark_papers.yaml   # provided, do not regenerate
eval/
  questions.yaml
  run_eval.py
config.yaml
docker-compose.yml
Makefile
.env.example
README.md
```

## 4. Data directory

All data lives under `DATA_DIR` (from `.env`), bind-mounted into containers. `data/` is in `.gitignore`.

```
DATA_DIR/
  postgres/    # Postgres data
  parsed/      # GROBID TEI output, gzip, one file per paper: {arxiv_id}.tei.xml.gz
  pdf_tmp/     # temporary PDFs, deleted after processing
  models/      # HF model cache (HF_HOME)
  raw/         # Kaggle metadata zip
  backups/
```

## 5. config.yaml (all tunables live here)

```yaml
corpus:
  years_back: 5                      # by v1 submission date
  categories_prefixes: ["cs.", "math."]
  landmarks_file: seeds/landmark_papers.yaml
  foundational_min_fulltext_citations: 20   # older papers cited by this many full-text papers join tier 1
  stub_min_corpus_citations: 3
  warm_default_top_n: 30             # papers fetched by `make warm`
score_weights:                       # metadata score, used as the retrieval prior and to rank fetch candidates
  venue_accepted: 0.55
  code_available: 0.25
  recency: 0.20
venue_patterns:                      # matched case-insensitively in journal-ref and comments
  - NeurIPS|NIPS|ICML|ICLR|COLT|AISTATS|UAI|AAAI|IJCAI|KDD|WWW|SIGIR|WSDM
  - CVPR|ICCV|ECCV|ACL|EMNLP|NAACL|COLING|TACL|TMLR|JMLR
  - OSDI|SOSP|NSDI|EuroSys|ATC|MLSys|ASPLOS|ISCA|MICRO|SIGMOD|VLDB|ICDE|PODC|SPAA|PLDI|POPL|SIGCOMM
  - STOC|FOCS|SODA|ICALP|CCC|ITCS
  - Annals of Mathematics|Inventiones|Journal of the AMS|J\. Amer\. Math\. Soc|Acta Mathematica|Duke Math|Publ\. Math\. IHES|Geometry & Topology|Forum of Mathematics
venue_context_words: "accepted|to appear|published|camera[- ]ready|proceedings|in press"
chunking:
  target_tokens: 350
  max_tokens: 450
  overlap_tokens: 50
  min_tokens: 50
models:
  embedding: BAAI/bge-small-en-v1.5   # 384 dims
  embedding_query_prefix: "Represent this sentence for searching relevant passages: "
  reranker: BAAI/bge-reranker-base
  llm_main: gemini-3.5-flash
  llm_light: gemini-3.5-flash-lite
retrieval:
  paper_keyword_k: 100               # per query, over title + abstract
  paper_vector_k: 100
  rerank_top_n: 50                   # merged candidates sent to the reranker
  chunk_keyword_k: 50                # within the selected papers only
  chunk_vector_k: 50
  rrf_k: 60
  rerank_min_score: null             # tune with eval
  hnsw_ef_search: 100
  prior_boost_max: 0.05              # composite-score boost after rerank, capped
agent:
  max_queries: 3                     # search queries generated from the question
  papers_for_answer: 12              # top N papers used for the answer (10 to 15)
  passages_for_answer: 25
  max_passages_per_paper: 3
  fetch_wait_s: 60                   # deep mode: max wait for full-text fetches
  quick_timeout_s: 20
  deep_timeout_s: 120
  max_notes: 50
  verify_batch_size: 5
  consensus_claims: 3
  chain_from_top_papers: 5            # deep mode: follow references of these papers
  chain_max_added: 3
  thread_full_turns: 3               # turns passed verbatim, older ones summarized
  thread_carryover_boost: 0.05
llm_client:
  max_concurrency: 4
  retries: 5
  backoff_base_s: 1
  backoff_max_s: 30
  cache_ttl_days: 7
arxiv:
  min_interval_s: 3.1
  user_agent: "arxiv-research-engine/0.1 (mailto:${CONTACT_EMAIL})"
```

## 6. External APIs (use exactly these)

### 6.1 Kaggle arXiv metadata (bulk metadata, one-time)

- Download: `kaggle datasets download -d Cornell-University/arxiv -p $DATA_DIR/raw`. Auth from env: use `KAGGLE_API_TOKEN` if set (new-style token, current Kaggle CLI), else `KAGGLE_USERNAME` + `KAGGLE_KEY` (legacy). Install the latest `kaggle` package. Gives a zip containing `arxiv-metadata-oai-snapshot.json`, a JSONL file, one paper per line, covering all of arXiv. Stream it from the zip, never fully unzip or load into memory.
- Fields per line: `id`, `submitter`, `authors`, `title`, `comments`, `journal-ref`, `doi`, `report-no`, `categories` (space-separated string, primary first), `license` (URL or null), `abstract`, `versions` (list of `{version, created}`, `created` like `"Mon, 2 Apr 2007 19:18:42 GMT"`), `update_date`, `authors_parsed` (list of `[last, first, suffix]`).
- Submission date = `versions[0].created` (v1). Never use `update_date` for the window.
- Updated weekly. The nightly OAI-PMH job covers new papers between snapshots.

### 6.2 arXiv OAI-PMH (nightly incremental metadata)

- Base URL: `https://oaipmh.arxiv.org/oai`
- Request: `GET ?verb=ListRecords&metadataPrefix=arXiv&set=cs&from=YYYY-MM-DD` (repeat with `set=math`). Paginate with `?verb=ListRecords&resumptionToken=...` (resumption requests take only the token).
- Namespaces: envelope `http://www.openarchives.org/OAI/2.0/`, record metadata `<arXiv>` in `http://arxiv.org/OAI/arXiv/` with `id`, `created`, `updated`, `authors/author(keyname, forenames)`, `title`, `categories` (space-separated, primary first), `abstract`, `license` (may be absent: treat as arXiv default license).
- `from` filters by datestamp (last modified), so filter by `created` locally for new papers.
- Flow control: HTTP 503 with `Retry-After`, honor it. Error code `noRecordsMatch` means empty result, not failure.
- Rate: see 6.4.

### 6.2b arXiv query API (ID lookups only)

- `GET https://export.arxiv.org/api/query?id_list=ID1,ID2,...&max_results=100` returns Atom XML (namespace `http://www.w3.org/2005/Atom`, arXiv extensions `http://arxiv.org/schemas/atom` for `arxiv:primary_category`, `arxiv:comment`, `arxiv:journal_ref`). Entry `id` is `http://arxiv.org/abs/{id}v{n}`, `published` is the v1 date.
- Use it only to fill metadata for arXiv ids referenced but missing from the DB (up to 100 ids per call). Never use it for bulk harvesting (Kaggle + OAI-PMH do that).
- Goes through the fetcher and its rate limit like all arXiv traffic.

### 6.3 arXiv PDFs

- URL: `https://export.arxiv.org/pdf/{arxiv_id}` (latest version).
- Send the `User-Agent` from config.
- On 429/503 honor `Retry-After`, else exponential backoff.

### 6.4 arXiv rate limit (hard rule)

arXiv terms: at most one request every 3 seconds, single connection, across all machines. Therefore ALL arXiv traffic (OAI-PMH and PDFs) goes through ONE process: the `fetcher` service, a plain asyncio loop (not arq) that:
- Pops jobs with `BLPOP fetch:urgent fetch:bulk` (urgent always drains first). Urgent = fetches for research runs and the "Get full text" button. Bulk = landmark prefetch and `make warm`.
- Enforces `min_interval_s` between any two requests, globally, via a single in-process limiter.
- Publishes completion to Redis pub/sub channel `fetch:done:{arxiv_id}` so waiting research runs can continue.

### 6.5 Citations (no external citation API)

No Semantic Scholar, no OpenAlex. Citation edges are built from our own parsed papers:
- For every full-text paper, take its GROBID bibliography (6.6).
- Resolve each reference to a corpus paper by, in order: (1) an arXiv id found in the reference (`<idno type="arXiv">`, or regex `(\d{4}\.\d{4,5})(v\d+)?` / old style `[a-z\-]+(\.[A-Z]{2})?/\d{7}` in the raw text), (2) exact match on normalized title (lowercase, strip punctuation and whitespace) against `papers.title_norm`, (3) trigram similarity ≥ 0.9 on `title_norm` plus matching year ±1.
- Unresolved references with a title become stubs only if cited by at least `stub_min_corpus_citations` full-text papers.
- Edges therefore exist only from full-text papers. "Cited by" counts mean "cited by N full-text papers in this corpus", and the UI must label them that way. Counts grow as lazy fetching adds papers: update edges incrementally when a paper reaches `ready`.
- Log the reference resolution rate per batch.

### 6.6 GROBID

- Health: `GET http://grobid:8070/api/isalive`
- Parse: `POST http://grobid:8070/api/processFulltextDocument`, multipart form, file field `input`, plus form fields `consolidateHeader=0`, `consolidateCitations=0`, `segmentSentences=1`, `includeRawCitations=1`. Returns TEI XML.
- From TEI extract: section headings (`<div><head>`), paragraphs and sentences (`<p>`, `<s>`), figure captions optional. Drop the bibliography from chunk text, but parse it separately: each `<listBibl>/<biblStruct>` gives title (`analytic/title` or `monogr/title`), year (`imprint/date/@when`), any `<idno type="arXiv">`, and the raw reference string (`<note type="raw_reference">`, request it with form field `includeRawCitations=1`). These feed 6.5.
- Run up to 4 concurrent requests. If GROBID fails twice, fall back to PyMuPDF text extraction with heading heuristics (font size), and set `parser='pymupdf'`.

### 6.7 Gemini (google-genai SDK)

```python
from google import genai
from google.genai import types

client = genai.Client(api_key=GEMINI_API_KEY)
resp = await client.aio.models.generate_content(
    model=cfg.models.llm_main,
    contents=prompt,
    config=types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=2048,
        response_mime_type="application/json",   # for structured calls
        response_schema=SomePydanticModel,          # for structured calls
        thinking_config=types.ThinkingConfig(thinking_level="low"),
    ),
)
resp.text / resp.parsed
resp.usage_metadata.prompt_token_count, resp.usage_metadata.candidates_token_count
```

- On startup, call `client.models.list()` once and fail with a clear message if a configured model id is not available.
- If the API rejects `thinking_level` for the configured model, drop `thinking_config` for that model and log it once.

### 6.8 Local models

- Embeddings: `SentenceTransformer(cfg.models.embedding, device="cuda")`, `normalize_embeddings=True`. Queries get `embedding_query_prefix`, passages do not. Chunk text is embedded with a header `Title: {title} | Section: {section}\n{text}`, stored text has no header.
- Reranker: `CrossEncoder(cfg.models.reranker)`, score `(query, passage)` pairs.
- Token counting for chunking uses the embedding model's tokenizer (bge truncates at 512 tokens).
- GPU in Compose via `deploy.resources.reservations.devices` (nvidia). CPU fallback if no GPU.

## 7. Corpus rules

- Landmarks: `seeds/landmark_papers.yaml` (provided, hand-compiled). Validate every entry against the Kaggle metadata with a fuzzy title match. Log and skip mismatches, never guess. Landmarks are always in tier 1 regardless of date, and are the only papers prefetched as full text.
- Tier 1 (all papers, abstract level): every paper with any category starting with `cs.` or `math.` whose v1 date is within `years_back`, plus landmarks, plus foundational papers (older cs/math papers cited by at least `foundational_min_fulltext_citations` full-text papers, discovered from 6.5 edges and pulled from the Kaggle file in the nightly job).
- Full text is ON DEMAND ONLY. There is no bulk full-text backfill. The only upfront fetch is the validated landmark list.
- A paper becomes full text when: the agent fetches it during a run, the user clicks "Get full text", or `make warm TOPIC="..."` runs (paper-level search for the topic, enqueue the top `warm_default_top_n` not yet `ready` into `fetch:bulk`).
- Once full text, a paper stays full text.
- Print exact counts after the metadata pass, before anything expensive.

## 8. Ingestion pipeline

Per-paper state machine in `papers.state`:
`discovered → metadata_done → signals_done → queued_fulltext → pdf_fetched → parsed → chunked → embedded → ready`
plus `failed` with `fail_count` and `last_error`. `abstract_only` is the terminal state for papers whose full text failed permanently.

Rules:
- Every stage is an idempotent job keyed by paper id. Restarting any worker never duplicates work.
- Stages run concurrently (parsing and embedding proceed while fetching continues).
- Abstract embeddings are computed for all tier 1 papers in `metadata_done`, independent of full text.

PDF lifecycle:
1. Fetcher writes `pdf_tmp/{arxiv_id}.pdf`, state `pdf_fetched`.
2. Parser runs GROBID, writes `parsed/{arxiv_id}.tei.xml.gz`.
3. Chunks + embeddings committed in one transaction.
4. Delete the PDF immediately after the commit.
5. On parse failure keep the PDF for retry. After 3 failures delete it and mark `abstract_only`.
6. A janitor task caps `pdf_tmp/` at 4GB and deletes orphans older than 24h.

Re-chunking or re-embedding reads from `parsed/`, never refetches.

## 9. Scores (arXiv metadata + our own citation edges only)

Metadata score (retrieval prior and fetch-candidate ranking), from metadata alone:
- venue_accepted = 1 if `journal-ref` is present, or `comments` matches any `venue_patterns` near a `venue_context_words` term, else 0. Store the matched venue name for the badge.
- code_available = 1 if a github.com / gitlab.com URL appears in abstract or comments, else 0
- recency = percentile of v1 date within `(primary_category)`
- `meta_score = weighted sum` using `score_weights`. Landmarks get meta_score 1.

Citation score (computed once edges exist, recomputed nightly):
- fulltext_citations = number of full-text papers citing it (6.5)
- citation_velocity = fulltext_citations / max(months since v1, 1)
- pagerank = PageRank over citation edges (scipy sparse, damping 0.85)
- `score` = 0.4 × percentile(citation_velocity) + 0.3 × percentile(pagerank) + 0.3 × meta_score, percentiles within `(primary_category, v1_year)`. Papers with no citation data yet use meta_score alone. Used as the retrieval prior.

Badges stored per paper: venue name if accepted, code_available, landmark, is_survey (title/abstract contains survey, review, overview), is_benchmark (benchmark, dataset), fulltext_citations.

## 10. Database schema (Postgres)

- `papers`: arxiv_id PK, title, title_norm, authors jsonb, abstract, categories text[], primary_category, v1_date, license, journal_ref, comments, venue_accepted bool, venue_name, code_available bool, is_landmark, is_foundational, fulltext_citations int, pagerank, meta_score, score, badges jsonb, state, parser, fail_count, last_error, abstract_embedding halfvec(384), abstract_tsv tsvector, updated_at
- `chunks`: id PK, arxiv_id FK, section, position int, text, token_count, char_start, char_end, embedding halfvec(384), tsv tsvector
- `references_raw`: citing_id, position, title, title_norm, year, arxiv_id_found, raw_text, resolved_id nullable (arxiv id or stub id)
- `citation_edges`: citing_id, cited_id (arxiv id or `stub:{id}`), PK(citing_id, cited_id)
- `stub_papers`: id PK, title, title_norm unique, year, cited_by_count
- `threads`: id uuid PK, title, summary text, pinned_papers text[], created_at, updated_at
- `thread_citations`: thread_id FK, arxiv_id, number int, PK(thread_id, arxiv_id), unique(thread_id, number)
- `research_runs`: id uuid PK, thread_id FK, turn int, question, standalone_question, mode (quick|deep), filters jsonb, status (queued|running|done|timed_out|failed|cancelled), selected_papers jsonb (ranked arxiv ids with rerank scores, via_chain), cost_usd, input_tokens, output_tokens, started_at, finished_at
- `run_steps`: id, run_id FK, seq, type (rewrite|find_papers|fetch|chain|passages|read|write|verify|consensus), payload jsonb, created_at
- `evidence_notes`: id, run_id FK, claim, chunk_id FK nullable, arxiv_id, source_type (chunk|abstract)
- `answer_sentences`: id, run_id FK, seq, section, text, verdict (supported|partial_rewritten|unsupported_removed), sources jsonb (list of chunk ids / abstract refs)
- `consensus`: id, run_id FK, claim, items jsonb (arxiv_id, chunk_id, stance: supports|contradicts|unrelated)
- `paper_summaries`: arxiv_id PK, summary jsonb (problem, method, datasets, results), created_at
- `collections`, `collection_items` (paper or run), `feeds` (name, query_text, query_embedding, keywords text[]), `digests` (feed_id, period, content jsonb)
- `llm_calls`: id, run_id nullable, purpose, model, input_tokens, output_tokens, cost_usd, latency_ms, cache_hit, created_at

Indexes: HNSW `halfvec_cosine_ops` (m=16, ef_construction=64) on both embedding columns, GIN on both tsvectors, GIN `gin_trgm_ops` on papers.title_norm (enable `pg_trgm`), btree on papers(title_norm), papers(primary_category, v1_date), papers(score), chunks(arxiv_id), citation_edges(cited_id).

Full-text search: `to_tsvector('english', ...)`, query with `websearch_to_tsquery`, rank with `ts_rank_cd`.
Vector search: `SET hnsw.ef_search`, and `SET hnsw.iterative_scan = relaxed_order` for filtered queries.

## 11. Retrieval

Paper selection (titles + abstracts only):
1. For each search query: keyword top `paper_keyword_k` + vector top `paper_vector_k` over papers (title + abstract), with filters (year range, categories). Fuse with RRF (`1/(rrf_k + rank)`).
2. Merge all queries' fused lists by summed RRF score, take the top `rerank_top_n`.
3. Rerank each candidate with the cross-encoder on `(original question, title + ". " + abstract)`, drop below `rerank_min_score` if set, add the capped prior `prior_boost_max * score`.
4. Keep the top `papers_for_answer` papers.

Passage selection (within those papers only):
5. For papers that are `ready`: keyword + vector search over their chunks for the original question and each query, RRF fuse, rerank, enforce `max_passages_per_paper`, keep the top `passages_for_answer` overall.
6. Selected papers that are not `ready` contribute their abstract as a single passage.
7. Return passages with arxiv_id, section, chunk_id or `abstract`, text, scores.

## 12. Answer pipeline (fixed sequence, no agent loop)

Steps: `REWRITE → FIND_PAPERS → FETCH (deep only) → CHAIN (deep only) → PASSAGES → READ → WRITE → VERIFY → CONSENSUS → DONE`

- REWRITE (llm_light, JSON): turn the question into a standalone question (resolving pronouns and references using thread context, see 12b), then 1 to `max_queries` short search queries. Single-topic questions get one query.
- FIND_PAPERS: section 11 steps 1 to 4. Result: the top `papers_for_answer` papers, shown in the UI as a ranked list with title, year, badges, and rerank score.
- FETCH (deep mode only): push every selected paper that is not `ready` to `fetch:urgent`, in rank order. Wait on `fetch:done:{id}` up to `fetch_wait_s`, streaming progress ("fetched 4 of 9"). Papers still missing after the wait use their abstract. Quick mode skips this step.
- CHAIN (deep mode only, one hop, no loop): collect the resolved in-corpus references (6.5) of the top `chain_from_top_papers` selected papers. Rerank those referenced papers on `(standalone question, title + abstract)`. Add up to `chain_max_added` whose rerank score is at least the score of the lowest selected paper. Added papers are used at abstract level unless already `ready`. Mark them `via_chain` with the citing paper, shown in the UI as "found via references of [n]".
- PASSAGES: section 11 steps 5 to 7.
- READ (llm_light, JSON): from the passages, produce up to `max_notes` evidence notes `{claim, source_id}`. Claims must be supported by the passage.
- WRITE (llm_main, temp 0.3): answer in sections using ONLY evidence notes. Every sentence ends with source tags like `[[c:123]]` or `[[a:2106.09685]]`. Sentences without tags are invalid.
- VERIFY (llm_light, temp 0, JSON, `verify_batch_size` sentences per call): given sentence + cited source texts, verdict supported / partial / unsupported. Partial: rewrite once to match the source, no second verify loop. Unsupported: remove. Store counts.
- CONSENSUS: for up to `consensus_claims` central claims, classify each selected passage as supports / contradicts / unrelated. Uses the passages already selected, no extra retrieval.
- Timeouts: `quick_timeout_s` / `deep_timeout_s` for the whole run. On timeout, write from whatever evidence exists and mark the run `timed_out`.
- Every step writes a `run_steps` row and publishes to Redis channel `run:{id}`. Runs are cancellable via a Redis flag checked between steps. On worker restart, runs in `running` resume from the last completed step.
- Follow-ups: see 12b.

## 12b. Threads (chaining context)

- Every run belongs to a `thread`. A new question from the ask box starts a thread, a follow-up adds a run to it.
- Context passed to REWRITE and WRITE: the last `thread_full_turns` turns verbatim (question + final answer), plus the thread's running summary for older turns. After each run, if the thread has more than `thread_full_turns` turns, update the summary with llm_light (max ~300 words, must keep paper numbers and key conclusions).
- Paper carryover: papers cited earlier in the thread join FIND_PAPERS candidates with an extra `thread_carryover_boost` after reranking. They still compete, so a follow-up on a new subtopic can surface new papers.
- Evidence reuse: chunks cited earlier in the thread are added to PASSAGES candidates, so they can be cited again without refetching.
- Pinned papers: a thread can pin papers. Pinned papers are always in the selected set, and FIND_PAPERS fills the remaining slots. "Ask about this paper" creates a thread with that paper pinned and search restricted to it plus its in-corpus references.

## 12c. Citations in answers

- WRITE may only use source ids from the provided passages. Tags naming any other id are stripped, and the sentence is treated as untagged (removed).
- A sentence may carry several tags: `[[c:12]][[a:2305.14314]]`.
- Numbering is per paper and stable per thread: the first time a paper is cited in a thread it gets the next number, and it keeps that number in all later runs of the thread. Store the mapping in `thread_citations`.
- Rendered as chips `[n]`. Hover shows the exact passage text with section. Click opens `/papers/[id]?chunk={chunk_id}` scrolled to the passage, highlighted using `char_start`/`char_end`. Abstract citations open the paper page with the abstract highlighted.
- Under each answer: a source list with number, authors (first 3 + "et al."), year, title, sections used, badges, and "via references of [n]" for chained papers.
- Thread-level "Export BibTeX" for all cited papers (arXiv BibTeX with eprint, archivePrefix, primaryClass).

## 13. LLM client (backend/core/llm.py)

Single module used everywhere: async semaphore `max_concurrency`, retries with exponential backoff on 429/5xx, response cache in Redis keyed by sha256(model + prompt + params) with `cache_ttl_days`, logs every call to `llm_calls` with tokens, cost (per-model prices in config), latency, cache_hit. Structured calls validate against Pydantic models and retry once on invalid JSON.

## 14. Backend API

- `POST /threads` {question, mode, filters, pinned_papers?} → {thread_id, run_id}
- `POST /threads/{id}/runs` {question, mode, filters} → {run_id} (follow-up)
- `GET /threads/{id}` runs in order, citation numbering, pinned papers
- `POST /threads/{id}/pin` / `DELETE /threads/{id}/pin/{arxiv_id}`
- `GET /threads/{id}/bibtex`
- `GET /runs/{id}` full run: status, steps, answer sentences with sources, removed count, consensus, cost
- `GET /runs/{id}/events` SSE stream of steps and status
- `POST /runs/{id}/cancel`
- `GET /search?q=&filters` paper-level search results (no LLM)
- `GET /papers/{arxiv_id}` metadata, badges, score, full-text status
- `GET /papers/{arxiv_id}/content` parsed sections and paragraphs (from DB chunks / TEI), for the reader
- `GET /papers/{arxiv_id}/summary` generated with llm_light on first request, cached
- `GET /papers/{arxiv_id}/graph?hops=1` nodes and edges (references and cited-by, stubs included)
- `POST /papers/{arxiv_id}/fulltext` enqueue urgent fetch
- `GET /chunks/{id}` passage text with section and char offsets
- Collections CRUD, `GET /collections/{id}/bibtex`
- Feeds CRUD, `GET /feeds/{id}/digests`
- `GET /admin/stats` counts per state, fetch queue lengths, throughput, recent failures, disk usage of DATA_DIR subfolders

## 15. Frontend pages

- `/` Ask: question box, Quick / Deep toggle, filters (year range, categories), recent runs
- `/threads/[id]`: the conversation, one run after another, each rendered as below, with pinned papers shown at the top and a follow-up input at the bottom. Each run: left, live trace (SSE) with search queries, the ranked list of selected papers (title, year, badges, full-text or abstract-only), fetch progress, reading, verifying, cancel button. Right, answer with citation chips per sentence. Hover a chip: popover with the passage. Click: opens `/papers/[id]?chunk=...` scrolled to and highlighting that passage. Footer: "N claims removed", cost, time. Consensus bars under central claims. Tab with the citation graph of papers used (node size = fulltext_citations, color = year, click to expand, legend states that citations are counted within this corpus). 
- `/papers/[id]`: title, authors, badges, links to arXiv abs and PDF, structured summary, reader of parsed sections (not a PDF viewer, PDFs are not stored), references, cited-by, "Ask about this paper" (runs a quick run filtered to this paper), "Get full text" button if abstract-only.
- `/collections`, `/feeds`, `/admin`
- Clean, dense, dark and light mode, keyboard shortcut `/` to focus the ask box.

## 16. Scheduled jobs (arq cron)

- Nightly 02:00: OAI-PMH harvest for cs and math, new in-window papers → pipeline (metadata, metadata score, abstract embedding). Recompute citation scores, PageRank, and foundational papers from current edges.
- Weekly: feed digests (new papers matched by embedding similarity to feed query + keywords, summarized with llm_light).
- Janitor every 30 min.

## 17. Eval (eval/)

- `questions.yaml`: 40 questions, each with `expected_papers` (arxiv ids) and mode.
- `run_eval.py`: runs all questions, computes recall@10 for retrieval against expected papers, latency p50/p95, cost per run, removed-claims rate, and exports a sample of (sentence, cited passage) pairs to `eval/results/citation_sample.csv` for manual precision checking. Writes `eval/results/{timestamp}.json` and a markdown table.
- Ablation flags: `--no-keyword`, `--no-vector`, `--no-rerank`, `--embedding <model>`, `--chunk-size <n>` (chunk-size ablation re-chunks from `parsed/` into a separate table).

## 18. Makefile

`up`, `down`, `logs`, `migrate`, `ingest-metadata`, `landmarks`, `warm TOPIC="..."`, `score`, `embed`, `status`, `nightly`, `eval`, `backup` (pg_dump custom format to `DATA_DIR/backups/`), `restore FILE=...`, `test`.

## 19. .env.example

```
DATA_DIR=./data
GEMINI_API_KEY=
KAGGLE_API_TOKEN=
KAGGLE_USERNAME=
KAGGLE_KEY=
CONTACT_EMAIL=
POSTGRES_PASSWORD=
```

## 20. Quality bar

- Type hints everywhere, ruff + mypy clean, pytest for pipeline stages (with small fixture files: a sample Kaggle JSONL, a sample OAI-PMH response, a sample TEI), retrieval fusion, answer pipeline steps (LLM mocked), verifier logic, reference resolution.
- Structured JSON logging.
- No secrets in the repo. Add `.env` to `.gitignore` before anything else. Never read, print, log, or commit `.env` or any key value. Code reads secrets only from environment variables, and Compose passes them via `env_file: .env`. If a key is missing at runtime, fail with a message naming the variable.
- Postgres tuned for 16GB RAM: shared_buffers 2GB, effective_cache_size 6GB, maintenance_work_mem 1GB (for HNSW builds), work_mem 64MB.

## 21. Milestones (stop after each)

1. **Skeleton**: Compose (postgres, redis, grobid, api, worker, fetcher, frontend), config loading, migrations for all tables, LLM client with model check, health endpoints, Makefile.
2. **Metadata**: Kaggle stream + filter, landmark validation, tier 1 papers, metadata score, abstract embeddings + indexes, counts printed. `/admin` page.
3. **Full text on demand**: fetcher service with global limiter and two queues, GROBID parsing with bibliography, chunking, chunk embeddings, PDF lifecycle and janitor, landmark prefetch, `POST /papers/{id}/fulltext`, `make warm`, reference resolution into citation edges and stubs, citation scores and PageRank. Report the reference resolution rate.
4. **Search**: hybrid retrieval, rerank, `/search` endpoint, basic search UI returning papers and passages.
5. **Quick answers**: quick mode end to end with verification, threads with follow-ups and running summary, stable citation numbering, chips with hover and click-through to the reader, source list, BibTeX export.
6. **Deep mode**: FETCH step with on-demand full text, CHAIN step, pinned papers, live progress over SSE, timeouts, cancel, resume, follow-ups.
7. **Exploration**: paper page, summaries, citation graph, consensus view, collections with BibTeX.
8. **Feeds, nightly jobs, eval, README**: digests, OAI-PMH nightly, eval suite with ablations, README with architecture diagram, setup, and eval results table (leave a placeholder for the demo video).
