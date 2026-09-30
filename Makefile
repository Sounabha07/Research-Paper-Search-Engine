# Stop Git Bash / MSYS on Windows from rewriting container paths like /backups.
export MSYS_NO_PATHCONV := 1

COMPOSE := docker compose
# One-off backend container without starting other services (tests, lint, model check).
BACKEND := $(COMPOSE) run --rm --no-deps api

.PHONY: help up down logs migrate status check-models test lint backup restore \
        ingest-metadata landmarks warm fetch fulltext-report catalog resolve-refs score embed eval-verifier nightly eval

help:
	@echo "up | down | logs [S=service] | migrate | status | check-models | test | lint"
	@echo "backup | restore FILE=<name>.dump"
	@echo "ingest-metadata [ARGS=--dry-run|--skip-download] | score | embed"
	@echo "landmarks | warm TOPIC=... [N=30] | fetch IDS=\"...\" | fulltext-report | catalog | resolve-refs"
	@echo "eval-verifier [ARGS=--legacy|--no-names]"
	@echo "nightly | eval [ARGS=\"--answers | --no-keyword | --no-vector | --no-rerank | --embedding M | --chunk-size N\"]"

up:
	$(COMPOSE) up -d --build postgres redis grobid
	$(COMPOSE) run --rm api alembic upgrade head
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200 $(S)

migrate:
	$(COMPOSE) run --rm api alembic upgrade head

status:
	$(COMPOSE) ps
	@curl -s http://localhost:8000/health/ready || echo "api not reachable on localhost:8000"
	@echo

check-models:
	$(BACKEND) python -m core.llm check

test:
	$(COMPOSE) build api
	$(BACKEND) pytest -q

lint:
	$(COMPOSE) build api
	$(BACKEND) sh -c "ruff check . && ruff format --check . && mypy ."

backup:
	$(COMPOSE) exec -T postgres sh -c 'f=/backups/arxiv-$$(date +%Y%m%d-%H%M%S).dump && pg_dump -U arxiv -Fc -f $$f arxiv && ls -lh $$f'

restore:
ifndef FILE
	$(error Usage: make restore FILE=<name>.dump (a file in DATA_DIR/backups))
endif
	$(COMPOSE) exec -T postgres pg_restore -U arxiv -d arxiv --clean --if-exists --no-owner /backups/$(FILE)

# Metadata pass: download/refresh the Kaggle snapshot, stream it, load tier 1, print counts, score.
# ARGS="--dry-run" counts only; ARGS="--skip-download" reuses the zip in DATA_DIR/raw.
ingest-metadata:
	$(COMPOSE) run --rm worker python -m ingest.metadata $(ARGS)

score:
	$(COMPOSE) run --rm worker python -m ingest.scores

embed:
	$(COMPOSE) run --rm worker python -m ingest.abstract_embed

# Full text on demand (milestone 3). Fetches go through the single fetcher service.
landmarks:
	$(COMPOSE) run --rm worker python -m ingest.fulltext_cli landmarks

warm:
ifndef TOPIC
	$(error Usage: make warm TOPIC="..." [N=30])
endif
	$(COMPOSE) run --rm worker python -m ingest.fulltext_cli warm "$(TOPIC)" $(if $(N),--n $(N))

fetch:
ifndef IDS
	$(error Usage: make fetch IDS="2106.09685 2305.14314")
endif
	$(COMPOSE) run --rm worker python -m ingest.fulltext_cli fetch $(IDS)

fulltext-report:
	$(COMPOSE) run --rm worker python -m ingest.fulltext_cli report

# Compact catalog of every arXiv record (ingest-metadata also refreshes it); then re-resolve references.
catalog:
	$(COMPOSE) run --rm worker python -m ingest.catalog $(ARGS)

resolve-refs:
	$(COMPOSE) run --rm worker python -m ingest.fulltext_cli resolve-all

# Adversarial verifier test (eval/verifier_adversarial.yaml). ARGS="--legacy" or "--no-names" for comparisons.
EVAL_RUN := $(COMPOSE) run --rm -v ./eval:/app/eval -e PYTHONPATH=/app/backend worker python
eval-verifier:
	$(EVAL_RUN) /app/eval/verifier_adversarial.py $(ARGS)

# Nightly job now (normally 02:00 UTC in the worker): OAI-PMH harvest in the fetcher, then embeddings and scores.
nightly:
	$(COMPOSE) run --rm --no-deps worker python -m ingest.nightly

# Eval (eval/questions.yaml). Retrieval metrics and ablations make no Gemini calls; --answers runs the answer
# pipeline through the API (response cache: reruns are free). ARGS e.g. "--no-rerank", "--answers".
eval:
	$(EVAL_RUN) /app/eval/run_eval.py $(ARGS)
