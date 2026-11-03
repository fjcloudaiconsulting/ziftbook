"""ZIF-146: a signed-in person changes their own name and phone (owner rulings 4 and 5).

The change reaches no business: a business's client record refreshes only when the person books
there again (copy at booking, ZIF-99), and no membership's display name changes."""

import uuid

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth, platform_account
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, events, signed_in
from tests.test_account_sessions import account_client
from tests.test_clients_db import row_of, seed_client


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def client(app: FastAPI, people: People, migrate_engine: Engine) -> TestClient:
    return account_client(app, migrate_engine, people.both)


def stored(migrate_engine: Engine, user_id: uuid.UUID) -> tuple[str | None, str | None]:
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("SELECT name, phone FROM users WHERE id = :u"), {"u": user_id}
        ).one()
    return row.name, row.phone


def test_a_person_changes_their_own_name_and_phone(
    client: TestClient, people: People, migrate_engine: Engine
) -> None:
    response = client.patch("/api/account", json={"name": "Nova Both", "phone": "+31 6 1234 5678"})

    assert response.status_code == 200, response.json()
    assert response.json()["name"] == "Nova Both"
    assert response.json()["phone"] == "+31612345678"
    assert response.headers["cache-control"] == "no-store, private"
    assert stored(migrate_engine, people.both) == ("Nova Both", "+31612345678")
    assert client.get("/api/account").json() == response.json()
    rows = events(migrate_engine, action="account_details_changed", actor_user_id=people.both)
    assert [(r["tenant_id"], r["details"]) for r in rows] == [
        (None, {"name": "changed", "phone": "changed"})
    ]


def test_only_the_fields_sent_change_and_null_clears_the_phone(
    client: TestClient, people: People, migrate_engine: Engine
) -> None:
    assert client.patch("/api/account", json={"phone": "+351912345678"}).status_code == 200
    assert stored(migrate_engine, people.both) == ("Test Person", "+351912345678")

    assert client.patch("/api/account", json={"name": "Nova"}).status_code == 200
    assert stored(migrate_engine, people.both) == ("Nova", "+351912345678")

    assert client.patch("/api/account", json={"phone": None}).status_code == 200
    assert stored(migrate_engine, people.both) == ("Nova", None)


# FENCE (ruling 5). Wrong impl: reading a phone with no region in some default country.
@pytest.mark.parametrize("phone", ["0612345678", "+31 6 12", "call me", "+31612345678 x12"])
def test_a_phone_must_be_international_and_valid(client: TestClient, phone: str) -> None:
    response = client.patch("/api/account", json={"phone": phone})

    assert (response.status_code, response.json()) == (422, {"code": "phone_invalid"})


@pytest.mark.parametrize(
    "body", [{"name": None}, {"name": "ㅤ"}, {"name": " "}, {"email": "x@example.com"}]
)
def test_a_name_cannot_be_cleared_or_blank_and_nothing_else_is_editable(
    client: TestClient, body: dict[str, object]
) -> None:
    response = client.patch("/api/account", json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


# FENCE (copy at booking). Wrong impl: a set_own_profile that also updates linked client rows (a
# live profile), or that fills display names as set_own_name does.
def test_a_profile_change_reaches_no_business(
    client: TestClient, people: People, migrate_engine: Engine
) -> None:
    client_id = seed_client(people.a, name="Old Name", phone="+31600000000", user_id=people.both)
    with tenant_context(people.b) as session:
        session.execute(
            text("UPDATE memberships SET display_name = NULL WHERE user_id = :u"),
            {"u": people.both},
        )

    assert (
        client.patch("/api/account", json={"name": "New", "phone": "+31611111111"}).status_code
        == 200
    )

    row = row_of(people.a, client_id)
    assert (row["name"], row["phone"]) == ("Old Name", "+31600000000")
    with tenant_context(people.b) as session:
        assert (
            session.scalar(
                text("SELECT display_name FROM memberships WHERE user_id = :u"), {"u": people.both}
            )
            is None
        )


# FENCE. Wrong impl: set_own_profile keyed by a user id, or one that accepts a business session.
def test_a_business_session_changes_no_profile(
    app: FastAPI, people: People, migrate_engine: Engine, app_engine: Engine
) -> None:
    business = signed_in(app, people.a, people.both)
    assert business.patch("/api/account", json={"name": "Hijack"}).status_code == 401
    token = business.cookies.get(auth.COOKIE)
    assert token is not None
    with app_engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT * FROM set_own_profile(:h, 'Hijack', NULL, false)"),
                {"h": auth.hash_token(token)},
            ).first()
            is None
        )
    assert stored(migrate_engine, people.both)[0] == "Test Person"


# FENCE (code review N3). Wrong impl: writing back the phone the request read before another tab
# changed it: two edits at once, one in each tab, lose one of them.
def test_a_name_edit_never_writes_back_a_phone_changed_meanwhile(
    app: FastAPI, client: TestClient, people: People, migrate_engine: Engine
) -> None:
    resolved = platform_account.account_session

    def another_tab_changes_the_phone(request: Request) -> platform_account.AccountSession:
        current = resolved(request)  # read before the other tab's change commits
        with migrate_engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET phone = '+351912345678' WHERE id = :u"), {"u": people.both}
            )
        return current

    app.dependency_overrides[platform_account.account_session] = another_tab_changes_the_phone
    try:
        response = client.patch("/api/account", json={"name": "Nova"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert stored(migrate_engine, people.both) == ("Nova", "+351912345678")
    assert response.json()["phone"] == "+351912345678"
    rows = events(migrate_engine, action="account_details_changed", actor_user_id=people.both)
    assert [r["details"] for r in rows] == [{"name": "changed"}]
