import json
import logging
from pathlib import Path
import argparse


def extract_papers(input_file: str, output_file: str):
    input_path = Path(input_file)
    output_path = Path(output_file)

    if not input_path.exists():
        logging.error(f"Input file not found: {input_path}")
        return

    logging.info(f"Starting extraction from {input_path}")

    processed = 0
    kept = 0

    with open(input_path, "r", encoding="utf-8", errors="ignore") as f_in, \
         open(output_path, "w", encoding="utf-8") as f_out:

        for line in f_in:
            try:
                data = json.loads(line)

                # ---------- FILTER ONLY CS PAPERS ----------
                categories = data.get("categories", "")
                if "cs." not in categories:
                    processed += 1
                    continue
                # -------------------------------------------

                paper_id = data.get("id", "")

                title = data.get("title", "")
                title = title.replace("\n", " ").strip()

                abstract = data.get("abstract", "")
                abstract = abstract.replace("\n", " ").strip()

                authors_array = []
                authors_parsed = data.get("authors_parsed", [])

                for author in authors_parsed:
                    if len(author) >= 2:
                        last = author[0]
                        first = author[1]

                        name = f"{first} {last}".strip()
                        if name:
                            authors_array.append(name)

                    elif len(author) == 1 and author[0]:
                        authors_array.append(author[0])

                pdf_url = f"https://arxiv.org/pdf/{paper_id}" if paper_id else ""

                output_record = {
                    "id": paper_id,
                    "title": title,
                    "abstract": abstract,
                    "authors": authors_array,
                    "pdf_url": pdf_url
                }

                f_out.write(json.dumps(output_record, ensure_ascii=False) + "\n")

                kept += 1
                processed += 1

                if processed % 100000 == 0:
                    logging.info(f"Processed {processed} | CS papers kept {kept}")

            except json.JSONDecodeError:
                logging.warning("Skipping malformed JSON line")
                continue

            except Exception as e:
                logging.error(f"Error processing record: {e}")
                continue

    logging.info(f"Extraction complete. Total processed: {processed}, CS papers kept: {kept}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Convert arXiv metadata snapshot to CS-only papers.jsonl format"
    )

    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default="./arxiv-metadata-oai-snapshot.json"
    )

    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="./papers_cs.jsonl"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    extract_papers(args.input, args.output)