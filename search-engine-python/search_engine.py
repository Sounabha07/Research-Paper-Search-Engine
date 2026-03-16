"""
Lazy-loading Hybrid Search Engine with GPU support.

Loads 1 shard at a time to stay within RAM limits.
"""

import os
import gc
import json
import faiss
import pickle
import numpy as np
import re
from typing import List, Dict, Any, Tuple
from sentence_transformers import SentenceTransformer, CrossEncoder
from symspellpy import SymSpell, Verbosity


def tokenize(text: str) -> List[str]:
    return re.findall(r"\b\w+\b", text.lower())


class HybridSearchEngine:

    def __init__(self, shards_dir: str = "data/shards"):
        self.shards_dir = shards_dir
        self.shards: List[str] = sorted([
            os.path.join(shards_dir, d)
            for d in os.listdir(shards_dir)
            if d.startswith("shard_") and os.path.isdir(os.path.join(shards_dir, d))
        ])

        if not self.shards:
            raise RuntimeError(f"No shards found in {shards_dir}.")

        print(f"[Engine] Found {len(self.shards)} shards.")

        # ── check GPU support ───────────────────────────────────────
        self.use_gpu = False
        self.res = None
        if os.environ.get("USE_GPU", "0") == "1":
            try:
                # faiss.StandardGpuResources is only in faiss-gpu
                self.res = faiss.StandardGpuResources()
                self.use_gpu = True
                print("[Engine] GPU enabled for FAISS.")
            except AttributeError:
                print("[Engine] GPU requested but faiss-gpu not installed. Falling back to CPU.")
        else:
            print("[Engine] CPU mode (set USE_GPU=1 for GPU).")

        # ── build global autocomplete/spell dicts lazily ────────────
        print("[Engine] Building autocomplete trie and spell dictionary...")
        self.trie: Dict[str, Any] = {}
        self.sym_spell = SymSpell(max_dictionary_edit_distance=2)

        total_docs = 0
        word_freq: Dict[str, int] = {}

        for shard_path in self.shards:
            meta_path = os.path.join(shard_path, "meta.json")
            if not os.path.exists(meta_path):
                continue
            
            with open(meta_path, "r", encoding="utf-8") as f:
                shard_meta = json.load(f)

            for doc in shard_meta:
                total_docs += 1
                title = doc.get("title", "")

                # Tri
                node = self.trie
                for char in title.lower():
                    if char not in node:
                        node[char] = {"_titles": []}
                    node = node[char]
                    if len(node["_titles"]) < 5:
                        node["_titles"].append(title)

                # Spell
                for word in tokenize(title):
                    word_freq[word] = word_freq.get(word, 0) + 1

            del shard_meta
            gc.collect()

        for word, freq in word_freq.items():
            self.sym_spell.create_dictionary_entry(word, freq)
            
        del word_freq
        gc.collect()

        self.total_docs = total_docs
        print(f"[Engine] Loaded metadata for {total_docs:,} total docs.")

        # ── load models ─────────────────────────────────────────────
        print("[Engine] Loading SentenceTransformer...")
        # CrossEncoder handles device detection automatically, but ST we force defaults
        self.model = SentenceTransformer("all-MiniLM-L6-v2")

        print("[Engine] Loading CrossEncoder reranker...")
        self.reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

        print("[Engine] Search engine readiness complete.")


    # --------------------------------------------------
    # UTILS
    # --------------------------------------------------

    def _load_shard_bm25(self, shard_path: str):
        with open(os.path.join(shard_path, "bm25.pkl"), "rb") as f:
            return pickle.load(f)

    def _load_shard_faiss(self, shard_path: str):
        idx = faiss.read_index(os.path.join(shard_path, "faiss.index"))
        if self.use_gpu and self.res is not None:
            # Move index to GPU 0
            idx = faiss.index_cpu_to_gpu(self.res, 0, idx)
        return idx
        
    def _load_shard_meta(self, shard_path: str) -> List[Dict[str, Any]]:
        with open(os.path.join(shard_path, "meta.json"), "r", encoding="utf-8") as f:
            return json.load(f)


    # --------------------------------------------------
    # SEARCH
    # --------------------------------------------------

    def search(self, query: str, page: int = 1, size: int = 10) -> List[Dict[str, Any]]:
        
        print(f"Executing search for: '{query}'")

        # 1. Spell correction
        suggestions = self.sym_spell.lookup_compound(query, max_edit_distance=2)
        if suggestions:
            corrected = suggestions[0].term
            if corrected != query.lower():
                print(f"  [Spell] Corrected: '{query}' → '{corrected}'")
                query = corrected

        query_tokens = tokenize(query)

        # 2. Embed query
        query_embedding = self.model.encode(
            [query],
            convert_to_numpy=True
        )
        
        # We need top-K from EACH shard to ensure global recall
        # K per shard can be smaller than global K
        FAISS_K_PER_SHARD = 50
        BM25_K_PER_SHARD = 100

        all_candidates: List[Tuple[float, Dict[str, Any]]] = []

        # 3. Iterate shards
        for shard_path in self.shards:
            
            # --- Load Shard ---
            bm25 = self._load_shard_bm25(shard_path)
            meta = self._load_shard_meta(shard_path)
            shard_size = len(meta)

            # --- BM25 ---
            title_scores = bm25["title"].get_scores(query_tokens)
            abstract_scores = bm25["abstract"].get_scores(query_tokens)
            
            # Hybrid BM25 score formula
            bm25_scores = 3.0 * title_scores + 1.5 * abstract_scores
            
            max_bm25 = np.max(bm25_scores) + 1e-9
            bm25_scores /= max_bm25

            top_bm25_k = min(BM25_K_PER_SHARD, shard_size)
            if top_bm25_k > 0:
                bm25_indices = np.argpartition(bm25_scores, -top_bm25_k)[-top_bm25_k:]
            else:
                bm25_indices = np.array([], dtype=int)

            # --- FAISS ---
            faiss_idx = self._load_shard_faiss(shard_path)
            
            top_faiss_k = min(FAISS_K_PER_SHARD, shard_size)
            if top_faiss_k > 0:
                D, I = faiss_idx.search(query_embedding, top_faiss_k)
                semantic_candidates = I[0]
                semantic_distances = D[0]
                semantic_map = {doc_id: dist for doc_id, dist in zip(semantic_candidates, semantic_distances)}
            else:
                semantic_candidates = []
                semantic_map = {}

            # --- Fuse Shard Results ---
            shard_candidate_indices = set(bm25_indices.tolist()) | set(semantic_candidates)

            for idx in shard_candidate_indices:
                if idx < 0 or idx >= shard_size:
                    continue  # safeguard
                    
                b_score = bm25_scores[idx]
                s_score = 0
                if idx in semantic_map:
                    # D is L2 distance, convert to similarity
                    s_score = 1.0 / (1.0 + semantic_map[idx])

                final_score = float(b_score + 0.75 * s_score)
                all_candidates.append((final_score, meta[idx]))

            # --- Free Shard ---
            del bm25, meta, faiss_idx, title_scores, abstract_scores, bm25_scores
            gc.collect()


        # 4. Global Merge & Rerank
        all_candidates.sort(key=lambda x: x[0], reverse=True)
        
        # Take top 100 globally for cross-encoder
        top_candidates = all_candidates[:100]

        if not top_candidates:
            return []

        pairs = []
        for score, doc in top_candidates:
            text = (doc.get("title", "") + " " + doc.get("abstract", "")).strip()
            pairs.append((query, text))

        rerank_scores = self.reranker.predict(pairs)

        reranked = sorted(
            zip(rerank_scores, [doc for _, doc in top_candidates]),
            key=lambda x: x[0],
            reverse=True
        )

        # 5. Pagination
        if page < 1: page = 1
        if page > 10: page = 10
        start = (page - 1) * size
        end = start + size

        output = []
        for score, doc in reranked[start:end]:
            output.append({
                "id": doc.get("id"),
                "title": doc.get("title"),
                "abstract": doc.get("abstract", ""),
                "authors": doc.get("authors", []),
                "score": float(score)
            })

        return output


    # --------------------------------------------------
    # SIMILAR PAPERS
    # --------------------------------------------------

    def similar(self, paper_id: str, top_k: int = 10) -> List[Dict[str, Any]]:
        
        target_doc = None
        target_vec = None
        
        # 1. Find the paper and its embedding
        for shard_path in self.shards:
            meta = self._load_shard_meta(shard_path)
            
            for i, doc in enumerate(meta):
                if str(doc.get("id")) == str(paper_id):
                    target_doc = doc
                    
                    # try to get from FAISS if possible, or re-encode
                    try:
                        faiss_idx = self._load_shard_faiss(shard_path)
                        vec = np.zeros((1, faiss_idx.d), dtype=np.float32)
                        
                        # Note: IndexIVFFlat reconstruct might fail if not supported/enabled.
                        # We fallback to encoding text if reconstruct throws.
                        faiss_idx.reconstruct(i, vec[0])
                        target_vec = vec
                    except Exception:
                        text = f"{doc.get('title','')} {doc.get('abstract','')}"
                        target_vec = self.model.encode([text], convert_to_numpy=True)
                    
                    del faiss_idx
                    break
                    
            del meta
            gc.collect()
            
            if target_doc:
                break
                
        if target_vec is None:
            return []
            
        # 2. Search all shards
        all_results = []
        
        for shard_path in self.shards:
            faiss_idx = self._load_shard_faiss(shard_path)
            meta = self._load_shard_meta(shard_path)
            shard_size = len(meta)
            
            k = min(top_k + 1, shard_size)
            if k > 0:
                D, I = faiss_idx.search(target_vec, k)
                
                for dist, idx in zip(D[0], I[0]):
                    if idx < 0 or idx >= shard_size:
                        continue
                        
                    doc = meta[idx]
                    if str(doc.get("id")) == str(paper_id):
                        continue
                        
                    all_results.append((dist, doc))
                    
            del faiss_idx, meta
            gc.collect()
            
        # 3. Sort by L2 distance (lowest is best)
        all_results.sort(key=lambda x: x[0])
        
        # 4. Format output
        output = []
        for dist, doc in all_results[:top_k]:
            output.append({
                "id": doc.get("id"),
                "title": doc.get("title"),
                "abstract": doc.get("abstract", ""),
                "authors": doc.get("authors", [])
            })
            
        return output


    # --------------------------------------------------
    # AUTOCOMPLETE
    # --------------------------------------------------

    def autocomplete(self, prefix: str) -> List[str]:

        suggestions = self.sym_spell.lookup(prefix, verbosity=Verbosity.TOP)
        if suggestions:
            prefix = suggestions[0].term

        node = self.trie
        for char in prefix.lower():
            if char not in node:
                return []
            node = node[char]

        return node.get("_titles", [])


# --------------------------------------------------
# LOCAL TEST
# --------------------------------------------------

if __name__ == "__main__":
    
    # Needs shards built first!
    try:
        engine = HybridSearchEngine()
        
        while True:
            query = input("\nSearch query (or Enter to quit): ")
            if not query:
                break
                
            results = engine.search(query, page=1)
            print("\nTop Results:\n")
            
            for r in results:
                print("Title:", r["title"])
                authors = r.get("authors", [])
                if isinstance(authors, list):
                    authors = ", ".join(authors)
                print("Authors:", authors if authors else "Unknown")
                print("Score:", r["score"])
                print()
                
    except RuntimeError as e:
        print(f"Error: {e}")
        print("Please run `indexer.py --max-docs 2000` first to generate test shards.")