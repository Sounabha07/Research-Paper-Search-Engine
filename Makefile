# Stop Git Bash / MSYS on Windows from rewriting container paths like /backups.
export MSYS_NO_PATHCONV := 1

COMPOSE := docker compose
# One-off backend container without starting other services (tests, lint, model check).
BACKEND := $(COMPOSE) run --rm --no-deps api

.PHONY: help up down logs migrate status check-models test lint backup restore \
        ingest-metadata landmarks warm score embed nightly eval

help:
	@echo "up | down | logs [S=service] | migrate | status | check-models | test | lint"
	@echo "backup | restore FILE=<name>.dump"
	@echo "ingest-metadata | landmarks | warm TOPIC=... | score | embed | nightly | eval  (later milestones)"

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

ingest-metadata landmarks score embed:
	@echo "make $@ arrives in milestone 2" && exit 1

warm:
	@echo "make warm arrives in milestone 3" && exit 1

nightly eval:
	@echo "make $@ arrives in milestone 8" && exit 1
