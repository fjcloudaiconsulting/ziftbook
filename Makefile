.PHONY: setup up observe down reset seed migrate migration openapi lint typecheck test test-hooks

API := uv run --directory backend
PNPM ?= pnpm
WEB := $(PNPM) --dir frontend
# docker-compose.yaml's ZIF_SETUP_TOKEN: a development value, not a secret. make seed does first-run
# setup with it.
DEV_SETUP_TOKEN := dev-only-setup-token-not-a-secret-0000

setup: ## One-time: install the commit-msg git hook and the api and web dependencies
	git config --unset core.hooksPath || true
	hooks="$$(git rev-parse --git-common-dir)/hooks" && mkdir -p "$$hooks" && install -m 0755 .githooks/commit-msg "$$hooks/commit-msg"
	@if path="$$(git config --get core.hooksPath)"; then echo "WARNING: core.hooksPath is still set ($$path), globally or system-wide: git will not run this repo's hook until you unset it (git config --global --unset core.hooksPath)."; fi
	uv sync --directory backend
	$(WEB) install

up: ## Start the whole app (http://localhost:3000), syncing source changes into the containers
	sh scripts/check-ports.sh
	docker compose up --build --watch

observe: ## Like up, exporting OpenTelemetry to the shared local lgtm stack (Grafana: http://127.0.0.1:3300)
	@curl -sf -o /dev/null http://127.0.0.1:3300/api/health || { echo "The shared lgtm stack is not running: see CONTRIBUTING.md, Observability."; exit 1; }
	sh scripts/check-ports.sh
	ZIF_OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318 ZIF_OTEL_EXPORTER_OTLP_HEADERS= \
		docker compose up --build --watch

down:
	docker compose down

reset: ## Delete the local database (every account, business and booking) and start fresh; asks first
	@printf 'This deletes the local database. Type "reset" to continue: '; read answer; [ "$$answer" = reset ] || { echo "Nothing deleted."; exit 1; }
	docker compose down -v
	$(MAKE) up

seed: ## Fill the running local app with synthetic businesses, people and bookings (make up first)
	ZIF_SETUP_TOKEN=$(DEV_SETUP_TOKEN) $(API) python scripts/seed.py

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
	$(API) mypy app tests migrations scripts
	$(WEB) typecheck

test: test-hooks
	node --test "scripts/*.test.mjs"
	npm --prefix landing test
	docker compose run --rm db-init
	$(API) pytest -n auto
	$(WEB) build
	$(WEB) test

test-hooks:
	sh .githooks/commit-msg.test.sh
