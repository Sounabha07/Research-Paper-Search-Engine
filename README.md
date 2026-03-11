# Distributed Research Paper Search Engine

A state-of-the-art, scalable, and distributed search engine for research papers. Features a hybrid retrieval approach combining BM25 keyword search and MiniLM-L6 vector embeddings for unparalleled semantic search performance.

## 🚀 Architecture overview

This system is built using a modern decoupled microservices architecture:

- **Frontend**: A sleek, premium React + TypeScript UI built with Vite and Lucide React.
- **Backend (Java)**: A Spring Boot application serving as the primary API gateway, orchestrating Redis caching and interacting with the Python Search Engine via gRPC.
- **Search Engine (Python)**: High-performance indexing and retrieval service combining FAISS for vector search and rank-bm25 for keyword search. Exposed via a gRPC server.
- **Cache**: Redis is used to cache frequent search queries for lightning-fast responses.

## 📁 Repository Structure

```
research-paper-search-engine/
│
├── frontend/               # React + TS + Vite web application
├── backend-java/           # Spring Boot REST API + gRPC Client
├── search-engine-python/   # FAISS + BM25 indexing and gRPC Server
├── dataset/                # papers.jsonl dataset files
├── docker/                 # Service Dockerfiles
├── proto/                  # search.proto definitions
└── docker-compose.yml      # Orchestration
```

## 🛠️ Quick Start (Docker Orchestration)

The easiest way to run the entire system is via Docker Compose.

1.  **Start all services**
    ```bash
    docker-compose up --build
    ```
    This will automatically build the images, compile the protobuf definitions, generate the FAISS and BM25 indexes (if they don't exist), and launch the components.

2.  **Access the Application**
    Open your browser to `http://localhost:3000`.

## 💻 Manual Setup

If you prefer to run the services individually without Docker, follow these steps:

### 1. Python Search Engine

```bash
cd search-engine-python
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Generate gRPC stubs
python -m grpc_tools.protoc -I../proto --python_out=. --grpc_python_out=. ../proto/search.proto

# Build indexes and start server
python indexer.py
python grpc_server.py
```

### 2. Redis
Ensure you have Redis running locally on default port `6379`.

### 3. Java Backend

```bash
cd backend-java
# Ensure Python server is running first, then build and run Java backend
mvn clean install
mvn spring-boot:run
```

### 4. React Frontend

```bash
cd frontend
npm install
npm run dev
```

## 🔍 Hybrid Search Mechanism

The system implements a weighted hybrid search. Given a query:
1.  **Lexical Search (35%)**: Tokenizes the query and evaluates via `rank_bm25` against the paper corpus.
2.  **Semantic Search (65%)**: Converts the query to an embedding via `sentence-transformers/all-MiniLM-L6-v2` and searches the `FAISS` index for cosine distance.
3.  **Reranking**: Scores are min-max normalized and combined using the specified weighting to provide highly relevant, final search results.

## 📝 Demo Dataset

By default, an initial `dataset/papers.jsonl` contains sample documents used to verify the search capabilities immediately upon launch.
