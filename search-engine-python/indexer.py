import os
import ujson
import pickle
import numpy as np
import faiss
import re
import torch
from tqdm import tqdm
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer


DATASET_PATH = "../dataset/cs_focused_dataset_clean.jsonl"
BM25_TITLE_PATH = "bm25_title.pkl"
BM25_ABSTRACT_PATH = "bm25_abstract.pkl"
FAISS_INDEX_PATH = "faiss.index"
DOCS_PATH = "docs.pkl"

EMBED_BATCH_SIZE = 128
FAISS_BATCH_SIZE = 512


def tokenize(text):
    return re.findall(r"\b\w+\b", text.lower())


def build_indexes():

    print("Loading embedding model...")

    device = "cuda" if torch.cuda.is_available() else "cpu"

    model = SentenceTransformer(
        "all-MiniLM-L6-v2",
        device=device
    )

    dimension = model.get_sentence_embedding_dimension()

    print("Initializing FAISS index...")

    index = faiss.IndexHNSWFlat(dimension, 32)
    index.hnsw.efConstruction = 200
    index.hnsw.efSearch = 64

    docs = []
    tokenized_titles = []
    tokenized_abstracts = []

    batch_texts = []

    if not os.path.exists(DATASET_PATH):
        raise FileNotFoundError(f"Dataset not found: {DATASET_PATH}")

    print("Streaming dataset and building indexes...")

    with open(DATASET_PATH, "r", encoding="utf-8", errors="ignore") as f:

        for line in tqdm(f):

            if not line.strip():
                continue

            try:
                paper = ujson.loads(line)
            except ValueError:
                continue

            title = paper.get("title", "")
            abstract = paper.get("abstract", "")

            text = f"{title} {abstract}"

            docs.append({
                "id": paper.get("id"),
                "title": title,
                "abstract": abstract,
                "authors": paper.get("authors"),
                "pdf_url": paper.get("pdf_url")
            })

            tokenized_titles.append(tokenize(title))
            tokenized_abstracts.append(tokenize(abstract))

            batch_texts.append(text)

            if len(batch_texts) >= FAISS_BATCH_SIZE:

                embeddings = model.encode(
                    batch_texts,
                    batch_size=EMBED_BATCH_SIZE,
                    convert_to_numpy=True,
                    show_progress_bar=False
                )

                faiss.normalize_L2(embeddings)

                index.add(embeddings)

                batch_texts = []

    if batch_texts:

        embeddings = model.encode(
            batch_texts,
            batch_size=EMBED_BATCH_SIZE,
            convert_to_numpy=True,
            show_progress_bar=False
        )

        faiss.normalize_L2(embeddings)

        index.add(embeddings)

    print("Building BM25 title index...")
    bm25_title = BM25Okapi(tokenized_titles)

    with open(BM25_TITLE_PATH, "wb") as f:
        pickle.dump(bm25_title, f)
    print("Saved BM25 title index.")

    print("Building BM25 abstract index...")
    bm25_abstract = BM25Okapi(tokenized_abstracts)

    with open(BM25_ABSTRACT_PATH, "wb") as f:
        pickle.dump(bm25_abstract, f)
    print("Saved BM25 abstract index.")

    print("Saving FAISS index...")

    faiss.write_index(index, FAISS_INDEX_PATH)

    print("Saved FAISS index.")

    print("Saving document metadata...")

    with open(DOCS_PATH, "wb") as f:
        pickle.dump(docs, f)

    print("Saved document metadata.")

    print("Indexing complete.")


if __name__ == "__main__":
    build_indexes()