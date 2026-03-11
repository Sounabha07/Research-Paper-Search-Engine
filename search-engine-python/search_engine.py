import pickle
import numpy as np
import faiss
import re
from sentence_transformers import SentenceTransformer


def tokenize(text: str):
    return re.findall(r"\b\w+\b", text.lower())


class HybridSearchEngine:

    def __init__(self,
                 bm25_path="bm25.pkl",
                 faiss_path="faiss.index",
                 docs_path="docs.pkl"):

        print("Loading BM25 index...")
        with open(bm25_path, "rb") as f:
            self.bm25 = pickle.load(f)

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

        print("Search engine ready.")

    # --------------------------------------------------
    # HYBRID SEARCH
    # --------------------------------------------------

    def search(self, query: str, top_k: int = 10):

        query_tokens = tokenize(query)

        # BM25
        bm25_scores = self.bm25.get_scores(query_tokens)

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

        output = []

        for score, idx in results[:top_k]:

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

        results = engine.search(query, top_k=5)

        print("\nTop Results:\n")

        for r in results:

            print("Title:", r["title"])

            authors = r.get("authors")

            if isinstance(authors, list):
                authors = ", ".join(authors)

            print("Authors:", authors if authors else "Unknown")

            print("Abstract:", r["abstractText"][:200], "...")

            print("PDF:", f"https://arxiv.org/pdf/{r['id']}.pdf")

            print("Score:", r["score"])
            print()