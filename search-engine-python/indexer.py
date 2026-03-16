"""
Sharded Indexer for ~1.1M arXiv papers.

Creates 8 shards, each containing:
  - bm25.pkl        (BM25Okapi index for title + abstract)
  - faiss.index      (FAISS IndexIVFFlat)
  - meta.json        (paper metadata list)
  - embeddings.npy   (float32 embedding matrix)

Processes one shard at a time to stay within RAM limits.
"""

import os
import sys
import gc
import math
import json
import pickle
import argparse
import re

import numpy as np
import faiss
import ujson
import torch
from tqdm import tqdm
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer


# ── defaults ────────────────────────────────────────────────────────
DATASET_PATH = os.environ.get("DATASET_PATH", "../dataset/papers_cs_clean.jsonl")
OUTPUT_DIR = os.environ.get("INDEX_OUTPUT_DIR", "data/shards")
NUM_SHARDS = int(os.environ.get("NUM_SHARDS", "8"))
EMBED_BATCH_SIZE = 128

# ── helpers ─────────────────────────────────────────────────────────

def tokenize(text: str):
    return re.findall(r"\b\w+\b", text.lower())


def count_lines(path: str) -> int:
    """Fast line count without loading entire file into memory."""
    count = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for _ in f:
            count += 1
    return count


def choose_nlist(n: int) -> int:
    """Pick IVF nlist based on shard size.  sqrt(n) clamped to [16, 4096]."""
    return max(16, min(4096, int(math.sqrt(n))))


# ── main ────────────────────────────────────────────────────────────

def build_indexes(max_docs: int = 0):
    """
    Build sharded indexes from the JSONL dataset.

    Args:
        max_docs: If > 0, only index this many documents (for testing).
    """

    # ── count total docs ────────────────────────────────────────────
    print(f"[indexer] Dataset : {DATASET_PATH}")
    print(f"[indexer] Shards  : {NUM_SHARDS}")
    print(f"[indexer] Output  : {OUTPUT_DIR}")

    print("[indexer] Counting lines …")
    total_lines = count_lines(DATASET_PATH)
    if max_docs > 0:
        total_lines = min(total_lines, max_docs)
    print(f"[indexer] Total documents to index: {total_lines:,}")

    shard_size = math.ceil(total_lines / NUM_SHARDS)
    print(f"[indexer] Shard size ≈ {shard_size:,}")

    # ── load embedding model (kept across shards — only ~80 MB) ────
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[indexer] Loading SentenceTransformer on {device} …")
    model = SentenceTransformer("all-MiniLM-L6-v2", device=device)
    dim = model.get_sentence_embedding_dimension()
    print(f"[indexer] Embedding dimension: {dim}")

    # ── shard loop ──────────────────────────────────────────────────
    global_idx = 0  # tracks position across the whole dataset

    for shard_id in range(NUM_SHARDS):

        shard_start = shard_id * shard_size
        shard_end = min(shard_start + shard_size, total_lines)
        expected = shard_end - shard_start

        if expected <= 0:
            print(f"[shard {shard_id}] No documents — skipping.")
            continue

        print(f"\n{'='*60}")
        print(f"[shard {shard_id}] Building  ({shard_start:,} → {shard_end:,},  {expected:,} docs)")
        print(f"{'='*60}")

        # ── collect shard data ──────────────────────────────────────
        metadata = []
        tokenized_corpus = []  # list of (title_tokens, abstract_tokens)
        texts_for_embed = []

        with open(DATASET_PATH, "r", encoding="utf-8", errors="ignore") as f:

            for line_no, line in enumerate(f):

                if line_no < shard_start:
                    continue
                if line_no >= shard_end:
                    break

                if not line.strip():
                    continue

                try:
                    paper = ujson.loads(line)
                except ValueError:
                    continue

                title = paper.get("title", "")
                abstract = paper.get("abstract", "")

                metadata.append({
                    "id": paper.get("id"),
                    "title": title,
                    "abstract": abstract,
                    "authors": paper.get("authors"),
                    "pdf_url": paper.get("pdf_url"),
                })

                tokenized_corpus.append((tokenize(title), tokenize(abstract)))
                texts_for_embed.append(f"{title} {abstract}")

        actual = len(metadata)
        print(f"[shard {shard_id}] Loaded {actual:,} papers from disk.")

        if actual == 0:
            print(f"[shard {shard_id}] Empty — skipping.")
            continue

        # ── create shard dir ────────────────────────────────────────
        shard_dir = os.path.join(OUTPUT_DIR, f"shard_{shard_id}")
        os.makedirs(shard_dir, exist_ok=True)

        # ── save metadata ───────────────────────────────────────────
        meta_path = os.path.join(shard_dir, "meta.json")
        print(f"[shard {shard_id}] Saving metadata → {meta_path}")
        with open(meta_path, "w", encoding="utf-8") as f:
            ujson.dump(metadata, f)

        # ── build BM25 ──────────────────────────────────────────────
        print(f"[shard {shard_id}] Building BM25 index …")

        title_tokens = [t for t, _ in tokenized_corpus]
        abstract_tokens = [a for _, a in tokenized_corpus]

        bm25_title = BM25Okapi(title_tokens)
        bm25_abstract = BM25Okapi(abstract_tokens)

        bm25_path = os.path.join(shard_dir, "bm25.pkl")
        with open(bm25_path, "wb") as f:
            pickle.dump({"title": bm25_title, "abstract": bm25_abstract}, f)
        print(f"[shard {shard_id}] Saved BM25 → {bm25_path}")

        # free BM25 objects early
        del bm25_title, bm25_abstract, title_tokens, abstract_tokens, tokenized_corpus
        gc.collect()

        # ── compute embeddings ──────────────────────────────────────
        print(f"[shard {shard_id}] Computing embeddings ({actual:,} texts) …")

        all_embeddings = []
        for batch_start in tqdm(range(0, actual, EMBED_BATCH_SIZE),
                                desc=f"shard {shard_id} embed"):
            batch = texts_for_embed[batch_start : batch_start + EMBED_BATCH_SIZE]
            emb = model.encode(batch, batch_size=EMBED_BATCH_SIZE,
                               convert_to_numpy=True, show_progress_bar=False)
            faiss.normalize_L2(emb)
            all_embeddings.append(emb)

        embeddings = np.vstack(all_embeddings).astype(np.float32)
        del all_embeddings, texts_for_embed
        gc.collect()

        # save embeddings
        emb_path = os.path.join(shard_dir, "embeddings.npy")
        print(f"[shard {shard_id}] Saving embeddings → {emb_path}  shape={embeddings.shape}")
        np.save(emb_path, embeddings)

        # ── build FAISS IVF index ───────────────────────────────────
        nlist = choose_nlist(actual)
        print(f"[shard {shard_id}] Building FAISS IVFFlat  (nlist={nlist}) …")

        quantizer = faiss.IndexFlatL2(dim)
        index = faiss.IndexIVFFlat(quantizer, dim, nlist)

        # IVF requires training on representative vectors
        train_size = min(actual, max(nlist * 40, 50_000))
        train_vecs = embeddings[:train_size]
        print(f"[shard {shard_id}] Training IVF on {train_size:,} vectors …")
        index.train(train_vecs)

        # add all vectors
        print(f"[shard {shard_id}] Adding {actual:,} vectors to index …")
        index.add(embeddings)

        faiss_path = os.path.join(shard_dir, "faiss.index")
        faiss.write_index(index, faiss_path)
        print(f"[shard {shard_id}] Saved FAISS index → {faiss_path}")

        # ── free everything ─────────────────────────────────────────
        del embeddings, index, quantizer, train_vecs, metadata
        gc.collect()

        print(f"[shard {shard_id}] ✓ Done.  Memory freed.\n")

    print("=" * 60)
    print("[indexer] All shards built successfully.")
    print(f"[indexer] Output directory: {OUTPUT_DIR}")
    print("=" * 60)


# ── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build sharded search indexes")
    parser.add_argument("--max-docs", type=int, default=0,
                        help="Limit number of documents (0 = all)")
    parser.add_argument("--dataset", type=str, default=None,
                        help="Override dataset path")
    parser.add_argument("--output", type=str, default=None,
                        help="Override output directory")
    parser.add_argument("--shards", type=int, default=None,
                        help="Override number of shards")
    args = parser.parse_args()

    if args.dataset:
        DATASET_PATH = args.dataset
    if args.output:
        OUTPUT_DIR = args.output
    if args.shards:
        NUM_SHARDS = args.shards

    build_indexes(max_docs=args.max_docs)