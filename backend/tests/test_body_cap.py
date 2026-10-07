"""ZIF-83: a request body over MAX_BODY is a 413, declared (Content-Length) or counted (chunked)."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import MAX_BODY, create_app
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

    assert len(body.decode().encode()) == MAX_BODY
    assert response.status_code == 422


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def test_a_declared_oversize_no_body_write_does_not_run_the_handler(
    app: FastAPI, people: People
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = owner.request("DELETE", "/api/sessions", headers={**JSON, "content-length": "70000"})

    assert (response.status_code, response.json()) == (413, TOO_LARGE)
    assert owner.get("/api/session").status_code == 200


def test_a_declared_oversize_get_is_a_json_413(client: TestClient) -> None:
    response = client.get("/api/healthz", headers={"content-length": str(MAX_BODY + 1)})

    assert (response.status_code, response.json()) == (413, TOO_LARGE)


def test_the_access_line_records_the_413(client: TestClient, log_lines: Lines) -> None:
    client.post("/api/sign-up", content=padded(MAX_BODY + 1), headers=JSON)

    line = next(rec for rec in reversed(log_lines()) if rec["msg"] == "access")
    assert line["status"] == 413
