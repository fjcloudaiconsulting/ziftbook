"""ZIF-297: admin creation never grants business access or chooses the owner's password."""

import uuid
from typing import Any

import pytest
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import admin as platform_admin
from app import auth, limits
from app.admin_auth import ADMIN_COOKIE, admin_recently_verified
from app.db import SessionLocal, tenant_context
from tests.conftest import (
    PASSWORD,
    People,
    email_of,
    fresh_email,
    mailed,
    new_client,
    signed_in,
    token_in,
)
from tests.test_admin_billing import billing_settings as billing_settings
from tests.test_admin_businesses import admin as admin
from tests.test_admin_businesses import an_admin as an_admin
from tests.test_admin_businesses import app as app
from tests.test_admin_sessions import platform_events
from tests.test_migrations import lock_timeout_config, migration
from tests.test_password_reset import NEW_PASSWORD, complete
from tests.test_sign_up import businesses as businesses

URL = "/api/admin/businesses"


def body(email: str | None = None) -> dict[str, str]:
    return {
        "email": email or fresh_email(),
        "name": "Ana Silva",
        "business_name": "Test studio",
        "country": "NL",
        "locale": "pt",
    }


def test_owner_sets_their_password_admin_stays_separate(
    admin: TestClient,
    app: FastAPI,
    an_admin: uuid.UUID,
    migrate_engine: Engine,
    businesses: list[dict[str, Any]],
) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE platform_billing SET trial_plan_id = default_plan_id, trial_days = 14")
        )
    details = body()
    response = admin.post(URL, json=details)
    assert response.status_code == 201, response.text
    tenant = response.json()["id"]
    with migrate_engine.connect() as conn:
        owner = conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": details["email"]})
        assert owner != an_admin
        assert (
            conn.scalar(
                text("SELECT count(*) FROM password_credentials WHERE user_id = :u"), {"u": owner}
            )
            == 0
        )
    businesses.append({"tenant_id": tenant, "user_id": str(owner)})
    with tenant_context(uuid.UUID(tenant)) as db:
        members = db.execute(text("SELECT user_id, role, display_name FROM memberships")).all()
        assert members == [(owner, "owner", details["name"])]
        assert db.scalar(text("SELECT currency FROM tenants WHERE id = :t"), {"t": tenant}) == "EUR"
        assert db.scalar(text("SELECT trial_ends_at IS NOT NULL FROM tenant_billing")) is True
        settings = {
            key: value for key, value in db.execute(text("SELECT key, value FROM settings"))
        }
        assert settings["timezone"] == "Europe/Amsterdam"
        assert settings["language"] == "nl"
    assert platform_events(migrate_engine, "business_created", tenant_id=tenant) == [
        {
            "actor_user_id": an_admin,
            "kind": "write",
            "tenant_id": uuid.UUID(tenant),
            "target": "business",
        }
    ]
    outsider = new_client(app)
    assert (
        outsider.post(
            "/api/session", json={"email": details["email"], "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        outsider.post(
            "/api/session", json={"email": email_of(an_admin), "password": PASSWORD}
        ).status_code
        == 403
    )
    assert admin.get("/api/session").status_code == 401
    message = mailed(details["email"])
    assert complete(outsider, token_in(message)).status_code == 204
    signed = outsider.post(
        "/api/session", json={"email": details["email"], "password": NEW_PASSWORD}
    )
    assert signed.status_code == 200, signed.text
    assert (signed.json()["tenant_id"], signed.json()["role"]) == (tenant, "owner")
    assert admin.get("/api/admin/session").status_code == 200


@pytest.mark.parametrize("which", ["admin", "existing"])
def test_registered_addresses_cannot_become_new_owners(
    admin: TestClient,
    an_admin: uuid.UUID,
    people: People,
    migrate_engine: Engine,
    which: str,
) -> None:
    email = email_of(an_admin if which == "admin" else people.only_a)
    before = platform_events(migrate_engine, "business_created", actor_user_id=an_admin)
    response = admin.post(URL, json=body(email))
    assert response.status_code == 409
    assert response.json()["code"] == "account_exists"
    assert platform_events(migrate_engine, "business_created", actor_user_id=an_admin) == before


def test_sql_rechecks_factor_even_if_route_guard_was_bypassed(
    admin: TestClient,
    app: FastAPI,
    migrate_engine: Engine,
) -> None:
    digest = auth.hash_token(admin.cookies[ADMIN_COOKIE])
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE admin_sessions SET second_factor_at = now() - interval '16 minutes' "
                "WHERE id_hash = :h"
            ),
            {"h": digest},
        )
    details = body()
    assert admin.post(URL, json=details).status_code == 403
    app.dependency_overrides[admin_recently_verified] = lambda: None
    try:
        response = admin.post(URL, json=details)
        assert response.status_code == 403
        assert response.json()["code"] == "second_factor_stale"
    finally:
        app.dependency_overrides.clear()
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": details["email"]})
            is None
        )


def test_creation_is_rate_limited_before_writing(
    admin: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(limits, "hit", lambda *args: True)
    assert admin.post(URL, json=body()).status_code == 429


def test_audit_failure_rolls_back_owner_business_and_email(
    admin: TestClient,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    details = body()
    monkeypatch.setattr(platform_admin, "record", fail)
    assert admin.post(URL, json=details).status_code == 500
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT id FROM users WHERE email = :e"), {"e": details["email"]})
            is None
        )
        assert (
            conn.scalar(
                text("SELECT count(*) FROM email_tokens WHERE email = :e"), {"e": details["email"]}
            )
            == 0
        )


def test_0108_roundtrip_preserves_signup_and_restricts_creation(
    bound: None,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = lock_timeout_config(monkeypatch)
    m = migration("0108_admin_create_business.py")
    state = text("""SELECT pg_get_functiondef(p.oid), p.proowner, p.proacl::text, p.proconfig
        FROM pg_proc p WHERE p.oid = to_regprocedure(:signature)""")
    with migrate_engine.connect() as conn:
        after = conn.execute(state, {"signature": m.SIGN_UP}).one()
        for signature in (m.CREATE, m.ADMIN):
            assert not conn.scalar(
                text("SELECT has_function_privilege('public', :s, 'EXECUTE')"), {"s": signature}
            )
        assert not conn.scalar(
            text("SELECT has_function_privilege('ziftbook_app', :s, 'EXECUTE')"), {"s": m.CREATE}
        )
    try:
        command.downgrade(cfg, "0107")
        with migrate_engine.connect() as conn:
            before = conn.execute(state, {"signature": m.SIGN_UP}).one()
            assert before[0] == after[0].replace(m.NEW, m.OLD)
            assert before[1:] == after[1:]
            for signature in (m.CREATE, m.ADMIN):
                assert conn.scalar(text("SELECT to_regprocedure(:s)"), {"s": signature}) is None
    finally:
        command.upgrade(cfg, "head")
    with migrate_engine.connect() as conn:
        assert conn.execute(state, {"signature": m.SIGN_UP}).one() == after
    with SessionLocal.begin() as db:
        result = db.execute(
            text("SELECT * FROM admin_create_business(:h, :e, 'en', 'Test', 'NL', 'EUR', 'Ana')"),
            {"h": b"invalid", "e": fresh_email()},
        ).one()
        assert result.outcome == "stale"


def test_only_admin_sessions_can_create(app: FastAPI, people: People) -> None:
    assert new_client(app).post(URL, json=body()).status_code == 401
    merchant = signed_in(app, people.a, people.both)
    assert merchant.post(URL, json=body()).status_code == 401


@pytest.mark.parametrize("extra", [{"password": PASSWORD}, {"role": "owner"}])
def test_admin_cannot_supply_credentials_or_roles(admin: TestClient, extra: dict[str, str]) -> None:
    assert admin.post(URL, json=body() | extra).status_code == 422
