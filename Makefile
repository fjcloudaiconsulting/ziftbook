.PHONY: setup up observe down reset migrate migration openapi lint typecheck test test-hooks

API := uv run --directory backend
PNPM ?= pnpm
WEB := $(PNPM) --dir frontend

setup: ## One-time: enable the repo git hooks and install api and web dependencies
	git config core.hooksPath .githooks
	uv sync --directory backend
	$(WEB) install

up: ## Start the whole app (http://localhost:3000), syncing source changes into the containers
	sh scripts/check-ports.sh
	docker compose up --build --watch

observe: ## Like up, plus logs and traces in the shared local lgtm stack (Grafana: http://127.0.0.1:3300)
	@curl -sf -o /dev/null http://127.0.0.1:3300/api/health || { echo "The shared lgtm stack is not running: see CONTRIBUTING.md, Local logs and traces."; exit 1; }
	sh scripts/check-ports.sh
	ZIF_OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318 ZIF_OTEL_EXPORTER_OTLP_HEADERS= \
		docker compose --profile observability up --build --watch

down:
	docker compose --profile observability down

reset: ## Delete the local database (every account, business and booking) and start fresh; asks first
	@printf 'This deletes the local database. Type "reset" to continue: '; read answer; [ "$$answer" = reset ] || { echo "Nothing deleted."; exit 1; }
	docker compose --profile observability down -v
	$(MAKE) up

migrate: ## Apply database migrations (as ziftbook_migrate)
	docker compose run --rm --build migrate

migration: ## Create a migration file: make migration name="add bookings"
	$(API) alembic revision -m "$(name)"

openapi: ## Regenerate the committed API contract (backend/openapi.json)
	$(API) python -m app.main > backend/openapi.json

lint:
	node scripts/check-catalogs.mjs
	node scripts/check-env-names.mjs
	node scripts/check-env-doc.mjs
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
	$(API) pytest -n auto
	$(WEB) build
	$(WEB) test

test-hooks:
	sh .githooks/commit-msg.test.sh
