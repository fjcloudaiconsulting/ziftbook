.PHONY: setup test test-hooks

setup: ## One-time: enable the repo git hooks
	git config core.hooksPath .githooks

test: test-hooks

test-hooks:
	sh .githooks/commit-msg.test.sh
