import assert from "node:assert/strict";
import { test } from "node:test";

import { unprefixedNames } from "./check-env-names.mjs";

test("TypeScript: process.env reads need the ZIF_ prefix", () => {
  const source = 'const a = process.env.ZIF_API_URL; const b = process.env.API_URL; const c = process.env["SECRET"];';
  assert.deepEqual(unprefixedNames("proxy.ts", source), ["API_URL", "SECRET"]);
});

test("TypeScript: framework variables are allowed", () => {
  assert.deepEqual(unprefixedNames("proxy.ts", 'if (process.env.NODE_ENV === "development") {}'), []);
});

test("Python: os.environ, getenv and monkeypatch reads, including across lines", () => {
  const source = [
    'os.environ["ZIF_DATABASE_URL"]',
    'os.environ.get("DATABASE_URL")',
    'os.getenv("TOKEN")',
    'os.environ.setdefault(\n    "MIGRATE_DATABASE_URL",\n    "x")',
    'monkeypatch.setenv("APP_VERSION", "1")',
    'monkeypatch.delenv("ZIF_APP_VERSION", raising=False)',
  ].join("\n");
  assert.deepEqual(unprefixedNames("conftest.py", source), [
    "DATABASE_URL",
    "TOKEN",
    "MIGRATE_DATABASE_URL",
    "APP_VERSION",
  ]);
});

test("TypeScript: OTEL_ names are allowed, including a ??= default write", () => {
  const source = 'process.env.OTEL_SERVICE_NAME ??= "ziftbook-web";';
  assert.deepEqual(unprefixedNames("trace.ts", source), []);
});

test("Python: OTEL_ names are allowed, including a setdefault default write", () => {
  const source = 'os.environ.setdefault("OTEL_SERVICE_NAME", service)';
  assert.deepEqual(unprefixedNames("tracing.py", source), []);
});

test("Python: settings classes must declare the ZIF_ prefix", () => {
  const without = "class Settings(BaseSettings):\n    app_version: str = 'dev'\n";
  const withPrefix = 'class Settings(BaseSettings):\n    model_config = SettingsConfigDict(env_prefix="ZIF_")\n';
  assert.deepEqual(unprefixedNames("config.py", without), ['BaseSettings without env_prefix="ZIF_"']);
  assert.deepEqual(unprefixedNames("config.py", withPrefix), []);
});

test("SQL: psql \\getenv reads need the prefix", () => {
  const source = "\\getenv migrate_password ZIF_MIGRATE_PASSWORD\n\\getenv app_password APP_PASSWORD\n";
  assert.deepEqual(unprefixedNames("bootstrap.sql", source), ["APP_PASSWORD"]);
});

test("Dockerfile: build args need the prefix", () => {
  const source = "ARG ZIF_APP_VERSION=dev\nARG VERSION\nENV PYTHONUNBUFFERED=1\n";
  assert.deepEqual(unprefixedNames("Dockerfile", source), ["VERSION"]);
});
