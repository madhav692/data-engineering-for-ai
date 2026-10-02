.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
RUN := $(UV) run
SCHEMAS_OUT := packages/schemas/generated

.PHONY: help setup schemas check-schemas test-contracts lint fmt clean

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup: ## Install the toolchain and every workspace package (uv sync)
	$(UV) sync --all-packages --all-groups

schemas: ## Regenerate Avro, Iceberg DDL and JSON Schema from the Pydantic models
	$(RUN) python -m cairn_schemas.generate --out $(SCHEMAS_OUT)

check-schemas: ## Fail if the committed generated files differ from the models (CI runs this)
	$(RUN) python -m cairn_schemas.generate --out $(SCHEMAS_OUT) --check

test-contracts: ## Run the contract tests for packages/schemas
	$(RUN) pytest packages/schemas/tests

lint: ## Ruff lint and format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt: ## Format and auto-fix with Ruff
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

clean: ## Remove caches and build artefacts (never the generated schemas)
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache packages/*/dist
