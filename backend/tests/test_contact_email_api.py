"""A business's contact email through the API (ZIF-232): a typed address waits for its inbox to
confirm it, and only a confirmed one is in effect."""

import hashlib
import secrets
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, Row, text

import app as app_package
from app import contact_email
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    confirm_contact_email,
    events,
    fresh_address,
    migrate_tenant_context,
    new_client,
    put_settings,
    saved_settings,
    set_role,
    signed_in,
)


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def waiting(migrate_engine: Engine, tenant_id: uuid.UUID) -> Row[Any] | None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        return conn.execute(
            text("SELECT id, email, token_hash, expires_at FROM contact_email_requests")
        ).first()


def contact_jobs(migrate_engine: Engine, tenant_id: uuid.UUID) -> int:
    with migrate_engine.connect() as conn:
        found: int = conn.scalar(
            text("SELECT count(*) FROM jobs WHERE tenant_id = :t AND kind LIKE 'email.contact%'"),
            {"t": tenant_id},
        )
    return found


def contact_events(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return [
        (e["action"], e["actor_user_id"], e["details"])
        for e in events(migrate_engine, tenant_id=tenant_id, target="setting:contact_email")
    ]


# F2.3 FENCE. Wrong impl: the PUT still saves the key (the address in effect unconfirmed), or it
# queues a link in the release before every worker knows the job.
def test_a_typed_address_waits_and_is_not_in_effect(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = put_settings(owner, {"contact_email": " Shop@Example.COM "})

    assert (response.status_code, response.json()["contact_email"]) == (200, None)
    assert owner.get("/api/settings").json()["contact_email"] is None
    request = waiting(migrate_engine, people.a)
    assert request is not None
    assert (request.email, request.token_hash, request.expires_at) == (
        "shop@example.com",
        None,
        None,
    )
    assert saved_settings(people.a) == {}
    assert contact_jobs(migrate_engine, people.a) == 0
    with migrate_tenant_context(migrate_engine, people.a) as conn:  # the owner who asked
        assert conn.scalar(text("SELECT requested_by FROM contact_email_requests")) == people.both


def test_the_address_in_effect_stays_until_the_new_one_is_confirmed(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)

    response = put_settings(owner, {"contact_email": "new@example.com"})

    assert response.json()["contact_email"] == "old@example.com"
    assert waiting(migrate_engine, people.a).email == "new@example.com"  # type: ignore[union-attr]


# F2.7 FENCE. Wrong impl: the typed address in the audit details (it may be a third party's, and
# nobody confirmed it), or a setting_changed for a change that is not in effect.
def test_a_typed_address_is_recorded_without_it(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put_settings(owner, {"contact_email": "shop@example.com"}).status_code == 200
    assert put_settings(owner, {"contact_email": "shop@example.com"}).status_code == 200

    assert contact_events(migrate_engine, people.a) == [
        ("contact_email_requested", people.both, None)
    ]


# F2.5 FENCE. Wrong impl: a clear that removes only the waiting address.
def test_null_clears_the_address_in_effect_and_the_waiting_one(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "new@example.com"}).status_code == 200

    response = put_settings(owner, {"contact_email": None})

    assert (response.status_code, response.json()["contact_email"]) == (200, None)
    assert (saved_settings(people.a), waiting(migrate_engine, people.a)) == ({}, None)
    assert contact_events(migrate_engine, people.a)[-1] == (
        "setting_changed",
        people.both,
        {"old": "old@example.com", "new": None},
    )
    assert put_settings(owner, {"contact_email": None}).status_code == 200
    assert len(contact_events(migrate_engine, people.a)) == 2  # nothing in effect: not recorded


def test_typing_the_address_in_effect_drops_the_waiting_one(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "new@example.com"}).status_code == 200

    response = put_settings(owner, {"contact_email": "Old@Example.com"})

    assert response.json()["contact_email"] == "old@example.com"
    assert waiting(migrate_engine, people.a) is None
    assert len(contact_events(migrate_engine, people.a)) == 1


# F2.6 FENCE. Wrong impl: an absent key treated as null.
def test_a_save_of_other_keys_leaves_both_addresses_alone(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "new@example.com"}).status_code == 200
    before = waiting(migrate_engine, people.a)

    assert put_settings(owner, {"language": "nl"}).status_code == 200

    assert waiting(migrate_engine, people.a) == before
    assert saved_settings(people.a)["contact_email"] == "old@example.com"


def test_a_worker_member_cannot_type_one(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)

    assert put_settings(worker, {"contact_email": "shop@example.com"}).status_code == 403
    assert waiting(migrate_engine, people.a) is None


NAMES = (
    "contact_email_requests",
    "change_contact_email",
    "resend_contact_email",
    "confirm_contact_email",
    "mint_contact_email_token",
    "cancel_contact_email",
)


# F2.8 FENCE. Wrong impl: another module reading the typed address, for clients or anyone.
def test_only_the_contact_email_module_and_the_mailer_name_the_requests() -> None:
    here = Path(app_package.__file__).parent
    naming = sorted(
        path.name
        for path in here.glob("*.py")
        if any(name in path.read_text() for name in NAMES)
        and path.name not in {"contact_email.py", "mail.py"}
    )
    assert naming == []


CONFIRM = "/api/contact-email/confirm"


def link(worker_engine: Engine, tenant_id: uuid.UUID, email: str) -> str:
    """The fragment of a freshly mailed link for email, as the job mints it, asked for by one of
    the business's owners."""
    with tenant_context(tenant_id) as session:
        request_id = session.execute(
            text(
                "SELECT request_id FROM change_contact_email(:e, true,"
                " (SELECT user_id FROM memberships WHERE role = 'owner' ORDER BY id LIMIT 1))"
            ),
            {"e": email},
        ).scalar_one()
    secret = secrets.token_urlsafe(32)
    with worker_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        assert conn.execute(
            text("SELECT * FROM mint_contact_email_token(:id, :h)"),
            {"id": request_id, "h": hashlib.sha256(secret.encode()).digest()},
        ).first()
    return f"{tenant_id}.{secret}"


# F5.1 FENCE. Wrong impl: an answer that names the business or the address, or a confirm that is
# not audited.
def test_a_live_link_confirms_the_address(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    token = link(worker_engine, people.a, "new@example.com")

    response = new_client(app).post(CONFIRM, json={"token": token})

    assert (response.status_code, response.content) == (204, b"")
    assert response.headers["cache-control"] == "no-store"
    assert saved_settings(people.a)["contact_email"] == "new@example.com"
    assert waiting(migrate_engine, people.a) is None
    assert contact_events(migrate_engine, people.a)[-1] == (
        "setting_changed",
        None,
        {"old": "old@example.com", "new": "new@example.com"},
    )


# F5.2, F5.3 FENCE. Wrong impl: a different answer for any of these (the endpoint would tell
# whether a business, an address or a link exists), or a link that works twice.
def test_every_dead_link_gets_the_same_answer(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    set_role(people.b, people.only_b, "owner")  # b's link needs an owner who asked for it
    used = link(worker_engine, people.b, "used@example.com")
    assert new_client(app).post(CONFIRM, json={"token": used}).status_code == 204
    resent = link(worker_engine, people.a, "resent@example.com")
    with tenant_context(people.a) as session:
        session.execute(text("SELECT resend_contact_email()"))
    changed = link(worker_engine, people.a, "changed@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "x@example.com"}).status_code == 200
    lapsed_tenant, secret = link(worker_engine, people.b, "lapsed@example.com").split(".")
    with migrate_tenant_context(migrate_engine, people.b) as conn:
        conn.execute(
            text("UPDATE contact_email_requests SET expires_at = now() - interval '1 second'")
        )
    dead = [
        "nonsense",
        f"{uuid.uuid4()}.{secrets.token_urlsafe(32)}",
        f"{people.a}.{secrets.token_urlsafe(32)}",
        f"{people.a}.short",
        f"{people.a}.{secret}",  # the lapsed link under another business's id
        f"{lapsed_tenant}.{secret}",
        used,
        resent,
        changed,
    ]

    answers = [new_client(app).post(CONFIRM, json={"token": token}) for token in dead]

    assert {(a.status_code, a.text) for a in answers} == {(400, '{"code":"invalid_token"}')}
    assert saved_settings(people.a) == {}


# F5.4 FENCE. Wrong impl: no limit, or one counted after the lookup (a guess then costs a
# database transaction before it is refused).
def test_the_eleventh_try_from_one_address_is_refused_before_any_lookup(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = new_client(app, fresh_address())
    for _ in range(10):
        assert client.post(CONFIRM, json={"token": "nonsense"}).status_code == 400

    def no_lookup(*args: object, **kwargs: object) -> None:
        raise AssertionError("looked up")

    monkeypatch.setattr(contact_email, "tenant_context", no_lookup)
    over = client.post(CONFIRM, json={"token": f"{people.a}.{secrets.token_urlsafe(32)}"})

    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})


# F5.6 FENCE. Wrong impl: a GET that confirms (a mail scanner's prefetch would), or a form post.
def test_only_a_json_post_confirms(people: People, app: FastAPI, worker_engine: Engine) -> None:
    token = link(worker_engine, people.a, "shop@example.com")
    client = new_client(app)

    assert client.get(CONFIRM, params={"token": token}).status_code == 405
    assert client.post(CONFIRM, data={"token": token}).status_code == 415
    assert saved_settings(people.a) == {}


# F5.5 FENCE. Wrong impl: the token, its hash or the address in a log line.
def test_a_confirm_logs_ids_only(
    people: People, app: FastAPI, worker_engine: Engine, log_lines: Any
) -> None:
    token = link(worker_engine, people.a, "zzyzx-confirm@example.com")
    secret = token.split(".")[1]

    assert new_client(app).post(CONFIRM, json={"token": token}).status_code == 204

    lines = log_lines()
    (confirmed,) = [line for line in lines if line["msg"] == "contact email confirmed"]
    assert confirmed["tenant_id"] == str(people.a)
    logged = str(lines)
    for value in (secret, hashlib.sha256(secret.encode()).hexdigest(), "zzyzx-confirm"):
        assert value not in logged
