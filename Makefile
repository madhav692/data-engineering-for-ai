.DEFAULT_GOAL := help
SHELL := /bin/bash

# ---- Developer toolchain (host): uv, ruff, pytest -------------------------------------------

UV ?= uv
RUN := $(UV) run
SCHEMAS_OUT := packages/schemas/generated

# ---- Cairn Stage 0 (containers): everything below `start` runs through docker compose --------

# --project-directory .: paths in base.yml are relative to the repository root, and compose reads
# the root .env (see .env.example).
COMPOSE := docker compose --project-directory . -f infra/compose/base.yml --profile core
EXEC := $(COMPOSE) exec -T
# Sources ingested by `make ingest`, as NAME=PATH inside the api container.
SOURCES := docs-series=/data/docs iceberg-docs=/data/datasets/small/iceberg-docs
PRUNE_FLAG := $(if $(PRUNE),--prune,)
# The optional generator for `make ask`: the host's CAIRN_LLM_* are passed per request.
LLM_ENV := -e CAIRN_LLM_BASE_URL="$(CAIRN_LLM_BASE_URL)" -e CAIRN_LLM_MODEL="$(CAIRN_LLM_MODEL)" \
           -e CAIRN_LLM_API_KEY="$(CAIRN_LLM_API_KEY)" -e CAIRN_LLM_PROVIDER="$(CAIRN_LLM_PROVIDER)" \
           -e CAIRN_LLM_MODEL_VERSION="$(CAIRN_LLM_MODEL_VERSION)"

.PHONY: help setup schemas check-schemas test-contracts test-unit test-platform lint fmt clean \
        start stop down restart logs ps build ingest build-index ask replay feedback eval lag \
        stats metrics shell psql test

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

# ---- toolchain -------------------------------------------------------------------------------

setup: ## Install the toolchain and every workspace package (uv sync)
	$(UV) sync --all-packages --all-groups

schemas: ## Regenerate Avro, Iceberg DDL, JSON Schema and Postgres DDL from the Pydantic models
	$(RUN) python -m cairn_schemas.generate --out $(SCHEMAS_OUT)

check-schemas: ## Fail if the committed generated files differ from the models (CI runs this)
	$(RUN) python -m cairn_schemas.generate --out $(SCHEMAS_OUT) --check

test-contracts: ## Run the contract tests for packages/schemas
	$(RUN) pytest packages/schemas/tests

test-unit: ## Run the platform's unit tests (no database)
	$(RUN) pytest platform/tests/unit

test-platform: ## Run the platform's whole suite on the host (embedded Postgres, hashing embedder)
	$(RUN) pytest platform/tests

lint: ## Ruff lint and format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## Format and auto-fix with Ruff
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

clean: ## Remove caches and build artefacts (never the generated schemas)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache packages/*/dist platform/dist

# ---- Stage 0 -----------------------------------------------------------------------------------

start: ## Build the api image if needed and start postgres, seaweedfs and api (waits until healthy)
	$(COMPOSE) up -d --build --wait
	@$(EXEC) api cairn health

stop: ## Stop the containers; keep the data
	$(COMPOSE) stop

down: ## Remove the containers AND the volumes (database, raw objects, model cache)
	$(COMPOSE) down -v --remove-orphans

restart: ## Recreate the api container (picks up code edits; no rebuild needed) and wait until healthy
	$(COMPOSE) up -d --force-recreate --no-deps --wait api
	@$(EXEC) api cairn health

logs: ## Follow the api's JSON logs
	$(COMPOSE) logs -f api

ps: ## Container status
	$(COMPOSE) ps

build: ## Rebuild the api image (after a dependency change)
	$(COMPOSE) build api

ingest: ## Register, chunk and embed every Markdown file of the sources (PRUNE=1 tombstones removed files)
	$(EXEC) api cairn ingest $(PRUNE_FLAG) $(SOURCES)

build-index: ## Build the vector index for the live model; write an IndexSnapshot; flip the pointer
	$(EXEC) api cairn build-index

ask: ## Ask a question: make ask Q="How does Iceberg hidden partitioning work?"
	@test -n "$(Q)" || (echo 'usage: make ask Q="your question"'; exit 2)
	@$(EXEC) $(LLM_ENV) api cairn ask "$(Q)"

replay: ## Replay a request: make replay REQ=req_...  (add DIFF=req_... to diff two requests)
	@test -n "$(REQ)" || (echo 'usage: make replay REQ=req_... [DIFF=req_...]'; exit 2)
	$(EXEC) api cairn replay $(REQ) $(if $(DIFF),--diff $(DIFF),)

feedback: ## Record feedback: make feedback REQ=req_... KIND=thumbs VALUE=-1 [COMMENT=...] [KEY=dedup-key]
	@test -n "$(REQ)" || (echo 'usage: make feedback REQ=req_... KIND=thumbs VALUE=-1'; exit 2)
	$(EXEC) api cairn feedback $(REQ) --kind $(or $(KIND),thumbs) --value $(or $(VALUE),1) \
	  $(if $(COMMENT),--comment "$(COMMENT)",) $(if $(KEY),--dedup-key $(KEY),)

eval: ## Run the golden set through /ask and write an EvalRun (SET=small)
	$(EXEC) -e CAIRN_GIT_SHA="$$(git rev-parse HEAD 2>/dev/null)" api cairn eval --set $(or $(SET),small)

lag: ## index_lag_seconds per source
	$(EXEC) api cairn lag

requests: ## Recent request ids, newest first (for make replay / make feedback): make requests [N=10]
	$(EXEC) api cairn requests --limit $(or $(N),10)

stats: ## Corpus and database numbers
	$(EXEC) api cairn stats

metrics: ## Scrape /metrics
	curl -s localhost:$(or $(CAIRN_API_PORT),8000)/metrics

shell: ## A shell inside the api container
	$(COMPOSE) exec api bash

psql: ## psql on the cairn database
	$(COMPOSE) exec postgres psql -U cairn -d cairn

test: ## The platform suite against the running containers: real Postgres, real S3, real model
	$(COMPOSE) up -d --wait
	$(EXEC) -e CAIRN_TEST_EMBEDDER=fastembed -e CAIRN_TEST_S3_ENDPOINT=http://seaweedfs:8333 \
	  api pytest platform/tests -q -p no:cacheprovider
