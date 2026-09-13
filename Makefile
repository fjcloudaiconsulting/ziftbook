.PHONY: setup up down web openapi lint typecheck test test-hooks

API := uv run --directory apps/api
PNPM ?= pnpm
WEB := $(PNPM) --dir apps/web

setup: ## One-time: enable the repo git hooks and install api and web dependencies
	git config core.hooksPath .githooks
	uv sync --directory apps/api
	$(WEB) install

up: ## Start postgres and the api, syncing source changes into the container
	docker compose up --build --watch

down:
	docker compose down

web: ## Run the web app on the host (http://localhost:3000); /api is forwarded to the api from `make up`
	$(WEB) dev

openapi: ## Regenerate the committed API contract (apps/api/openapi.json)
	$(API) python -m app.main > apps/api/openapi.json

lint:
	node scripts/check-catalogs.mjs
	node scripts/check-env-names.mjs
	$(API) ruff check .
	$(API) ruff format --check .
	$(WEB) lint

typecheck:
	$(API) mypy app tests
	$(WEB) typecheck

test: test-hooks
	node --test "scripts/*.test.mjs"
	npm --prefix landing test
	$(API) pytest
	$(WEB) build
	$(WEB) test

test-hooks:
	sh .githooks/commit-msg.test.sh
