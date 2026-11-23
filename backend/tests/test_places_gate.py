"""The places gate at invite send and accept (ZIF-78): live members plus pending invites against
the plan's places, counted under a per-business lock."""

import threading
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from app.db import tenant_context
from tests.conftest import (
    PASSWORD,
    People,
    add_membership,
    add_password,
    email_of,
    fresh_email,
    give_plan,
    member_id,
    new_client,
    signed_in,
)
from tests.test_account_email import bound  # noqa: F401
from tests.test_invites_api import app, mint  # noqa: F401
from tests.test_invites_db import NewAccount, new_accounts  # noqa: F401

# Tenant a has two live members (an owner and a worker), so places 2 is full.
PLAN_LIMIT = {"code": "plan_limit", "feature": "places", "limit": 2}


def test_invite_over_places_is_402(people: People, app: FastAPI) -> None:  # noqa: F811
    give_plan(people.a, {"places": 1, "email_reminders": True})  # a holds two members already
    response = signed_in(app, people.a, people.both).post(
        "/api/invites", json={"email": fresh_email()}
    )
    assert response.status_code == 402
    assert response.json() == {"code": "plan_limit", "feature": "places", "limit": 1}


def test_pending_invites_count(people: People, app: FastAPI) -> None:  # noqa: F811
    give_plan(people.a, {"places": 3, "email_reminders": True})
    owner = signed_in(app, people.a, people.both)
    assert owner.post("/api/invites", json={"email": fresh_email()}).status_code == 201
    response = owner.post("/api/invites", json={"email": fresh_email()})
    assert response.status_code == 402
    assert response.json()["limit"] == 3


def test_resend_does_not_count_twice(people: People, app: FastAPI) -> None:  # noqa: F811
    give_plan(people.a, {"places": 3, "email_reminders": True})
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()
    assert owner.post("/api/invites", json={"email": email}).status_code == 201
    assert owner.post("/api/invites", json={"email": email}).status_code == 201


def test_accept_rechecks_after_a_downgrade(
    people: People,  # noqa: F811
    app: FastAPI,  # noqa: F811
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    give_plan(people.a, {"places": 3, "email_reminders": True})
    email = fresh_email()
    created = signed_in(app, people.a, people.both).post("/api/invites", json={"email": email})
    token = mint(monkeypatch, people.a, uuid.UUID(created.json()["id"]))
    give_plan(people.a, {"places": 2, "email_reminders": True})

    response = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD, "name": "New Person"}
    )

    assert response.status_code == 402
    assert response.json() == PLAN_LIMIT
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 0
    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT count(*) FROM memberships")) == 2


def test_accept_within_places_still_works(
    people: People,  # noqa: F811
    app: FastAPI,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    new_accounts: list[NewAccount],  # noqa: F811
) -> None:
    give_plan(people.a, {"places": 3, "email_reminders": True})
    created = signed_in(app, people.a, people.both).post(
        "/api/invites", json={"email": fresh_email()}
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created.json()["id"]))
    response = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD, "name": "New Person"}
    )
    assert response.status_code == 201
    new_accounts.append(
        NewAccount(tenant_id=people.a, user_id=uuid.UUID(response.json()["user_id"]))
    )


def test_two_invites_for_the_last_place_admit_one(
    people: People,
    app: FastAPI,  # noqa: F811
) -> None:
    give_plan(people.a, {"places": 3, "email_reminders": True})
    statuses: list[int] = []

    def send() -> None:
        client = signed_in(app, people.a, people.both)
        statuses.append(client.post("/api/invites", json={"email": fresh_email()}).status_code)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [201, 402]


def test_a_returning_removed_member_counts(
    people: People,
    app: FastAPI,  # noqa: F811
) -> None:
    give_plan(people.a, {"places": 2, "email_reminders": True})
    owner = signed_in(app, people.a, people.both)
    old_id = member_id(people.a, people.only_a)
    assert owner.request("DELETE", f"/api/members/{old_id}", json={}).status_code == 204
    # Removed: one place free. The re-invite takes it; nothing else fits.
    assert owner.post("/api/invites", json={"email": email_of(people.only_a)}).status_code == 201
    assert owner.post("/api/invites", json={"email": fresh_email()}).status_code == 402


# ZIF-78 R30 fence. Kills: a revived membership (a removed member re-invited and accepting) that
# the places count misses, so the business grows past its plan.
def test_a_revived_member_takes_the_place_they_accept(
    people: People,
    app: FastAPI,  # noqa: F811
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    give_plan(people.a, {"places": 2, "email_reminders": True})
    add_password(migrate_engine, people.only_a, PASSWORD)
    owner = signed_in(app, people.a, people.both)
    old_id = member_id(people.a, people.only_a)
    assert owner.request("DELETE", f"/api/members/{old_id}", json={}).status_code == 204
    invite = owner.post("/api/invites", json={"email": email_of(people.only_a)})
    assert invite.status_code == 201
    token = mint(monkeypatch, people.a, uuid.UUID(invite.json()["id"]))

    accepted = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD, "name": "Back Again"}
    )
    assert accepted.status_code == 201, accepted.text
    assert member_id(people.a, people.only_a) == old_id  # the same row, revived

    refused = owner.post("/api/invites", json={"email": fresh_email()})
    assert (refused.status_code, refused.json()) == (
        402,
        {"code": "plan_limit", "feature": "places", "limit": 2},
    )


def test_accept_by_a_member_in_a_full_business_is_409(
    people: People,  # noqa: F811
    app: FastAPI,  # noqa: F811
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    give_plan(people.a, {"places": 3, "email_reminders": True})
    add_password(migrate_engine, people.only_b, PASSWORD)
    invite = signed_in(app, people.a, people.both).post(
        "/api/invites", json={"email": email_of(people.only_b)}
    )
    token = mint(monkeypatch, people.a, uuid.UUID(invite.json()["id"]))
    add_membership(people.a, people.only_b)  # joined another way meanwhile
    give_plan(people.a, {"places": 1, "email_reminders": True})

    response = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD, "name": "x"}
    )

    assert (response.status_code, response.json()["code"]) == (409, "already_member")


def test_accept_of_a_revoked_link_in_a_full_business_is_400(
    people: People,  # noqa: F811
    app: FastAPI,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    give_plan(people.a, {"places": 3, "email_reminders": True})
    owner = signed_in(app, people.a, people.both)
    invite = owner.post("/api/invites", json={"email": fresh_email()}).json()
    token = mint(monkeypatch, people.a, uuid.UUID(invite["id"]))
    assert owner.request("DELETE", f"/api/invites/{invite['id']}", json={}).status_code == 204
    give_plan(people.a, {"places": 1, "email_reminders": True})

    response = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD, "name": "x"}
    )

    assert (response.status_code, response.json()["code"]) == (400, "invalid_token")
