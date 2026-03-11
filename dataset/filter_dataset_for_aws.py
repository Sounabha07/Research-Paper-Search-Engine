import json
from collections import defaultdict

input_file = "arxiv-metadata-oai-snapshot.json"
output_file = "cs_focused_dataset.jsonl"

# category limits
limits = {
    "cs.AI": 6000,
    "cs.LG": 6000,
    "cs.CL": 4000,
    "cs.CV": 4000,
    "cs.NE": 3000,

    "cs.DB": 5000,
    "cs.IR": 5000,
    "cs.DC": 4000,

    "cs.DS": 6000,
    "cs.GT": 3000,
    "cs.CG": 3000,

    "cs.CC": 4000,
    "cs.FL": 3000,
    "cs.LO": 3000,
    "cs.DM": 4000,

    "cs.SE": 4000,
    "cs.CR": 4000,
    "cs.NA": 3000
}

counts = defaultdict(int)
processed = 0

with open(input_file, "r", encoding="utf-8") as fin, open(output_file, "w", encoding="utf-8") as fout:

    for line in fin:
        processed += 1

        try:
            paper = json.loads(line)
        except:
            continue

        categories = paper.get("categories", "").split()

        cs_cat = None
        for c in categories:
            if c.startswith("cs."):
                cs_cat = c
                break

        if cs_cat is None:
            continue

        if cs_cat not in limits:
            continue

        if counts[cs_cat] >= limits[cs_cat]:
            continue

        cleaned = {
            "id": paper.get("id"),
            "title": paper.get("title"),
            "abstract": paper.get("abstract"),
            "authors": paper.get("authors", ""),
            "category": cs_cat,
            "date": paper.get("update_date")
        }

        fout.write(json.dumps(cleaned, ensure_ascii=False) + "\n")

        counts[cs_cat] += 1

        if processed % 100000 == 0:
            print(f"Processed {processed} papers")

print("\nSaved papers per category:")
for k in sorted(counts):
    print(k, counts[k])

print("\nTotal:", sum(counts.values()))