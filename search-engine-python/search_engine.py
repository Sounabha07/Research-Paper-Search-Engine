import pickle
import numpy as np
import faiss
import re
from sentence_transformers import SentenceTransformer, CrossEncoder
from symspellpy import SymSpell, Verbosity


def tokenize(text: str):
    return re.findall(r"\b\w+\b", text.lower())


class HybridSearchEngine:

    def __init__(self,
                 bm25_title_path="bm25_title.pkl",
                 bm25_abstract_path="bm25_abstract.pkl",
                 faiss_path="faiss.index",
                 docs_path="docs.pkl"):

        print("Loading BM25 title index...")
        with open(bm25_title_path, "rb") as f:
            self.bm25_title = pickle.load(f)

        print("Loading BM25 abstract index...")
        with open(bm25_abstract_path, "rb") as f:
            self.bm25_abstract = pickle.load(f)

        print("Loading FAISS index...")
        self.index = faiss.read_index(faiss_path)

        print("Loading document metadata...")
        with open(docs_path, "rb") as f:
            self.docs = pickle.load(f)

        print("Building autocomplete trie...")
        self.trie = {}

        for doc in self.docs:

            title = doc.get("title", "")
            node = self.trie

            for char in title.lower():

                if char not in node:
                    node[char] = {"_titles": []}

                node = node[char]

                if len(node["_titles"]) < 5:
                    node["_titles"].append(title)

        print("Loading embedding model...")
        self.model = SentenceTransformer("all-MiniLM-L6-v2")

        print("Loading cross-encoder reranker...")
        self.reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

        print("Building spell correction dictionary...")
        self.sym_spell = SymSpell(max_dictionary_edit_distance=2)

        # Build frequency dictionary from paper titles
        word_freq = {}
        for doc in self.docs:
            for word in tokenize(doc.get("title", "")):
                word_freq[word] = word_freq.get(word, 0) + 1

        for word, freq in word_freq.items():
            self.sym_spell.create_dictionary_entry(word, freq)

        print("Search engine ready.")

    # --------------------------------------------------
    # HYBRID SEARCH
    # --------------------------------------------------

    def search(self, query: str, page: int = 1, size: int = 10):

        # Spell correction
        suggestions = self.sym_spell.lookup_compound(query, max_edit_distance=2)
        if suggestions:
            corrected = suggestions[0].term
            if corrected != query.lower():
                print(f"Spell corrected: '{query}' → '{corrected}'")
                query = corrected

        query_tokens = tokenize(query)

        # Field-weighted BM25
        title_scores = self.bm25_title.get_scores(query_tokens)
        abstract_scores = self.bm25_abstract.get_scores(query_tokens)

        bm25_scores = 3.0 * title_scores + 1.5 * abstract_scores

        max_score = np.max(bm25_scores) + 1e-9
        bm25_scores = bm25_scores / max_score

        bm25_top = np.argsort(bm25_scores)[::-1][:200]

        # Semantic
        query_embedding = self.model.encode(
            [query],
            convert_to_numpy=True
        )

        D, I = self.index.search(query_embedding, 200)

        semantic_candidates = I[0]

        semantic_map = {doc_id: i for i, doc_id in enumerate(semantic_candidates)}

        candidates = set(bm25_top.tolist()) | set(semantic_candidates.tolist())

        results = []

        for idx in candidates:

            bm25_score = bm25_scores[idx]

            semantic_score = 0

            if idx in semantic_map:
                pos = semantic_map[idx]
                semantic_score = 1 / (1 + D[0][pos])

            final_score = bm25_score + 0.75 * semantic_score

            results.append((final_score, idx))

        results.sort(reverse=True)

        # Take top 100 candidates for cross-encoder reranking
        top_candidates = results[:100]

        pairs = []
        candidate_indices = []

        for score, idx in top_candidates:
            doc = self.docs[idx]
            text = (doc.get("title", "") + " " + doc.get("abstract", "")).strip()
            pairs.append((query, text))
            candidate_indices.append(idx)

        # Cross-encoder reranking
        rerank_scores = self.reranker.predict(pairs)

        reranked = sorted(
            zip(rerank_scores, candidate_indices),
            reverse=True
        )

        # Pagination (1-based, clamped to [1, 10])
        if page < 1:
            page = 1
        if page > 10:
            page = 10
        size = 10
        start = (page - 1) * size
        end = start + size

        output = []

        for score, idx in reranked[start:end]:

            doc = self.docs[idx]

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

    def similar(self, paper_id: str, top_k: int = 10):

        target_idx = -1

        for i, doc in enumerate(self.docs):

            if str(doc.get("id")) == str(paper_id):
                target_idx = i
                break

        if target_idx == -1:
            return []

        try:

            vec = np.zeros((1, self.index.d), dtype=np.float32)
            self.index.reconstruct(target_idx, vec[0])

        except Exception:

            text = f"{self.docs[target_idx]['title']} {self.docs[target_idx].get('abstract','')}"
            vec = self.model.encode([text], convert_to_numpy=True)

        D, I = self.index.search(vec, top_k + 1)

        results = []

        for idx in I[0]:

            if idx == target_idx:
                continue

            doc = self.docs[idx]

            results.append({
                "id": doc.get("id"),
                "title": doc.get("title"),
                "abstract": doc.get("abstract", ""),
                "authors": doc.get("authors", [])
            })

            if len(results) >= top_k:
                break

        return results

    # --------------------------------------------------
    # AUTOCOMPLETE
    # --------------------------------------------------

    def autocomplete(self, prefix: str):

        # Apply spell correction to handle typos
        suggestions = self.sym_spell.lookup(prefix, verbosity=0)

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

    engine = HybridSearchEngine()

    while True:

        query = input("\nSearch query: ")

        if not query:
            break

        results = engine.search(query, page=1)

        print("\nTop Results:\n")

        for r in results:

            print("Title:", r["title"])

            authors = r.get("authors")

            if isinstance(authors, list):
                authors = ", ".join(authors)

            print("Authors:", authors if authors else "Unknown")

            print("Abstract:", r.get("abstract", "")[:200], "...")

            print("PDF:", f"https://arxiv.org/pdf/{r['id']}.pdf")

            print("Score:", r["score"])
            print()