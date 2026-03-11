# NeuralSeek Engine — Distributed Research Paper Search Engine

> A production-grade, distributed search engine for **1.1 million+ arXiv computer science papers**, powered by hybrid BM25 + semantic vector retrieval with cross-encoder reranking.

---

## What It Does

NeuralSeek Engine lets you search across the full arXiv computer science corpus using natural language queries. It combines the precision of classic keyword search with the semantic understanding of transformer embeddings — the same hybrid retrieval approach used by state-of-the-art academic and commercial search systems.

**Try searching:**
- `"attention mechanisms in NLP"` — finds semantically related papers even without exact keyword matches
- `"graph neural networks fraud detection"` — cross-domain semantic retrieval
- `"BERT pretraining"` — precise keyword matching on terminology

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│              React 19 + TypeScript (Vite)            │
│                      Port 3000                       │
└─────────────────────────┬────────────────────────────┘
                          │  HTTP REST (axios)
                          ▼
┌──────────────────────────────────────────────────────┐
│           Java Spring Boot 3.2 — API Gateway         │
│                      Port 8080                       │
│        Redis @Cacheable · gRPC Client Stub           │
└─────────────────────────┬────────────────────────────┘
                          │  gRPC / Protocol Buffers v3
                          ▼  Port 50051
┌──────────────────────────────────────────────────────┐
│          Python 3.11 — ML Search Engine              │
│                                                      │
│   BM25 Title + Abstract Indexes  (rank-bm25)         │
│   FAISS Dense Vector Index        (faiss-cpu)        │
│   Sentence Embeddings             (all-MiniLM-L6-v2) │
│   Cross-Encoder Reranker  (ms-marco-MiniLM-L-6-v2)   │
│   Spell Correction                (SymSpell)         │
│   Prefix Trie                     (Autocomplete)     │
└──────────────────────────────────────────────────────┘
                           │
                  ┌────────┴────────┐
                  │   Redis 7       │
                  │  Result Cache   │
                  └─────────────────┘
```

**Communication:**
- **Frontend → Java:** HTTP REST (JSON)  
- **Java → Python:** gRPC with binary Protocol Buffer encoding (typed, schema-governed)  
- **Java → Redis:** Spring `@Cacheable` abstraction

**Service startup order:** Redis → Python gRPC engine → Java backend → React frontend

---

## Tech Stack

### Machine Learning / Search

| Technology | Role |
|---|---|
| `sentence-transformers` | Encodes queries into 384-dim vectors using `all-MiniLM-L6-v2` |
| `faiss-cpu` | Facebook AI Similarity Search — fast ANN retrieval over dense embeddings |
| `rank-bm25` | BM25Okapi keyword ranking — separate indexes for title and abstract fields |
| `CrossEncoder` (sentence-transformers) | BERT-based reranker (`ms-marco-MiniLM-L-6-v2`) — full attention over (query, document) |
| `symspellpy` | O(1) spell correction via SymSpell algorithm; custom dictionary from paper titles |
| `numpy` | Vectorised BM25 score fusion and normalisation |
| `ujson` | High-speed C-extension JSON parser for large JSONL dataset processing |

### Backend (Java)

| Technology | Role |
|---|---|
| Spring Boot 3.2 | REST API server, DI, lifecycle management |
| Spring Data Redis | `@Cacheable` annotation-driven result caching |
| `grpc-client-spring-boot-starter` | Injects gRPC blocking stubs as Spring beans |
| `protobuf-maven-plugin` | Compiles `search.proto` → Java classes during `mvn package` |
| Protocol Buffers 3.25.1 + gRPC 1.62.2 | Binary wire format — ~5–10× more efficient than REST/JSON for service-to-service |
| Lombok | `@Builder`, `@Data` on DTOs |

### Frontend

| Technology | Role |
|---|---|
| React 19 + TypeScript 5.9 | Component UI with type-safe API contracts |
| Vite 7 | Sub-second HMR, native ES module bundling |
| axios | HTTP client with clean error handling |
| lucide-react | Lightweight SVG icon set |

### Infrastructure

| Technology | Role |
|---|---|
| Docker + Docker Compose 3.8 | All 4 services containerised; one-command startup |
| Redis 7 Alpine | Minimal-footprint in-memory result cache |

---

## Core Features

### Hybrid BM25 + Semantic Search
Two retrieval signals are fused into a single score:

```
final_score = normalised_bm25_score + 0.75 × semantic_score
```

**BM25 (keyword):** Two separate indexes — title (weight `3.0×`) and abstract (weight `1.5×`). Equivalent to Elasticsearch's `multi_match` with per-field boosting.  
**Semantic:** Query encoded to 384-dim vector → FAISS ANN search returns 200 nearest neighbours by L2 distance, converted to `1 / (1 + distance)`.  
**Fusion:** Union of both top-200 sets, scored and pruned to top-100 before reranking.

### Cross-Encoder Reranking
Top-100 fused candidates are passed to `ms-marco-MiniLM-L-6-v2`. Unlike a bi-encoder, the cross-encoder sees the entire `(query, title + abstract)` as a single sequence, enabling full cross-attention. This produces substantially more accurate relevance judgements. Cross-encoder scores become the final ranking signal.

### Spell Correction
SymSpell runs on every query before any processing. A frequency dictionary is built at startup from all paper titles. Edit distance ≤ 2. Runs in O(1) — constant time regardless of dictionary size.

### Autocomplete / Typeahead
A prefix trie is built in-memory at startup from all paper titles. Each trie node stores up to 5 suggestions (memory-capped). React debounces calls by 300ms and fetches for queries ≥ 2 characters. Lookup is O(prefix_length).

### Similar Papers
Clicking "Find Similar" on any result reconstructs that paper's FAISS vector via `index.reconstruct()` (no recomputation), then returns its 10 nearest neighbours by embedding distance.

### Redis Caching
Spring `@Cacheable` applied to all three service methods:
- Search: keyed on `"<query>_<page>"`
- Autocomplete: keyed on `"<prefix>"`
- Similar: keyed on `"<paperId>"`

Cache hits bypass Python gRPC entirely. Latency: ~200–600ms cold → ~1ms cached.

### Pagination
10 results per page, maximum 10 pages (100 papers per query). 1-based, clamped at both Java controller and Python engine. "Next" auto-disables when fewer than 10 results are returned.

---

## Search Pipeline

```
Query: "bert pretraining language models"
    │
    ├─ [1] Spell Correction (SymSpell, max edit distance 2)
    │
    ├─ [2] Tokenisation  →  ["bert", "pretraining", "language", "models"]
    │
    ├─ [3] BM25 Scoring (NumPy vectorised, all N documents)
    │       bm25 = 3.0 × title_scores + 1.5 × abstract_scores → normalise → top-200
    │
    ├─ [4] Semantic Embedding  →  MiniLM.encode(query) → 384-dim float32 vector
    │       FAISS.search(vector, k=200) → top-200 nearest neighbours + L2 distances
    │
    ├─ [5] Candidate Fusion
    │       candidates = union(BM25-top-200, FAISS-top-200)
    │       score = bm25_score + 0.75 × (1 / 1 + L2_distance)
    │       sort ↓ → take top-100
    │
    ├─ [6] Cross-Encoder Reranking
    │       pairs = [(query, title + abstract)] × 100
    │       rerank_scores = CrossEncoder.predict(pairs)
    │       re-sort ↓ by cross-encoder score
    │
    └─ [7] Pagination slice  →  reranked[(page-1)×10 : page×10]
```

| Stage | Algorithm | Time Complexity |
|---|---|---|
| BM25 | BM25Okapi (k1=1.5, b=0.75) | O(query_terms × N), NumPy vectorised |
| FAISS | IndexFlatL2 (exact L2) | O(N × 384), BLAS-accelerated |
| Fusion | Union + weighted linear combo | O(candidates) |
| Cross-encoder | BERT bidirectional attention | O(100 × seq_len²) |

---

## Dataset & Indexing Pipeline

### Dataset
| Property | Value |
|---|---|
| Source | [arXiv Metadata Snapshot](https://www.kaggle.com/datasets/Cornell-University/arxiv) (OAI-PMH bulk export) |
| Raw size | ~3 million papers, all academic fields, JSONL format |
| CS filter | Papers with any `cs.*` category tag |
| CS subset | ~1.1 million papers |
| Cloud subset | ~74,000 papers (per-category capped for RAM-limited VMs) |
| Fields stored | `id`, `title`, `abstract`, `authors[]`, `pdf_url` |

### Processing Pipeline

```
arxiv-metadata-oai-snapshot.json  (~3M papers)
               │
               ▼  dataset/extract_papers.py
        Filter: categories contains "cs.*"
        Extract & flatten: id, title, abstract, authors, pdf_url
               │
               ▼  papers_cs.jsonl  (~1.1M papers)
               │
               ▼  dataset/clean_dataset.py
        LaTeX token replacement (\alpha → "alpha", \$...\$ removed)
        LaTeX accent stripping  (\'e → "e")
        Unicode NFKD normalisation → ASCII
        Whitespace collapse
               │
               ▼  papers_cs_clean.jsonl
               │
        ┌──────┴──────────────────────────────────┐
        ▼  (full server)                          ▼  dataset/filter_dataset_for_aws.py
   Build full ~1.1M index             Per-category caps (cs.AI: 6K, cs.LG: 6K,
                                       cs.IR: 5K, cs.DB: 5K, ...) → ~74K papers
                                       Build cloud-fit index
               │
               ▼  Offline index build (run once)
        bm25_title.pkl      — BM25Okapi over titles
        bm25_abstract.pkl   — BM25Okapi over abstracts
        faiss.index         — FAISS IndexFlatL2, 384-dim embeddings
        docs.pkl            — Serialised list of paper metadata dicts
```

**CS categories in cloud deployment:** cs.AI, cs.LG, cs.CL, cs.CV, cs.NE, cs.DB, cs.IR, cs.DC, cs.DS, cs.GT, cs.CG, cs.CC, cs.FL, cs.LO, cs.DM, cs.SE, cs.CR, cs.NA

---

## Performance

| Metric | Value |
|---|---|
| Cold query latency | ~200–600ms (BM25 + FAISS + cross-encoder) |
| Cached query latency | ~1ms (Redis hit) |
| FAISS index RAM (74K papers) | ~110 MB |
| FAISS index RAM (1.1M papers) | ~1.6 GB |
| Max results per query | 100 (10 pages × 10) |

**Key optimisations:**
- Two-stage retrieval: cheap BM25+FAISS first, expensive cross-encoder only on top-100
- Dual-field BM25 with per-field weights (avoids BM25 length normalisation contamination)
- SymSpell O(1) spell correction (faster than any edit-distance based alternative)
- Redis caching eliminates ML inference cost on repeat queries
- `ujson` C-extension for fast JSONL parsing during dataset processing

---

## Scalability

The architecture is designed to scale horizontally:

| Scale | FAISS Strategy | Notes |
|---|---|---|
| ~74K papers | `IndexFlatL2` (exact) | Current cloud deployment |
| ~1.1M CS papers | `IndexFlatL2` on 32GB RAM | Full CS corpus |
| ~3M+ papers | `IndexIVFPQ` or `IndexHNSWFlat` | ANN with ~95–99% recall, sub-linear search |

- **Python ML service:** Stateless after index load — horizontally scalable behind gRPC load balancer
- **Java backend:** Fully stateless REST — scale behind Nginx / AWS ALB
- **Redis:** Drop-in replacement with Redis Cluster or AWS ElastiCache
- **gRPC contract:** Services evolve independently — only `search.proto` is the coupling point

---

## Project Structure

```
Research_paper_search_engine/
│
├── proto/
│   └── search.proto                # Shared gRPC schema (SearchRequest, SearchResponse, Paper, ...)
│
├── search-engine-python/
│   ├── search_engine.py            # HybridSearchEngine: BM25, FAISS, CrossEncoder, SymSpell, Trie
│   ├── grpc_server.py              # gRPC server — routes requests to search_engine
│   └── requirements.txt            # ML dependencies
│
├── backend-java/
│   ├── pom.xml                     # Spring Boot 3.2, gRPC, protobuf-maven-plugin
│   └── src/main/java/com/searchengine/backend/
│       ├── controller/SearchController.java   # REST endpoints
│       ├── service/SearchService.java         # Cache + gRPC stub calls
│       └── dto/PaperDto.java                  # Lombok response DTO
│
├── frontend/
│   ├── package.json                # React 19, Vite 7, TypeScript, axios, lucide-react
│   └── src/
│       ├── App.tsx                 # Search, results, pagination, autocomplete, similar papers
│       └── index.css               # Dark glassmorphism design system + animations
│
├── dataset/
│   ├── extract_papers.py           # Step 1 — 3M raw → 1.1M CS papers
│   ├── clean_dataset.py            # Step 2 — LaTeX + unicode cleaning
│   └── filter_dataset_for_aws.py  # Step 3 — category-capped cloud subset (~74K)
│
├── docker/
│   ├── Dockerfile.python           # python:3.11-slim, auto-compiles proto at build time
│   ├── Dockerfile.java             # Multi-stage Maven build → eclipse-temurin:17-jre
│   └── Dockerfile.frontend         # Vite dev server
│
└── docker-compose.yml              # Orchestrates redis, python-search-engine, java-backend, frontend
```

---

## Quick Start

### Prerequisites
Build the index files offline (run once after preparing your dataset):
```
bm25_title.pkl
bm25_abstract.pkl
faiss.index
docs.pkl
```
Place them in `search-engine-python/`. These are excluded from git due to size.

### Run Everything

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| Java REST API | http://localhost:8080 |
| Python gRPC Engine | localhost:50051 (internal) |
| Redis | localhost:6379 (internal) |

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
mvn clean package
java -jar target/backend-java-0.0.1-SNAPSHOT.jar
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/search?q={query}&page={n}` | Search papers — page 1–10, 10 results per page |
| `GET` | `/similar/{paperId}` | Find 10 semantically similar papers |
| `GET` | `/autocomplete?q={prefix}` | Get up to 5 title suggestions |

---

## gRPC Contract (`proto/search.proto`)

```protobuf
service SearchService {
  rpc Search      (SearchRequest)     returns (SearchResponse);
  rpc Similar     (SimilarRequest)    returns (SearchResponse);
  rpc Autocomplete(AutocompleteRequest) returns (AutocompleteResponse);
}

message SearchRequest {
  string query = 1;
  int32  page  = 3;
  int32  size  = 4;
}

message Paper {
  string id       = 1;
  string title    = 2;
  string abstract = 3;
  repeated string authors = 4;
  string pdf_url  = 5;
}
```

The `.proto` schema is compiled automatically inside Docker containers — no manual `protoc` invocation needed.

---

## Implementation Highlights

- **Zero-copy gRPC transport** between Java and Python — binary Protobuf encoding, ~5–10× faster than REST/JSON
- **Retrieve-then-rerank pipeline** mirrors production search systems (DPR, ColBERT, Elasticsearch ONNX rerankers)
- **Dual-field BM25** with separate title/abstract indexes and learned field weights
- **SymSpell O(1) spell correction** using a domain-specific dictionary built from paper titles
- **In-memory trie** with per-node suggestion cap for memory-bounded autocomplete at 1.1M+ title scale
- **Multi-stage Dockerfile** for Java (Maven build image → lean JRE runtime image)
- **Automatic proto compilation** in both Python and Java containers at build time
