# =============================================================================
# agent-platform 顶层 Makefile
# =============================================================================

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

FOUNDATION_DIR := /Users/chenzefeng/Development/saas-foundation
COMPOSE_FOUNDATION := docker compose -f $(FOUNDATION_DIR)/deploy/docker-compose/dev.yml --env-file $(FOUNDATION_DIR)/deploy/docker-compose/.env
COMPOSE_AGENT := docker compose -f deploy/docker-compose/agent-platform.yml --env-file $(FOUNDATION_DIR)/deploy/docker-compose/.env --env-file deploy/docker-compose/.env

SERVICES := bff session-orchestrator agent-scheduler sandbox-pool skill-store knowledge-ingest mcp-registry

BOLD := \033[1m
GREEN := \033[32m
YELLOW := \033[33m
RED := \033[31m
RESET := \033[0m

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-22s$(RESET) %s\n", $$1, $$2}'

# ---- Foundation ----
.PHONY: foundation-up
foundation-up: ## Start foundation dev stack
	cd $(FOUNDATION_DIR) && $(MAKE) up

.PHONY: foundation-down
foundation-down: ## Stop foundation stack
	cd $(FOUNDATION_DIR) && $(MAKE) down

# ---- Agent Platform ----
.PHONY: env
env: ## Copy .env.example to .env if missing
	@if [ ! -f deploy/docker-compose/.env ]; then \
	  cp deploy/docker-compose/.env.example deploy/docker-compose/.env; \
	  echo "$(YELLOW)Created deploy/docker-compose/.env — adjust values before use$(RESET)"; \
	fi

.PHONY: up
up: env ## Start foundation + agent services
	$(COMPOSE_FOUNDATION) up -d
	$(COMPOSE_AGENT) up -d --build
	@echo "$(GREEN)Agent services starting on ports 8005-8007, web on 5173$(RESET)"

.PHONY: agent-up
agent-up: env ## Start agent services only (foundation must be running)
	$(COMPOSE_AGENT) up -d --build

.PHONY: down
down: ## Stop agent services
	$(COMPOSE_AGENT) down

.PHONY: nuke
nuke: ## Stop everything + destroy volumes
	$(COMPOSE_AGENT) down -v
	$(COMPOSE_FOUNDATION) down -v

.PHONY: ps
ps: ## Show agent service status
	$(COMPOSE_AGENT) ps

.PHONY: logs
logs: ## Follow logs for SVC (e.g. make logs SVC=bff)
	$(COMPOSE_AGENT) logs -f $(SVC)

# ---- Foundation SDK install (path dependency) ----
#
# Business services `import foundation_sdk` but the SDK lives in a sibling
# repo, not on PyPI yet.  This target installs it editable into each
# service's poetry venv so local dev / CI mirror each other.  Required
# after `poetry install` in any fresh checkout.
.PHONY: install-sdk
install-sdk: ## Install foundation_sdk[fastapi] editable into each service venv
	@if [ ! -d "$(FOUNDATION_DIR)/sdks/python" ]; then \
	  echo "$(RED)foundation SDK not found at $(FOUNDATION_DIR)/sdks/python$(RESET)"; \
	  exit 1; \
	fi
	@for svc in $(SERVICES); do \
	  echo "$(BOLD)→ install SDK into $$svc venv$(RESET)"; \
	  cd services/$$svc && \
	    poetry run pip install -e "$(FOUNDATION_DIR)/sdks/python[fastapi]" && \
	    cd - > /dev/null; \
	done

.PHONY: bootstrap
bootstrap: ## Full local-dev bootstrap: poetry install + install-sdk for all services
	@for svc in $(SERVICES); do \
	  echo "$(BOLD)→ poetry install $$svc$(RESET)"; \
	  cd services/$$svc && poetry install --no-interaction && cd - > /dev/null; \
	done
	$(MAKE) install-sdk

# ---- Code Quality ----
.PHONY: lint
lint: ## ruff check + mypy across all services
	@for svc in $(SERVICES); do \
	  echo "$(BOLD)→ $$svc$(RESET)"; \
	  cd services/$$svc && poetry run ruff check . && poetry run mypy src/ && cd - > /dev/null; \
	done

.PHONY: format
format: ## ruff format across all services
	@for svc in $(SERVICES); do \
	  cd services/$$svc && poetry run ruff format . && cd - > /dev/null; \
	done

.PHONY: test
test: ## pytest across all services
	@for svc in $(SERVICES); do \
	  echo "$(BOLD)→ $$svc$(RESET)"; \
	  cd services/$$svc && poetry run pytest -v --cov=src && cd - > /dev/null; \
	done

# ---- DB ----
.PHONY: migrate
migrate: ## Run Alembic migrations for session-orchestrator
	cd services/session-orchestrator && poetry run alembic upgrade head

# ---- Frontend ----
.PHONY: web-dev
web-dev: ## Start React dev server locally
	cd apps/web && npm run dev

.PHONY: web-install
web-install: ## Install frontend dependencies
	cd apps/web && npm install

# ---- Conformance ----
.PHONY: conformance
conformance: ## Run conformance tests against running stack
	cd tests/conformance && poetry install --no-interaction && poetry run pytest -v

.PHONY: conformance-docker
conformance-docker: ## Build and run conformance tests in Docker
	docker build -t agent-conformance -f tests/conformance/Dockerfile tests/conformance
	docker run --rm --network saas-foundation-dev_foundation \
	  -e BFF_URL=http://bff:8005 \
	  -e ORCHESTRATOR_URL=http://session-orchestrator:8006 \
	  -e SCHEDULER_URL=http://agent-scheduler:8007 \
	  agent-conformance

.PHONY: conformance-static
conformance-static: ## Run static analysis only (SDK usage checks)
	cd tests/conformance && poetry install --no-interaction && poetry run pytest -v -k "TestSDKUsage"

# ---- Clean ----
.PHONY: clean
clean: ## Remove build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	find . -type d -name .mypy_cache -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
	find . -type d -name dist -prune -exec rm -rf {} +
	find . -type d -name node_modules -prune -exec rm -rf {} +
