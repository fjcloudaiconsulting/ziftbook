"""A business's contact email through the API (ZIF-232): a typed address waits for its inbox to
confirm it, and only a confirmed one is in effect."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, Row, text

import app as app_package
from app.main import create_app
from tests.conftest import (
    People,
    confirm_contact_email,
    events,
    migrate_tenant_context,
    put_settings,
    saved_settings,
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
