.PHONY: setup up down lint typecheck test test-hooks

API := uv run --directory apps/api

setup: ## One-time: enable the repo git hooks and install api dependencies
	git config core.hooksPath .githooks
	uv sync --directory apps/api

up: ## Start postgres and the api, syncing source changes into the container
	docker compose up --build --watch

down:
	docker compose down

lint:
	$(API) ruff check .
	$(API) ruff format --check .

typecheck:
	$(API) mypy app tests

test: test-hooks
	$(API) pytest

test-hooks:
	sh .githooks/commit-msg.test.sh
