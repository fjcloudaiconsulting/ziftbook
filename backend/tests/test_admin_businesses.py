"""ZIF-73: platform admins find and inspect businesses (app/admin_businesses.py), read-only."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import admin_businesses, auth
from app.admin_businesses import like
from app.business_settings import BusinessSettings
from app.db import join_tenant, tenant_context
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_membership,
    add_password,
    add_user,
    email_of,
    end_membership,
    member_id,
    new_client,
    suspend,
)
from tests.conftest import signed_in as merchant
from tests.test_admin_second_factor import sign_in, verified_client
from tests.test_admin_sessions import a_request, platform_events
from tests.test_bookings_db import insert_booking, seed_service
from tests.test_clients_db import seed_client

LIST = "/api/admin/businesses"


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    add_password(migrate_engine, people.both, PASSWORD)  # an owner in a, a worker in b
    removed = add_user(migrate_engine)  # a third person in a, removed: never counted or listed
    add_membership(people.a, removed)
    end_membership(people.a, removed)
    return create_app()


@pytest.fixture
def an_admin(migrate_engine: Engine, bound: None) -> uuid.UUID:
    user_id = add_user(migrate_engine)
    add_password(migrate_engine, user_id, PASSWORD)
    with migrate_engine.begin() as conn:
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": user_id})
    return user_id


@pytest.fixture
def admin(app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID) -> TestClient:
    """An admin session that has passed the second factor."""
    return verified_client(app, migrate_engine, an_admin).client


@pytest.fixture
def tag(people: People, migrate_engine: Engine) -> str:
    """A name only a and b carry: "<tag> one" (a, older) and "<tag> TWO" (b)."""
    tag = f"zif73-{uuid.uuid4().hex[:8]}"
    with migrate_engine.begin() as conn:
        for tenant_id, suffix in ((people.a, "one"), (people.b, "TWO")):
            conn.execute(
                text("UPDATE tenants SET name = :n WHERE id = :t"),
                {"n": f"{tag} {suffix}", "t": tenant_id},
            )
    return tag


def slug_of(migrate_engine: Engine, tenant_id: uuid.UUID) -> str:
    with migrate_engine.connect() as conn:
        slug: str = conn.scalar(text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id})
    return slug


def connect_stripe(tenant_id: uuid.UUID, enabled: bool) -> None:
    with tenant_context(tenant_id) as db:
        db.execute(
            text("INSERT INTO payment_accounts (tenant_id, stripe_account_id) VALUES (:t, :a)"),
            {"t": tenant_id, "a": "acct_" + uuid.uuid4().hex[:16]},
        )
        db.execute(text("UPDATE payment_accounts SET charges_enabled = :e"), {"e": enabled})


def ids(page: dict[str, Any]) -> list[str]:
    return [row["id"] for row in page["businesses"]]


# ZIF-73 fence. Kills: a search that misses a case-insensitive name or a slug, an order other than
# newest first, offsets that skip or repeat a row, counts that include removed members or another
# business's, and a Stripe state read from the wrong row.
def test_admins_find_businesses_by_name_or_slug(
    admin: TestClient,
    people: People,
    migrate_engine: Engine,
    tag: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect_stripe(people.a, enabled=False)
    connect_stripe(people.b, enabled=True)

    found = admin.get(LIST, params={"q": tag.upper()})

    assert found.status_code == 200, found.content
    assert found.json()["total"] == 2
    assert found.json()["page_size"] == admin_businesses.PAGE_SIZE
    assert ids(found.json()) == [str(people.b), str(people.a)]  # newest first
    row_a = found.json()["businesses"][1]
    assert row_a == {
        "id": str(people.a),
        "name": f"{tag} one",
        "slug": slug_of(migrate_engine, people.a),
        "country": "NL",
        "created_at": row_a["created_at"],
        "members": 2,  # only_a and both
        "stripe": "onboarding",
        "suspended_at": None,
    }
    assert row_a["created_at"] is not None
    assert found.json()["businesses"][0]["stripe"] == "ready"

    by_slug = admin.get(LIST, params={"q": slug_of(migrate_engine, people.b)}).json()
    assert ids(by_slug) == [str(people.b)]

    monkeypatch.setattr(admin_businesses, "PAGE_SIZE", 1)
    pages = [admin.get(LIST, params={"q": tag, "page": n}).json() for n in (1, 2, 3)]
    assert [ids(p) for p in pages] == [[str(people.b)], [str(people.a)], []]
    assert {p["total"] for p in pages} == {2}


def test_a_business_without_stripe_and_a_blank_search(
    admin: TestClient, people: People, tag: str
) -> None:
    rows = admin.get(LIST, params={"q": tag}).json()["businesses"]
    assert [(row["id"], row["stripe"]) for row in rows] == [
        (str(people.b), "none"),
        (str(people.a), "none"),
    ]
    every = admin.get(LIST).json()
    assert every["total"] >= 2
    assert len(every["businesses"]) <= admin_businesses.PAGE_SIZE
    assert admin.get(LIST, params={"q": "  "}).json()["total"] == every["total"]
    assert admin.get(LIST, params={"q": "x" * 101}).status_code == 422
    assert admin.get(LIST, params={"page": 0}).status_code == 422


# ZIF-73 fence. Kills: the count and the page read in two snapshots (READ COMMITTED), so a business
# created between them makes total disagree with the rows.
def test_the_count_and_the_page_share_one_snapshot(
    admin: TestClient, tag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def spy(session: Session, tenant_id: uuid.UUID) -> None:
        seen.append(session.scalar(text("SHOW transaction_isolation")))
        join_tenant(session, tenant_id)

    monkeypatch.setattr(admin_businesses, "join_tenant", spy)

    assert admin.get(LIST, params={"q": tag}).status_code == 200
    assert seen == ["repeatable read"] * 2


# ZIF-73 fence. Kills: a search term's % or _ matching any character (an admin searching "50%"
# would see every business).
def test_wildcards_in_a_search_match_literally() -> None:
    assert like("50%_off\\") == "%50\\%\\_off\\\\%"
    assert like("  ") is None


# ZIF-73 fence. Kills: a detail that skips the business_read record, records it for a business that
# does not exist, lists removed members, or reads another business's events.
def test_admins_inspect_a_business(
    admin: TestClient, people: People, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    with tenant_context(people.a) as session:
        auth.record(
            session, a_request(), "setting_changed", actor_user_id=people.both, target="setting:x"
        )
    with tenant_context(people.b) as session:
        auth.record(session, a_request(), "setting_changed", actor_user_id=people.only_b)

    answer = admin.get(f"{LIST}/{people.a}")

    assert answer.status_code == 200, answer.content
    body = answer.json()
    assert body["name"] == "a" and body["currency"] == "EUR" and body["stripe"] == "none"
    assert body["members"] == [
        {"user_id": str(people.only_a), "name": "Test Person", "role": "worker"},
        {"user_id": str(people.both), "name": "Test Person", "role": "owner"},
    ]
    assert body["settings"]["timezone"] == "Europe/Amsterdam"
    assert [(e["action"], e["actor_user_id"], e["target_kind"]) for e in body["events"]] == [
        ("setting_changed", str(people.both), "setting")
    ]
    assert platform_events(migrate_engine, "business_read", actor_user_id=an_admin) == [
        {
            "actor_user_id": an_admin,
            "kind": "read",
            "tenant_id": people.a,
            "target": "business_detail",
        }
    ]

    unknown = admin.get(f"{LIST}/{uuid.uuid4()}")
    assert (unknown.status_code, unknown.json()) == (404, {"code": "not_found"})
    assert len(platform_events(migrate_engine, "business_read", actor_user_id=an_admin)) == 1


def keys(value: object) -> set[str]:
    """Every key anywhere in a JSON answer."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


# ZIF-73 fence. Kills: a client's id, name, email, phone or note in either answer, including the id
# an audit target carries (client:<id>, booking:<id>, as clients.py and bookings.py record them) and
# a client account acting in the log; a member's email; the audit log's addresses and browsers; and
# any field added to the answers without review (the key lists are exact: widen them only after
# checking the new field holds no personal data).
def test_no_personal_data_in_the_answers(
    admin: TestClient, people: People, tag: str, migrate_engine: Engine
) -> None:
    secret = uuid.uuid4().hex[:10]
    client_account = add_user(migrate_engine)  # a client who books with an account: no member
    booking_id = uuid.uuid4()
    client_id = seed_client(
        people.a,
        name=f"Client {secret}",
        email=f"c{secret}@example.com",
        phone="+31612345678",
        client_note=f"note {secret}",
        internal_note=f"internal {secret}",
        user_id=client_account,
    )
    with tenant_context(people.a) as session:
        auth.record(
            session,
            a_request(),
            "client_created",
            actor_user_id=people.both,
            target=f"client:{client_id}",
        )
        auth.record(
            session,
            a_request(),
            "booking_created",
            actor_user_id=client_account,
            target=f"booking:{booking_id}",
        )

    # Suspended, with a booking of that client: its upcoming list is the part that must stay blind.
    service, worker, now = (
        seed_service(people.a),
        member_id(people.a, people.both),
        datetime.now(UTC),
    )
    with tenant_context(people.a) as session:
        real_booking = [
            insert_booking(
                session,
                client_id=client_id,
                worker_id=worker,
                service_id=service,
                starts_at=now + timedelta(hours=hours),
                ends_at=now + timedelta(hours=hours, minutes=30),
            )
            for hours in (1, 2)
        ]
    suspend(people.a)

    listed = admin.get(LIST, params={"q": tag})
    detail = admin.get(f"{LIST}/{people.a}")

    assert listed.status_code == detail.status_code == 200
    assert len(detail.json()["upcoming"]) == 2
    assert all(str(b) not in detail.text for b in real_booking)
    for answer in (listed.text, detail.text):
        for private in (secret, "612345678", email_of(people.both), email_of(people.only_a)):
            assert private not in answer
        assert "192.0.2.1" not in answer and "pytest" not in answer  # a_request's address, browser
    for private_id in (client_id, client_account, booking_id):
        assert str(private_id) not in detail.text
    events = [(e["action"], e["actor_user_id"], e["target_kind"]) for e in detail.json()["events"]]
    assert events[:2] == [
        ("booking_created", None, "booking"),
        ("client_created", str(people.both), "client"),
    ]
    assert keys(listed.json()) == {
        "businesses",
        "total",
        "page_size",
        *("id", "name", "slug", "country", "created_at", "members", "stripe", "suspended_at"),
    }
    body = detail.json()
    assert set(body) == {
        *("id", "name", "slug", "country", "currency", "created_at", "stripe"),
        *("members", "settings", "events"),
        *("suspended_at", "suspension_reason", "upcoming", "upcoming_total"),
    }
    assert {frozenset(row) for row in body["upcoming"]} == {
        frozenset(("starts_at", "ends_at", "service_name", "worker_display_name", "payment"))
    }
    assert keys(body["members"]) == {"user_id", "name", "role"}
    assert keys(body["events"]) == {"id", "created_at", "action", "actor_user_id", "target_kind"}
    assert set(body["settings"]) == set(BusinessSettings.model_fields)
    assert json.dumps(body["settings"]).find(secret) == -1


# ZIF-73 fence. Kills: either route on a router without the second-factor guard (admin_half_router,
# or none), or a merchant's business session let in. Nothing is recorded for a refused read.
def test_refused_without_a_passed_second_factor(
    app: FastAPI, people: People, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    half = sign_in(app, an_admin)  # the password only
    owner = merchant(app, people.a, people.both)
    for path in (LIST, f"{LIST}/{people.a}"):
        anonymous, as_owner, as_half = (c.get(path) for c in (new_client(app), owner, half))
        assert (anonymous.status_code, anonymous.json()) == (401, {"code": "unauthenticated"})
        assert (as_owner.status_code, as_owner.json()) == (401, {"code": "unauthenticated"})
        assert (as_half.status_code, as_half.json()) == (403, {"code": "second_factor_required"})
    assert platform_events(migrate_engine, "business_read", actor_user_id=an_admin) == []
