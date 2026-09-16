"""Business settings: typed, with defaults, readable by everyone in the business and changed only by
an owner."""

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, signed_in

DEFAULTS = {"timezone": "Europe/Amsterdam", "auto_confirm": False}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def put(client: TestClient, body: Any) -> Any:
    return client.put("/api/settings", json=body)


def stored(tenant_id: uuid.UUID) -> dict[str, Any]:
    with tenant_context(tenant_id) as session:
        return dict(session.execute(text("SELECT key, value FROM settings")).tuples().all())


def store(tenant_id: uuid.UUID, key: str, value: Any) -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("""
            INSERT INTO settings (tenant_id, key, value)
            VALUES (current_setting('app.tenant_id')::uuid, :key, CAST(:value AS jsonb))
            """),
            {"key": key, "value": json.dumps(value)},
        )


def test_everyone_in_the_business_reads_the_defaults(people: People, app: FastAPI) -> None:
    response = signed_in(app, people.a, people.only_a).get("/api/settings")

    assert (response.status_code, response.json()) == (200, DEFAULTS)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body", [{"auto_confirm": True}, {"auto_confirm": "yes"}, []], ids=["valid", "invalid", "list"]
)
def test_only_an_owner_changes_settings(people: People, app: FastAPI, body: Any) -> None:
    response = put(signed_in(app, people.a, people.only_a), body)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert stored(people.a) == {}


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Olympus"},
        {"timezone": "europe/amsterdam"},
        {"timezone": "../../etc/passwd"},
        {"timezone": ""},
        {"timezone": 5},
        {"timezone": None},
        {"auto_confirm": "true"},
        {"auto_confirm": 1},
        {"auto_confirm": None},
        {"nope": 1},
        [],
    ],
    ids=[
        "unknown zone",
        "lowercase zone",
        "path",
        "empty zone",
        "number zone",
        "null zone",
        "string true",
        "number one",
        "null auto_confirm",
        "unknown key",
        "list body",
    ],
)
def test_a_setting_of_the_wrong_kind_is_refused(people: People, app: FastAPI, body: Any) -> None:
    response = put(signed_in(app, people.a, people.both), body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a) == {}


def test_a_change_leaves_the_other_settings_as_they_were(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)

    first = put(owner, {"auto_confirm": True})
    second = put(owner, {"timezone": "America/Sao_Paulo"})

    expected = {"timezone": "America/Sao_Paulo", "auto_confirm": True}
    assert (first.status_code, first.json()) == (200, {**DEFAULTS, "auto_confirm": True})
    assert (second.status_code, second.json()) == (200, expected)
    assert owner.get("/api/settings").json() == expected


def test_a_saved_setting_can_be_changed_and_set_back_to_its_default(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)

    for changes in (
        {"timezone": "Asia/Tokyo", "auto_confirm": True},
        {"timezone": "Africa/Lagos"},
        {"timezone": "Europe/Amsterdam", "auto_confirm": False},
    ):
        response = put(owner, changes)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    assert response.json() == DEFAULTS
    # Saving the default keeps it saved: a later change of default doesn't reach this business.
    assert stored(people.a) == DEFAULTS


def test_each_business_has_its_own_settings(people: People, app: FastAPI) -> None:
    with tenant_context(people.b) as session:
        session.execute(
            text("UPDATE memberships SET role = 'owner' WHERE user_id = :u"), {"u": people.only_b}
        )
    owner_a = signed_in(app, people.a, people.both)
    owner_b = signed_in(app, people.b, people.only_b)

    assert put(owner_a, {"timezone": "Asia/Tokyo"}).status_code == 200
    assert put(owner_b, {"timezone": "Africa/Lagos"}).status_code == 200

    assert owner_a.get("/api/settings").json()["timezone"] == "Asia/Tokyo"
    assert owner_b.get("/api/settings").json()["timezone"] == "Africa/Lagos"


def test_a_stored_setting_no_longer_in_the_registry_is_ignored(
    people: People, app: FastAPI
) -> None:
    store(people.a, "retired", 1)
    store(people.a, "auto_confirm", True)

    response = signed_in(app, people.a, people.only_a).get("/api/settings")

    assert (response.status_code, response.json()) == (200, {**DEFAULTS, "auto_confirm": True})


@pytest.mark.parametrize(
    ("key", "value"), [("auto_confirm", "yes"), ("timezone", "Mars/Olympus")], ids=["bool", "zone"]
)
def test_a_stored_setting_that_no_longer_fits_fails_loudly(
    people: People, app: FastAPI, key: str, value: str
) -> None:
    # Never the default instead: that would quietly change how the business works.
    store(people.a, key, value)

    response = signed_in(app, people.a, people.only_a).get("/api/settings")

    assert response.status_code == 500


def test_an_old_timezone_name_is_kept_as_sent(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put(owner, {"timezone": "America/Sao_Paulo"}).json()["timezone"] == "America/Sao_Paulo"
    assert stored(people.a) == {"timezone": "America/Sao_Paulo"}


def test_the_contract_requires_every_setting_back_and_none_sent() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert sorted(schemas["BusinessSettings-Output"]["required"]) == ["auto_confirm", "timezone"]
    assert "required" not in schemas["BusinessSettings-Input"]
