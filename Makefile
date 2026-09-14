.PHONY: setup up down migrate migration openapi lint typecheck test test-hooks

API := uv run --directory backend
PNPM ?= pnpm
WEB := $(PNPM) --dir frontend

setup: ## One-time: enable the repo git hooks and install api and web dependencies
	git config core.hooksPath .githooks
	uv sync --directory backend
	$(WEB) install

up: ## Start the whole app (http://localhost:3000), syncing source changes into the containers
	docker compose up --build --watch

down:
	docker compose down

migrate: ## Apply database migrations (as ziftbook_migrate)
	docker compose run --rm --build migrate

migration: ## Create a migration file: make migration name="add bookings"
	$(API) alembic revision -m "$(name)"

openapi: ## Regenerate the committed API contract (backend/openapi.json)
	$(API) python -m app.main > backend/openapi.json

lint:
	node scripts/check-catalogs.mjs
	node scripts/check-env-names.mjs
	$(API) ruff check .
	$(API) ruff format --check .
	$(WEB) lint

typecheck:
	$(API) mypy app tests migrations
	$(WEB) typecheck

test: test-hooks
	node --test "scripts/*.test.mjs"
	npm --prefix landing test
	docker compose run --rm db-init
	docker compose up -d mailpit
	$(API) pytest
	$(WEB) build
	$(WEB) test

test-hooks:
	sh .githooks/commit-msg.test.sh
