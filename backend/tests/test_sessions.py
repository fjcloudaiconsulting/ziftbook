import hashlib
import uuid
from datetime import timedelta
from http.cookies import SimpleCookie

import pytest
from fastapi import Response
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import auth
from app.db import tenant_context
from tests.conftest import People


def start(tenant_id: uuid.UUID, user_id: uuid.UUID, **request: str | None) -> str:
    with tenant_context(tenant_id) as session:
        return auth.create(
            session,
            user_id,
            ip=request.get("ip", "203.0.113.9"),
            user_agent=request.get("user_agent", "pytest"),
        )


def stored(app_engine: Engine, token: str) -> dict[str, object] | None:
    with app_engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT *, expires_at - now() AS lifetime FROM sessions WHERE id_hash = :h"),
                {"h": hashlib.sha256(token.encode()).digest()},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def test_only_the_hash_of_a_random_token_is_stored(people: People, app_engine: Engine) -> None:
    token = start(people.a, people.both)

    assert len(token) >= 43  # 32 random bytes, so never a UUID
    row = stored(app_engine, token)
    assert row is not None
    assert (row["tenant_id"], row["user_id"], row["role"]) == (people.a, people.both, "owner")
    assert timedelta(days=29, hours=23) < row["lifetime"] <= auth.ABSOLUTE  # type: ignore[operator]


@pytest.mark.parametrize(
    ("tenant", "user", "role"),
    [("a", "only_b", "worker"), ("a", "both", "worker"), ("b", "both", "owner")],
)
def test_a_session_needs_the_membership_and_role_it_claims(
    people: People, app_engine: Engine, tenant: str, user: str, role: str
) -> None:
    with pytest.raises(IntegrityError) as error, app_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO sessions (id_hash, tenant_id, user_id, role, expires_at)
            VALUES (:h, :t, :u, :r, now() + interval '1 day')
            """),
            {
                "h": uuid.uuid4().bytes * 2,
                "t": getattr(people, tenant),
                "u": getattr(people, user),
                "r": role,
            },
        )
    assert isinstance(error.value.orig, ForeignKeyViolation)


def test_a_session_cannot_start_for_someone_outside_the_tenant(people: People) -> None:
    with pytest.raises(ValueError):
        start(people.a, people.only_b)


def test_removing_a_membership_ends_only_its_sessions(people: People, app_engine: Engine) -> None:
    in_a, in_b = start(people.a, people.both), start(people.b, people.both)

    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": people.both})

    assert stored(app_engine, in_a) is None
    assert stored(app_engine, in_b) is not None


def test_a_role_change_requires_ending_the_sessions_first(people: People) -> None:
    start(people.a, people.both)

    with pytest.raises(IntegrityError) as error, tenant_context(people.a) as session:
        session.execute(
            text("UPDATE memberships SET role = 'worker' WHERE user_id = :u"), {"u": people.both}
        )
    assert isinstance(error.value.orig, ForeignKeyViolation)


def test_starting_a_session_purges_expired_ones(people: People, app_engine: Engine) -> None:
    # Expired: another user in another tenant. Live: the same user and tenant signing in again.
    expired, live = start(people.b, people.only_b), start(people.a, people.only_a)
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE id_hash = :h"),
            {"h": hashlib.sha256(expired.encode()).digest()},
        )

    start(people.a, people.only_a)

    assert stored(app_engine, expired) is None
    assert stored(app_engine, live) is not None


def test_the_purge_skips_sessions_another_transaction_holds(
    people: People, app_engine: Engine
) -> None:
    # E.g. a membership being removed: waiting on its rows would queue sign-ins, or deadlock.
    expired = start(people.b, people.only_b)
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE id_hash = :h"),
            {"h": hashlib.sha256(expired.encode()).digest()},
        )

    with app_engine.begin() as holder:
        holder.execute(
            text("SELECT FROM sessions WHERE id_hash = :h FOR UPDATE"),
            {"h": hashlib.sha256(expired.encode()).digest()},
        )
        with tenant_context(people.a) as session:
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
            auth.create(session, people.only_a, ip=None, user_agent=None)

    assert stored(app_engine, expired) is not None


@pytest.mark.parametrize(
    ("ip", "kept"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8::1", "2001:db8::1"),
        ("testclient", None),
        ("fe80::1%eth0", None),  # Postgres inet has no zone ids
        (None, None),
    ],
)
def test_the_client_address_is_kept_only_when_valid(
    people: People, app_engine: Engine, ip: str | None, kept: str | None
) -> None:
    user_agent = "a" * 512 + "b" * 88
    row = stored(app_engine, start(people.a, people.only_a, ip=ip, user_agent=user_agent))

    assert row is not None
    assert (None if row["ip"] is None else str(row["ip"])) == kept
    assert row["user_agent"] == "a" * 512


def test_the_cookie_is_host_only_secure_and_unreadable_by_scripts() -> None:
    response = Response()
    auth.set_cookie(response, "token")
    auth.clear_cookie(response)
    set_cookie, cleared = (
        SimpleCookie(value.decode())
        for name, value in response.raw_headers
        if name == b"set-cookie"
    )

    for cookie, value, max_age in (
        (set_cookie, "token", str(int(auth.ABSOLUTE.total_seconds()))),
        (cleared, '""', "0"),
    ):
        morsel = cookie["__Host-session"]
        assert morsel.value == value.strip('"')
        assert (morsel["path"], morsel["max-age"], morsel["samesite"].lower()) == (
            "/",
            max_age,
            "lax",
        )
        assert morsel["secure"] and morsel["httponly"] and not morsel["domain"]
