import json
from collections import defaultdict

input_file = "arxiv-metadata-oai-snapshot.json"
output_file = "cs_focused_dataset.jsonl"

# category limits
limits = {
    "cs.AI": 600,
    "cs.LG": 600,
    "cs.CL": 400,
    "cs.CV": 400,
    "cs.NE": 300,

    "cs.DB": 500,
    "cs.IR": 500,
    "cs.DC": 400,

    "cs.DS": 600,
    "cs.GT": 300,
    "cs.CG": 300,

    "cs.CC": 400,
    "cs.FL": 300,
    "cs.LO": 300,
    "cs.DM": 400,

    "cs.SE": 400,
    "cs.CR": 400,
    "cs.NA": 300
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