import json
import re
import argparse
import unicodedata
from pathlib import Path

# Common LaTeX replacements
LATEX_MAP = {
    r"\alpha": "alpha",
    r"\beta": "beta",
    r"\gamma": "gamma",
    r"\delta": "delta",
    r"\lambda": "lambda",
    r"\Lambda": "lambda",
    r"\theta": "theta",
    r"\mu": "mu",
    r"\sigma": "sigma",
    r"\phi": "phi",
    r"\pi": "pi"
}


def clean_text(text: str):
    if not text:
        return ""

    # Replace common LaTeX tokens
    for latex, replacement in LATEX_MAP.items():
        text = text.replace(latex, replacement)

    # Remove LaTeX accent commands like \^a, \'e, \"o
    text = re.sub(r"\\[\^`'\"~=.]{1}\{?([a-zA-Z])\}?", r"\1", text)

    # Remove LaTeX math symbols
    text = text.replace("$", "")

    # Remove braces
    text = re.sub(r"[{}]", "", text)

    # Convert subscripts to spaces
    text = text.replace("_", " ")

    # Remove newline characters
    text = text.replace("\n", " ")

    # Remove remaining backslashes
    text = re.sub(r"\\", "", text)

    # Normalize unicode accents
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def clean_dataset(input_file: str, output_file: str):

    input_path = Path(input_file)
    output_path = Path(output_file)

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        return

    processed = 0
    skipped = 0

    print("Starting dataset cleaning...\n")

    with open(input_path, "rb") as f_in, open(output_path, "w", encoding="utf-8") as f_out:

        for line in f_in:

            try:
                # Decode line safely
                line = line.decode("utf-8", errors="ignore").strip()

                # Remove BOM if present
                line = line.lstrip("\ufeff")

                if not line:
                    continue

                data = json.loads(line)

            except Exception:
                skipped += 1
                continue

            # Clean text fields
            data["title"] = clean_text(data.get("title", ""))
            data["abstract"] = clean_text(data.get("abstract", ""))

            f_out.write(json.dumps(data, ensure_ascii=False) + "\n")

            processed += 1

            if processed % 100000 == 0:
                print(f"Processed {processed} papers | skipped {skipped}")

    print("\nCleaning finished")
    print(f"Total processed: {processed}")
    print(f"Total skipped: {skipped}")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Clean arXiv JSONL dataset for search indexing"
    )

    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Input JSONL file"
    )

    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output cleaned JSONL file"
    )

    args = parser.parse_args()

    clean_dataset(args.input, args.output)