"""ZIF-83: a request body over MAX_BODY is a 413, declared (Content-Length) or counted (chunked)."""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.business_settings import Locale
from app.clients import NoteChange
from app.main import MAX_BODY, create_app
from app.services import ServiceIn
from tests.conftest import People, new_client, signed_in

JSON = {"content-type": "application/json"}
TOO_LARGE = {"code": "content_too_large"}
Lines = Callable[[], list[dict[str, Any]]]


def padded(size: int) -> bytes:
    """Valid JSON of exactly `size` bytes."""
    body = b"{}" + b" " * (size - 2)
    assert len(body) == size
    return body


@pytest.fixture
def client() -> TestClient:
    return new_client(create_app())


def test_a_declared_oversize_body_is_a_413_with_the_standard_headers(client: TestClient) -> None:
    response = client.post("/api/sign-up", content=padded(MAX_BODY + 1), headers=JSON)

    assert (response.status_code, response.json()) == (413, TOO_LARGE)
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Request-ID"]


def test_a_chunked_oversize_body_is_a_413(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        body = padded(MAX_BODY + 1)
        yield from (body[i : i + 4096] for i in range(0, len(body), 4096))

    response = client.post("/api/sign-up", content=chunks(), headers=JSON)

    assert "content-length" not in response.request.headers
    assert (response.status_code, response.json()) == (413, TOO_LARGE)
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Request-ID"]


def test_a_body_of_exactly_the_cap_reaches_validation(client: TestClient) -> None:
    body = padded(MAX_BODY)

    response = client.post("/api/sign-up", content=body, headers=JSON)

    assert response.status_code == 422


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.mark.parametrize(
    "declared", ["70000", "1000000000000000000", "10000000000000000000", "0" * 17 + "70000"]
)
def test_a_declared_oversize_no_body_write_does_not_run_the_handler(
    app: FastAPI, people: People, declared: str
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = owner.request(
        "DELETE", "/api/sessions", headers={**JSON, "content-length": declared}
    )

    assert (response.status_code, response.json()) == (413, TOO_LARGE)
    assert owner.get("/api/session").status_code == 200


def test_a_declared_oversize_get_is_a_json_413(client: TestClient) -> None:
    response = client.get("/api/healthz", headers={"content-length": str(MAX_BODY + 1)})

    assert (response.status_code, response.json()) == (413, TOO_LARGE)


def test_the_access_line_records_the_413(client: TestClient, log_lines: Lines) -> None:
    client.post("/api/sign-up", content=padded(MAX_BODY + 1), headers=JSON)

    line = next(rec for rec in reversed(log_lines()) if rec["msg"] == "access")
    assert line["status"] == 413


def test_the_contract_declares_413_on_every_write_route_and_no_get_route() -> None:
    spec = create_app().openapi()
    wrong = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            declared = operation["responses"].get("413")
            write = method.upper() in ("POST", "PUT", "PATCH", "DELETE")
            expected = {"$ref": "#/components/schemas/Error"}
            if (
                write
                and (declared or {}).get("content", {}).get("application/json", {}).get("schema")
                != expected
            ):
                wrong.append(f"{method.upper()} {path} lacks 413")
            if not write and declared:
                wrong.append(f"{method.upper()} {path} declares 413")
    assert wrong == []


def limit(schema: dict[str, Any], node: Any, key: str) -> int:
    """The first `key` (maxLength, maxItems) under this schema node, through $ref, anyOf and the
    additionalProperties of a dict."""
    if isinstance(node, dict):
        if key in node:
            return int(node[key])
        if "$ref" in node:
            return limit(schema, schema["$defs"][node["$ref"].rsplit("/", 1)[-1]], key)
        for child in node.values():
            try:
                return limit(schema, child, key)
            except LookupError:
                pass
    if isinstance(node, list):
        for child in node:
            try:
                return limit(schema, child, key)
            except LookupError:
                pass
    raise LookupError(key)


# The cap must stay above the largest body the models themselves allow. Read the bounds
# from the models, so raising one fails here and not in production. "𝐀" is a letter, outside the
# BMP: json.dumps escapes it to 12 bytes, the worst case per character.
def worst_note_change() -> tuple[type[BaseModel], dict[str, Any]]:
    schema = NoteChange.model_json_schema()
    note = "𝐀" * limit(schema, schema["properties"]["client_note"], "maxLength")
    return NoteChange, {"client_note": note, "internal_note": note}


def worst_service_in() -> tuple[type[BaseModel], dict[str, Any]]:
    schema = ServiceIn.model_json_schema()
    props = schema["properties"]
    name = "𝐀" * limit(schema, props["name"], "maxLength")
    description = "𝐀" * limit(schema, props["description"], "maxLength")
    workers = limit(schema, props["worker_ids"], "maxItems")
    return ServiceIn, {
        "name": dict.fromkeys(get_args(Locale), name),
        "description": dict.fromkeys(get_args(Locale), description),
        "price": {"amount_minor": 1_000_000},
        "duration_minutes": 720,
        "buffer_minutes": 240,
        "worker_ids": [str(uuid.uuid4()) for _ in range(workers)],
    }


@pytest.mark.parametrize("worst", [worst_note_change, worst_service_in])
def test_the_largest_legitimate_bodies_fit_under_the_cap(
    worst: Callable[[], tuple[type[BaseModel], dict[str, Any]]],
) -> None:
    model, body = worst()

    model.model_validate(body)  # it is a legitimate body, not just a big one
    assert len(json.dumps(body).encode()) <= MAX_BODY
