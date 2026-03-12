<div align="center">

# NeuralSeek Engine

### Distributed Research Paper Search Engine

*Hybrid BM25 + Semantic Vector Search over arXiv Computer Science Papers*

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Java](https://img.shields.io/badge/Java-17-ED8B00?style=for-the-badge&logo=openjdk&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![Spring Boot](https://img.shields.io/badge/Spring_Boot-3.2-6DB33F?style=for-the-badge&logo=springboot&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?style=for-the-badge&logo=redis&logoColor=white)
![gRPC](https://img.shields.io/badge/gRPC-Protobuf-244C5A?style=for-the-badge&logo=google&logoColor=white)
![Nginx](https://img.shields.io/badge/Nginx-009639?style=for-the-badge&logo=nginx&logoColor=white)

</div>

---

## Overview

NeuralSeek Engine is a production-grade, multi-service search engine for discovering computer science research papers from arXiv. It combines keyword-based retrieval (BM25) with dense vector search (FAISS) and neural reranking (cross-encoder) to deliver highly relevant results across a large-scale academic corpus.

The system is built as a distributed microservice architecture: a **React frontend** served by **Nginx**, a **Java Spring Boot API gateway** with **Redis caching**, and a **Python ML search engine** communicating over **gRPC**. All services are containerised and deployable to any Linux cloud VM with a single `docker compose up -d --build`.

---

## Key Highlights

- 🔍 **Hybrid retrieval** — BM25 keyword search fused with FAISS HNSW dense vector search
- 🤖 **Cross-encoder reranking** — BERT-based `ms-marco-MiniLM-L-6-v2` reranks top-100 candidates for high-precision results
- ⚡ **Redis caching** — repeat query latency drops from ~600ms → ~1ms with `@Cacheable` at the API layer
- 🔗 **Cross-language gRPC** — Python ML engine ↔ Java API gateway communicate via typed Protocol Buffer messages
- 🧠 **Transformer embeddings** — `all-MiniLM-L6-v2` (384-dim) encodes queries for semantic similarity search
- 📖 **arXiv CS corpus** — extracted from the full arXiv snapshot, filtered to CS papers, LaTeX-cleaned
- 🐳 **Fully containerised** — one `docker compose up --build` starts all 4 services
- 🔤 **Spell correction + autocomplete** — SymSpell O(1) correction + prefix trie typeahead
- 🌐 **Cloud-ready** — Nginx production frontend, configurable API URLs via build args, auto-restart policies
- 📄 **Similar paper discovery** — FAISS vector reconstruction for finding semantically related papers
- 📑 **Paginated results** — 10 results per page, up to 10 pages, clamped at both API and engine layers

---

## Architecture

```
┌────────────────────────────────────────────────┐
│     React 19 + TypeScript (Vite Build)         │
│     Served by Nginx — Port 3000                │
└──────────────────────┬─────────────────────────┘
                       │  HTTP REST / JSON (axios)
                       ▼
┌────────────────────────────────────────────────┐
│       Java Spring Boot 3.2 — API Gateway       │
│                   Port 8080                    │
│                                                │
│  ┌─────────────────┐    ┌─────────────────┐    │
│  │ SearchController│────│  SearchService  │    │
│  └─────────────────┘    └────────┬────────┘    │
│                                  │             │
│                                  ▼             │
│                    ┌─────────────────────────┐ │
│                    │     Redis 7 Cache       │ │
│                    │   @Cacheable by         │ │
│                    │   (query, page)         │ │
│                    └─────────────────────────┘ │
└──────────────────────┬─────────────────────────┘
                       │  gRPC / Protocol Buffers v3
                       ▼  Port 50051
┌────────────────────────────────────────────────┐
│        Python 3.11 — ML Search Engine          │
│                                                │
│  BM25 Title + Abstract Indexes  (rank-bm25)    │
│  FAISS HNSW Vector Index        (faiss-cpu)    │
│  SentenceTransformer Embeddings (MiniLM-L6-v2) │
│  Cross-Encoder Reranker  (ms-marco-MiniLM-L-6) │
│  Spell Correction               (SymSpell)     │
│  Prefix Trie                    (Autocomplete) │
└────────────────────────────────────────────────┘
```

**Protocols:**
- `Frontend → Java` — HTTP REST, JSON responses, CORS enabled (`*`)
- `Java → Python` — gRPC with binary Protobuf encoding (schema-governed, typed)
- `Java → Redis` — Spring Data Redis with JSON serialisation (1-hour TTL)

**Startup order:** Redis → Python gRPC Engine → Java Backend → Frontend (Nginx)

---

## System Design Rationale

| Decision | Rationale |
|---|---|
| **Python for ML engine** | Best-in-class ML ecosystem. sentence-transformers, FAISS, rank-bm25, and SymSpell all have native Python support. No viable Java equivalent. |
| **Java Spring Boot as API gateway** | Production-grade HTTP server with Spring's `@Cacheable` abstraction, type-safe gRPC client injection, and mature REST tooling. Separates ML concerns from API concerns. |
| **gRPC instead of REST between services** | Binary Protobuf encoding is ~5–10× more efficient than JSON. Schema-governed — breaking changes caught at compile time, not runtime. Auto-generates stubs for both Python and Java from a single `.proto` file. |
| **Redis at the Java layer** | Caches the final serialised result set. Cache hits never touch the Python engine at all — eliminating BM25 + FAISS + cross-encoder inference cost entirely. |
| **Nginx for frontend** | Multi-stage Docker build: Vite compiles the React app → Nginx serves static assets with gzip compression. Production-grade, zero-config SPA hosting. |
| **Microservice split** | Python ML worker and Java API are independently deployable and scalable. ML model upgrades (swap embeddings model) require no Java changes. API changes require no Python changes. Only `search.proto` is the coupling point. |

---

## Search Pipeline

```
User Query: "attention mechanisms transformers"
                    │
                    ▼
     ┌────────────────────────────┐
     │   1. Spell Correction      │
     │   SymSpell (O(1) lookup)   │
     │   Custom dict from titles  │
     └──────────────┬─────────────┘
                    │
                    ▼
     ┌──────────────────────────────┐
     │   2. Tokenisation            │
     │   regex r"\b\w+\b"           │
     │   ["attention", "mechanisms" │
     │    "transformers"]           │
     └──────┬───────────────┬───────┘
            │               │
            ▼               ▼
  ┌────────────────┐  ┌────────────────────────┐
  │ 3. BM25 Scoring│  │ 4. Semantic Embedding  │
  │                │  │                        │
  │ title_score ×  │  │ MiniLM.encode(query)   │
  │   3.0          │  │ → 384-dim float32 vec  │
  │ abstract_score │  │                        │
  │   × 1.5        │  │ FAISS HNSW.search(vec, │
  │ normalise [0,1]│  │   k=200) → top-200     │
  │ → top-200      │  │                        │
  └─────────┬──────┘  └─────┬──────────────────┘
            │               │
            ▼               ▼
     ┌──────────────────────────────┐
     │   5. Candidate Fusion        │
     │   union(BM25-200, FAISS-200) │
     │   score = bm25 +             │
     │     0.75 × (1/1+L2_dist)     │
     │   sort ↓ → top-100           │   
     └──────────────┬───────────────┘
                    │
                    ▼
     ┌─────────────────────────────┐
     │   6. Cross-Encoder Rerank   │
     │   pairs = [(query, title +  │
     │     abstract)] × 100        │
     │   BERT cross-attention      │
     │   scores → re-sort ↓        │
     └──────────────┬──────────────┘
                    │
                    ▼
     ┌──────────────────────────────┐
     │   7. Pagination Slice        │
     │   start = (page-1) × 10      │
     │   return reranked[start:end] │
     │   10 results per page        │
     │   max 10 pages (100 total)   │
     └──────────────────────────────┘
```

### Why Hybrid Retrieval Works

| Method | Strength | Weakness |
|---|---|---|
| **BM25** | Exact terminology, acronyms ("BERT", "CNN"), author names | Vocabulary mismatch — misses synonyms and paraphrases |
| **Semantic (FAISS)** | Concept-level similarity — "seq2seq" matches "encoder-decoder" | Imprecise for specific technical terms |
| **Hybrid fusion** | Maximises both precision AND recall | Requires two infrastructure components |
| **Cross-encoder rerank** | Full bidirectional attention over (query, document) — highest quality relevance | Computationally expensive; only feasible on a short candidate list |

The two-stage design (cheap retrieval → expensive reranking) is the same architecture used by production search teams at major tech companies and described in academic systems like DPR (Facebook Research) and ColBERT.

---

## Dataset & Indexing Pipeline

```
arxiv-metadata-oai-snapshot.json
          (~3M papers, all fields, JSONL)
                       │
                       ▼
         ┌───────────────────────────┐
         │   extract_papers.py       │
         │   Filter: "cs." in        │
         │   categories              │
         │   Flatten author names    │
         │   Construct pdf_url       │
         └─────────────┬─────────────┘
                       │
              papers_cs.jsonl
                       │
                       ▼
         ┌───────────────────────────┐
         │   clean_dataset.py        │
         │   LaTeX token replacement │
         │   (\alpha → "alpha")      │
         │   Accent stripping        │
         │   Unicode NFKD → ASCII    │
         │   Whitespace collapse     │
         └─────────────┬─────────────┘
                       │
          papers_cs_clean.jsonl
                       │
             ┌─────────┴────────────┐
             │                      │
             ▼                      ▼
  ┌──────────────────┐   ┌──────────────────────────┐
  │ Full deployment  │   │ filter_dataset_for_aws.py │
  │ All CS papers    │   │ Per-category hard caps    │
  │ (high RAM)       │   │ cs.AI: 600, cs.LG: 600,  │
  └──────────┬───────┘   │ cs.IR: 500, cs.DS: 600   │
             │           │ Total: ~7K papers         │
             │           └───────────┬──────────────┘
             │                       │
             └──────────┬────────────┘
                        │  indexer.py (offline build)
                        ▼
           ┌────────────────────────────────────┐
           │  bm25_title.pkl   (BM25 titles)    │
           │  bm25_abstract.pkl (BM25 abstracts)│
           │  faiss.index      (HNSW, 384-dim)  │
           │  docs.pkl         (metadata)       │
           └────────────────────────────────────┘
```

### FAISS Index Configuration

The indexer builds a FAISS `IndexHNSWFlat` with the following parameters:

| Parameter | Value | Purpose |
|---|---|---|
| Dimension | 384 | `all-MiniLM-L6-v2` embedding size |
| HNSW M | 32 | Max connections per layer |
| efConstruction | 200 | Build-time search depth (higher = more accurate index) |
| efSearch | 64 | Query-time search depth |

Vectors are L2-normalised before insertion. HNSW provides sub-linear approximate nearest neighbour search, making it suitable for scaling to millions of documents.

### CS Categories Indexed

`cs.AI` · `cs.LG` · `cs.CL` · `cs.CV` · `cs.NE` · `cs.DB` · `cs.IR` · `cs.DC` · `cs.DS` · `cs.GT` · `cs.CG` · `cs.CC` · `cs.FL` · `cs.LO` · `cs.DM` · `cs.SE` · `cs.CR` · `cs.NA`

---

## Tech Stack

### Machine Learning / Search

| Library | Purpose |
|---|---|
| `sentence-transformers` | `all-MiniLM-L6-v2` query/document encoding → 384-dim vectors |
| `faiss-cpu` | FAISS `IndexHNSWFlat` — approximate nearest neighbour search with sub-linear complexity |
| `rank-bm25` | `BM25Okapi` — dual field indexes (title × 3.0, abstract × 1.5) |
| `CrossEncoder` | `ms-marco-MiniLM-L-6-v2` — BERT reranker on (query, document) pairs |
| `symspellpy` | O(1) spell correction; domain dictionary built from paper titles |
| `numpy` | Vectorised BM25 score fusion and normalisation |
| `ujson` | C-extension JSON parser for fast JSONL streaming during indexing |

### Java Backend

| Library | Purpose |
|---|---|
| Spring Boot 3.2.3 | REST API server, DI, application lifecycle |
| Spring Data Redis | `@Cacheable` annotation-driven result caching (JSON serialisation, 1h TTL) |
| `grpc-client-spring-boot-starter` 3.0 | Injects gRPC blocking stubs as Spring beans via `@GrpcClient` |
| `protobuf-maven-plugin` | Compiles `search.proto` → Java classes during `mvn package` |
| Protocol Buffers 3.25.1 + gRPC 1.62.2 | Binary wire format between services |
| Lombok | `@Builder`, `@Data` on `PaperDto` |

### Frontend

| Library | Purpose |
|---|---|
| React 19 + TypeScript 5.9 | Component UI with type-safe API contracts |
| Vite 7 | ES module bundler — builds production assets |
| Nginx | Production static file server with gzip and SPA fallback |
| axios | HTTP client for REST calls |
| lucide-react | Lightweight SVG icon set |

### Infrastructure

| Tool | Purpose |
|---|---|
| Docker Compose | Multi-service orchestration with dependency ordering |
| Nginx | Frontend production server with gzip compression and SPA routing |
| Redis 7 (Alpine) | Cache layer with healthcheck |
| gRPC + Protobuf | Cross-language inter-service communication |

---

## Core Features

### 🔍 Hybrid BM25 + Semantic Search
Fuses keyword and vector scores into a single ranking signal:

```
final_score = normalised_bm25_score + 0.75 × semantic_score
```

Two separate BM25 indexes (title, abstract) with field-level weights — equivalent to Elasticsearch `multi_match` boosting. FAISS HNSW returns 200 nearest neighbours; their L2 distances are inverted (`1 / 1 + distance`) to a similarity score. The union of both top-200 sets is fused, sorted, and pruned to top-100 before the expensive reranking step.

### 🤖 Cross-Encoder Reranking
After fusion, `ms-marco-MiniLM-L-6-v2` receives each `(query, title + abstract)` pair as a single sequence. Full BERT cross-attention produces a scalar relevance score that is substantially more accurate than bi-encoder cosine similarity. These scores replace the fusion scores as the final ranking signal.

### 🔤 Spell Correction
SymSpell runs on every query before retrieval. A word frequency dictionary is built at startup from all paper titles. `lookup_compound` is used for multi-word correction with edit distance ≤ 2. Runs in O(1) — constant time regardless of dictionary size.

### ⌨️ Autocomplete / Typeahead
A prefix trie is built in-memory at startup from all paper titles, with a maximum of 5 suggestions per node (memory-bounded). Autocomplete also applies SymSpell single-word correction on the typed prefix. React debounces requests by 300ms. Lookup is O(prefix_length).

### 📄 Similar Papers
"Find Similar" reconstructs the paper's FAISS vector via `index.reconstruct()` (no re-encoding needed), then returns 10 nearest neighbours by L2 distance. Falls back to encoding the paper's title + abstract if reconstruction fails.

### ⚡ Redis Caching
`@Cacheable` applied to all three service methods (search, similar, autocomplete). Search keyed on `"<query>_<page>"`, similar keyed on paper ID, autocomplete keyed on prefix. Cache entries serialised as JSON with 1-hour TTL. Cold: ~200–600ms → Cached: ~1ms.

### 📑 Pagination
10 results per page, 10 pages max (100 results per query). 1-based, clamped at both the Java controller and Python engine layers. "Next" auto-disables when fewer than 10 results are returned.

---

## Performance

| Metric | Value |
|---|---|
| Cold query latency | ~200–600ms |
| Cached query latency | ~1ms |
| Cache TTL | 3600s (1 hour) |
| Results per page | 10 |
| Max results per query | 100 (10 pages) |
| FAISS index type | HNSW (M=32, efSearch=64) |

**Two-stage retrieval tradeoff:**  
Cross-encoder inference is expensive — O(n × seq_len²) BERT computation. By limiting it to the top-100 fused candidates (not all N), the system achieves high reranking quality at a fixed, bounded cost. BM25 + FAISS operate in milliseconds over the full corpus; cross-encoder only runs on the shortlist.

---

## Project Structure

```
Research_paper_search_engine/
│
├── proto/
│   └── search.proto                 # Shared gRPC schema (3 RPCs, 6 messages)
│
├── search-engine-python/
│   ├── search_engine.py             # HybridSearchEngine — BM25, FAISS, CrossEncoder,
│   │                                # SymSpell, Trie, search/similar/autocomplete
│   ├── grpc_server.py               # gRPC servicer — routes requests to search_engine
│   ├── indexer.py                   # Offline index builder — HNSW + BM25 + metadata
│   └── requirements.txt             # ML dependencies
│
├── backend-java/
│   ├── pom.xml                      # Spring Boot 3.2, gRPC, protobuf-maven-plugin
│   └── src/main/java/com/searchengine/backend/
│       ├── BackendApplication.java          # @SpringBootApplication + @EnableCaching
│       ├── config/RedisConfig.java          # RedisTemplate + CacheManager (JSON, 1h TTL)
│       ├── controller/SearchController.java # /search, /similar/{id}, /autocomplete
│       ├── service/SearchService.java       # @Cacheable + gRPC stub calls
│       └── dto/PaperDto.java                # Lombok response DTO
│
├── frontend/
│   ├── package.json                 # React 19, Vite 7, TypeScript, axios, lucide-react
│   └── src/
│       ├── App.tsx                  # Search, results, pagination, autocomplete, similar
│       └── index.css                # Dark glassmorphism UI — Outfit font, animations
│
├── dataset/
│   ├── extract_papers.py            # Step 1 — Full arXiv → CS-only papers (JSONL)
│   ├── clean_dataset.py             # Step 2 — LaTeX sanitisation + unicode normalisation
│   └── filter_dataset_for_aws.py    # Step 3 — Category-capped subset for small VMs
│
├── docker/
│   ├── Dockerfile.python            # python:3.11-slim; auto-compiles proto at build
│   ├── Dockerfile.java              # Multi-stage: Maven build → eclipse-temurin:17-jre
│   ├── Dockerfile.frontend          # Multi-stage: Vite build → Nginx Alpine
│   └── nginx.conf                   # SPA routing, gzip compression, port 3000
│
├── docker-compose.yml               # Orchestrates all 4 services with restart policies
└── .dockerignore                    # Excludes datasets, keeps index files for builds
```

---

## Quick Start

### Prerequisites

Build the index files offline (once) and place them in `search-engine-python/`:

```
bm25_title.pkl
bm25_abstract.pkl
faiss.index
docs.pkl
```

> These files are excluded from git (`.gitignore`) due to their size. Run `indexer.py` against your prepared dataset to generate them.

### Run with Docker (Local)

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Java REST API | http://localhost:8080 |
| Python gRPC Engine | `localhost:50051` (internal) |
| Redis | `localhost:6379` (internal) |

### Run Without Docker

**Python search engine:**
```bash
cd search-engine-python
pip install -r requirements.txt
python -m grpc_tools.protoc -I../proto --python_out=. --grpc_python_out=. ../proto/search.proto
python grpc_server.py
```

**Java backend:**
```bash
cd backend-java
mvn clean package -DskipTests
java -jar target/backend-java-0.0.1-SNAPSHOT.jar
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

---

## Deployment

The system is fully containerized using Docker Compose and runs locally with the full arXiv Computer Science dataset (~1.1M papers).

The architecture includes multiple services:

* Python search engine (FAISS + BM25 hybrid retrieval)
* Java Spring Boot backend
* Redis cache
* React frontend
* Docker Compose orchestration

Cloud deployment was attempted on Oracle Cloud Free Tier, but the available 1 GB RAM instance was insufficient to run all services (FAISS + BM25 + Redis + Java backend) simultaneously.

Due to memory constraints of the free-tier environment, the full system is demonstrated locally with the complete dataset, while the containerized architecture is designed to scale on higher-memory cloud instances.

The project is fully deployable on any cloud VM with sufficient RAM (recommended ≥ 4–8 GB).

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/search?q={query}&page={n}` | Paginated hybrid search — pages 1–10, 10 results each |
| `GET` | `/similar/{paperId}` | 10 papers semantically similar to the given arXiv ID |
| `GET` | `/autocomplete?q={prefix}` | Up to 5 paper title suggestions with spell correction |

---

## gRPC Contract

```protobuf
service SearchService {
  rpc Search       (SearchRequest)       returns (SearchResponse);
  rpc Similar      (SimilarRequest)      returns (SearchResponse);
  rpc Autocomplete (AutocompleteRequest) returns (AutocompleteResponse);
}

message SearchRequest {
  string query = 1;
  int32  top_k = 2;
  int32  page  = 3;
  int32  size  = 4;
}

message SimilarRequest {
  string paper_id = 1;
  int32  top_k    = 2;
}

message AutocompleteRequest {
  string prefix = 1;
}

message Paper {
  string          id       = 1;
  string          title    = 2;
  string          abstract = 3;
  repeated string authors  = 4;
  string          pdf_url  = 5;
}
```

The `.proto` schema is compiled **automatically inside Docker containers** at build time — no manual `protoc` invocation needed.

---

## Future Improvements

- **GPU acceleration** — FAISS on GPU (CUDA) for faster batch embedding and search at scale
- **LLM query expansion** — Use an LLM to expand short queries before retrieval, improving recall on ambiguous queries
- **Learning-to-rank** — Replace the cross-encoder with a supervised LTR model trained on user click data
- **Citation graph search** — Add a graph index (Neo4j / NetworkX) to surface papers by citation proximity, not just text similarity
- **Streaming results** — Replace polling with server-sent events or WebSocket for progressive result loading
- **HTTPS / Reverse proxy** — Add TLS termination via Caddy or Nginx reverse proxy for production HTTPS
- **Kubernetes / AWS ECS** — Deploy with Kubernetes HPA or ECS Fargate for auto-scaled, cloud-native hosting
- **Query analytics dashboard** — Track popular queries, cache hit rates, and latency percentiles
- **User authentication** — Support saved searches, bookmarks, and personalised recommendations

---

## Engineering Highlights

| Aspect | Detail |
|---|---|
| **Retrieve-then-rerank** | Same two-stage architecture as DPR (Facebook Research), ColBERT, and Elasticsearch ONNX rerankers |
| **Cross-language gRPC** | Shared `.proto` schema auto-generates Python server stubs and Java client stubs — compile-time contract enforcement |
| **Dual-field BM25** | Separate BM25 indexes per field with tunable weights avoids BM25 length-normalisation contamination across fields |
| **HNSW index** | `IndexHNSWFlat` with M=32, efConstruction=200 — production-grade approximate search with sub-linear query time |
| **Bounded trie autocomplete** | 5-suggestion per-node cap keeps memory usage bounded over large title sets |
| **SymSpell over NLTK** | O(1) spell correction vs O(n) for edit-distance algorithms — critical for low-latency query preprocessing |
| **Multi-stage Docker builds** | Maven → JRE (Java), Vite → Nginx (Frontend) — final images contain no SDKs or build tools |
| **Automatic proto compilation** | `grpc_tools.protoc` runs inside the Python container; `protobuf-maven-plugin` runs during `mvn package` — zero manual setup |
| **Cloud-ready configuration** | `VITE_API_URL` as build arg, Nginx production serving, restart policies, Redis healthchecks |
