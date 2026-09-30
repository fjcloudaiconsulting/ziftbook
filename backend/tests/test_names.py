"""ZIF-131: everyone gives a name. users.name is the person's platform name (NULL = an account from
before registration asked for one) and gates every session route but three."""

import os
import re
import uuid
from typing import Any
from urllib.parse import urlencode

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from psycopg.errors import CheckViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

import app.auth as app_auth  # conftest shadows the name `signed_in`
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    API_DIR,
    PASSWORD,
    People,
    add_password,
    email_of,
    events,
    fresh_email,
    issue_link,
    live,
    member_id,
    new_client,
    signed_in,
)
from tests.test_invites_db import NewAccount, minted, new_accounts  # noqa: F401
from tests.test_sign_up import MISSING, businesses, complete, created  # noqa: F401

EXEMPT = {("GET", "/api/session"), ("PUT", "/api/session/name"), ("DELETE", "/api/sessions")}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def user_name(engine: Engine, user_id: uuid.UUID) -> str | None:
    with engine.connect() as conn:
        name: str | None = conn.scalar(text("SELECT name FROM users WHERE id = :u"), {"u": user_id})
        return name


def set_user_name(engine: Engine, user_id: uuid.UUID, name: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET name = :n WHERE id = :u"), {"n": name, "u": user_id})


def display_name(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    with tenant_context(tenant_id) as session:
        shown: str | None = session.scalar(
            text("SELECT display_name FROM memberships WHERE user_id = :u"), {"u": user_id}
        )
    return shown


def invite_token(tenant_id: uuid.UUID, email: str) -> str:
    _, secret = minted(tenant_id, email)
    return f"{tenant_id}.{secret}"


# F1: a missing or blank name is a 422 before anything is consumed.
@pytest.mark.parametrize("name", [MISSING, "", "   ", "x" * 61, "a​b", None])
def test_sign_up_without_a_usable_name_is_refused_and_the_link_lives(
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    businesses: list[dict[str, Any]],  # noqa: F811
    name: object,
) -> None:
    email = fresh_email()
    token = issue_link(app_engine, "sign_up", email)
    assert token is not None
    client = new_client(app)

    refused = complete(client, token, country="NL", name=name)

    assert refused.status_code == 422
    assert live(app_engine, token, "sign_up")
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 0
    created(complete(client, token, country="NL"), businesses)


# F2: the name lands on the user and, as a stored copy, on the owner's membership.
def test_sign_up_stores_the_name_on_the_user_and_the_membership(
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    businesses: list[dict[str, Any]],  # noqa: F811
) -> None:
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None

    session = created(
        complete(new_client(app), token, country="NL", name="  Ana Silva "), businesses
    )

    assert session["name"] == "Ana Silva"
    user = uuid.UUID(session["user_id"])
    assert user_name(migrate_engine, user) == "Ana Silva"
    with migrate_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": session["tenant_id"]}
        )
        raw = conn.scalar(
            text("SELECT display_name FROM memberships WHERE user_id = :u"), {"u": user}
        )
    assert raw == "Ana Silva"


# F3: the business's display name is a copy, not a live view: editing it leaves the person alone.
def test_editing_a_display_name_leaves_the_platform_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    changed = owner.put(
        f"/api/members/{member_id(people.a, people.both)}/display-name",
        json={"display_name": "Nickname"},
    )

    assert changed.status_code == 200
    assert display_name(people.a, people.both) == "Nickname"
    assert user_name(migrate_engine, people.both) == "Test Person"


# F4: a new account cannot join without a name, and the link survives the refusal.
def test_a_new_account_needs_a_name_to_accept_an_invite(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    new_accounts: list[NewAccount],  # noqa: F811
) -> None:
    token = invite_token(people.a, fresh_email())
    client = new_client(app)

    for body in ({}, {"name": None}):
        refused = client.post(
            "/api/invites/accept", json={"token": token, "password": PASSWORD, **body}
        )
        assert refused.status_code == 422
        assert refused.json() == {"code": "name_required"}

    accepted = client.post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD, "name": " Bea "}
    )
    assert accepted.status_code == 201
    assert accepted.json()["name"] == "Bea"
    user = uuid.UUID(accepted.json()["user_id"])
    new_accounts.append(NewAccount(tenant_id=people.a, user_id=user))
    assert user_name(migrate_engine, user) == "Bea"
    assert display_name(people.a, user) == "Bea"


# F5: for an existing account the body's name is ignored; the membership copies the platform name.
def test_an_existing_named_user_joining_a_second_business_keeps_their_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)
    token = invite_token(people.a, email_of(people.only_b))

    accepted = new_client(app).post(
        "/api/invites/accept",
        json={"token": token, "password": PASSWORD, "name": "Somebody Else"},
    )

    assert accepted.status_code == 201
    assert accepted.json()["name"] == "Test Person"
    assert display_name(people.a, people.only_b) == "Test Person"
    assert user_name(migrate_engine, people.only_b) == "Test Person"


# F6: a legacy person in two businesses gives a name once: it fills the unset display names in all
# of them and never overwrites one an owner set.
def test_naming_a_legacy_person_fills_every_unset_display_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.both, None)
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE memberships SET display_name = 'Owner Set' WHERE user_id = :u"),
            {"u": people.both},
        )
    client = signed_in(app, people.a, people.both)

    named = client.put("/api/session/name", json={"name": " Nova "})

    assert named.status_code == 200
    assert named.headers["cache-control"] == "no-store"
    assert named.json()["name"] == "Nova"
    assert user_name(migrate_engine, people.both) == "Nova"
    assert display_name(people.a, people.both) == "Owner Set"
    assert display_name(people.b, people.both) == "Nova"
    (event,) = events(migrate_engine, action="user_name_set", actor_user_id=people.both)
    assert (event["target"], event["details"]) == (f"user:{people.both}", None)


# F7: the endpoint names a nameless person; it is not a rename.
def test_naming_someone_already_named_is_a_conflict(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client = signed_in(app, people.a, people.only_a)

    again = client.put("/api/session/name", json={"name": "Renamed"})

    assert (again.status_code, again.json()) == (409, {"code": "name_already_set"})
    assert user_name(migrate_engine, people.only_a) == "Test Person"


# F8: the app role has no way to write a name except set_own_name, and that only within the
# business it is acting for.
def test_the_app_role_cannot_name_anyone_outside_its_business(
    people: People, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.only_b, None)
    # Inside the person's own business, where users' policy lets the app role see them: the row is
    # visible and the UPDATE privilege exists, so only the missing UPDATE policy stops it.
    with tenant_context(people.b) as session:
        changed = session.execute(
            text("UPDATE users SET name = 'x' WHERE id = :u RETURNING id"), {"u": people.only_b}
        ).all()
    assert changed == []
    assert user_name(migrate_engine, people.only_b) is None

    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT set_own_name(:u, 'x')"), {"u": people.only_b}) is False
    assert user_name(migrate_engine, people.only_b) is None


def routes_behind_signed_in(app: FastAPI) -> list[tuple[str, str]]:
    def uses(dependant: Any) -> bool:
        return any(d.call is app_auth.signed_in or uses(d) for d in dependant.dependencies)

    found = []
    # include_router keeps the included router's routes behind an _IncludedRouter; none is included
    # under a prefix (main.py), so the route's own path is the whole path.
    included = (top.original_router.routes for top in app.routes if hasattr(top, "original_router"))
    for route in (r for routes in included for r in routes):
        dependant = getattr(route, "dependant", None)
        if dependant is not None and uses(dependant):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method, route.path))
    return found


# F9: deny by default. Every route that needs a session answers name_required to a nameless
# person, except exactly the three that let them give a name or leave.
def test_a_nameless_person_reaches_only_the_three_exempt_routes(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.both, None)
    client = signed_in(app, people.a, people.both)
    routes = routes_behind_signed_in(app)
    assert EXEMPT <= set(routes) and len(routes) >= 30

    reached = set()
    # Signing out everywhere ends this session, so it goes last.
    for method, path in sorted(routes, key=lambda r: r == ("DELETE", "/api/sessions")):
        url = re.sub(r"\{[^}]+\}", "x", path)
        # json={} on the rest: the json_only middleware answers 415 to a body-less write.
        response = client.request(method, url, json=None if method == "GET" else {})
        if response.headers.get("content-type", "").startswith("application/json") and (
            response.status_code == 403 and response.json() == {"code": "name_required"}
        ):
            continue
        reached.add((method, path))
    assert reached == EXEMPT


# F10: leaving is never gated.
def test_a_nameless_person_can_sign_out(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.both, None)
    everywhere = signed_in(app, people.a, people.both)
    other = signed_in(app, people.b, people.both)

    assert everywhere.request("DELETE", "/api/sessions", json={}).status_code == 204
    assert other.get("/api/session").status_code == 401

    single = signed_in(app, people.a, people.both)
    assert single.request("DELETE", "/api/session", json={}).status_code == 204
    assert single.get("/api/session").status_code == 401


# F11: the session read is how the console learns the name is missing.
def test_a_nameless_person_reads_their_session_with_a_null_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.both, None)

    response = signed_in(app, people.a, people.both).get("/api/session")

    assert response.status_code == 200
    assert response.json()["name"] is None


# F12: the gate reads the database on every request, not the cookie.
def test_removing_a_name_gates_a_live_session_at_once(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client = signed_in(app, people.a, people.only_a)
    assert client.get("/api/services").status_code == 200

    set_user_name(migrate_engine, people.only_a, None)

    gated = client.get("/api/services")
    assert (gated.status_code, gated.json()) == (403, {"code": "name_required"})


# F13: the name is asked before the role, so a nameless worker is told about their name.
def test_a_nameless_worker_on_an_owner_route_is_asked_for_a_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.only_a, None)

    response = signed_in(app, people.a, people.only_a).post("/api/invites", json={})

    assert (response.status_code, response.json()) == (403, {"code": "name_required"})


# F15: the old signatures are gone, and the new definer is pinned and not public.
def test_the_old_signatures_are_dropped_and_set_own_name_is_locked_down(
    migrate_engine: Engine,
) -> None:
    with migrate_engine.connect() as conn:
        for gone in ("complete_sign_up(bytea,text,text,text,text)", "accept_invite(bytea,text)"):
            assert conn.scalar(text("SELECT to_regprocedure(:s)"), {"s": gone}) is None
        for present in (
            "complete_sign_up(bytea,text,text,text,text,text)",
            "accept_invite(bytea,text,text)",
            "set_own_name(uuid,text)",
        ):
            assert conn.scalar(text("SELECT to_regprocedure(:s)"), {"s": present}) is not None
        config = conn.scalar(text("SELECT proconfig FROM pg_proc WHERE proname = 'set_own_name'"))
        assert config == ["search_path=pg_catalog, public, pg_temp"]
        for fn in ("set_own_name(uuid,text)", "accept_invite(bytea,text,text)"):
            assert not conn.scalar(
                text("SELECT has_function_privilege('public', :f, 'EXECUTE')"), {"f": fn}
            )
            assert conn.scalar(
                text("SELECT has_function_privilege('ziftbook_app', :f, 'EXECUTE')"), {"f": fn}
            )


# G1: naming yourself opens the gate for the very same cookie.
def test_after_naming_the_same_cookie_reaches_gated_routes(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.both, None)
    client = signed_in(app, people.a, people.both)
    assert client.get("/api/services").status_code == 403

    assert client.put("/api/session/name", json={"name": "Nova"}).status_code == 200

    assert client.get("/api/services").status_code == 200


# G2: the migration adds the column and a check that bites, and the downgrade takes them away.
def test_downgrading_and_upgrading_0032_removes_and_restores_the_name_column(
    migrated: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = text("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
    cfg = Config(toml_file=str(API_DIR / "pyproject.toml"))
    url = os.environ["ZIF_MIGRATE_DATABASE_URL"]
    options = urlencode({"options": "-c lock_timeout=5s"})
    monkeypatch.setenv("ZIF_MIGRATE_DATABASE_URL", f"{url}{'&' if '?' in url else '?'}{options}")
    legacy = uuid.uuid7()
    try:
        command.downgrade(cfg, "0031")
        with migrate_engine.begin() as conn:
            assert "name" not in set(conn.scalars(columns))
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e)"),
                {"u": legacy, "e": f"{legacy}@example.com"},
            )
            assert conn.scalar(text("SELECT to_regprocedure('set_own_name(uuid,text)')")) is None
    finally:
        command.upgrade(cfg, "head")
    try:
        with migrate_engine.connect() as conn:
            assert "name" in set(conn.scalars(columns))
            assert conn.scalar(text("SELECT name FROM users WHERE id = :u"), {"u": legacy}) is None
        for bad in ("''", "repeat('x', 61)"):
            with pytest.raises(IntegrityError) as error, migrate_engine.begin() as conn:
                conn.execute(text(f"UPDATE users SET name = {bad} WHERE id = :u"), {"u": legacy})
            assert isinstance(error.value.orig, CheckViolation)
            assert error.value.orig.diag.constraint_name == "ck_users_name_length"
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": legacy})
