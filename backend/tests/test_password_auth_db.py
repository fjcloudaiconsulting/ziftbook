"""The database side of password sign-in, sign-up and reset: tables the app role can't touch, and
SECURITY DEFINER functions that are its only way in."""

import hashlib
import secrets
import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app import auth
from app.db import SessionLocal, tenant_context
from tests.conftest import People, add_user

EXPIRE = "UPDATE email_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = :h"
HASH = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA"
NEW_HASH = "$argon2id$v=19$m=19456,t=2,p=1$bmV3c2FsdA$bmV3aGFzaG5ld2hhc2g"

VIOLATIONS = text("""
SELECT p.proname FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef
  AND (NOT coalesce('search_path=pg_catalog, public, pg_temp' = ANY (p.proconfig), false)
       OR has_function_privilege('public', p.oid, 'EXECUTE'))
ORDER BY 1
""")


def issue(app_engine: Engine, purpose: str, email: str, locale: str = "en") -> bytes | None:
    """A request plus its email job: a pending row, then a minted token. None if not minted."""
    token = secrets.token_bytes(32)
    with app_engine.begin() as conn:
        token_id = conn.scalar(
            text("SELECT start_email_token(:p, :e, :l)"), {"p": purpose, "e": email, "l": locale}
        )
        minted = conn.execute(
            text("SELECT * FROM mint_email_token(:id, :h)"),
            {"id": token_id, "h": hashlib.sha256(token).digest()},
        ).first()
    return token if minted else None


def digest(token: bytes) -> bytes:
    return hashlib.sha256(token).digest()


def email_of(user_id: uuid.UUID) -> str:
    return f"{user_id}@example.com"


@pytest.fixture
def created(migrate_engine: Engine, app_engine: Engine) -> Iterator[list[str]]:
    """Emails of accounts complete_sign_up creates; their rows are removed afterwards."""
    bound = SessionLocal.kw.get("bind") is None  # the people fixture may have bound it already
    if bound:
        SessionLocal.configure(bind=app_engine)
    emails: list[str] = []
    yield emails
    with migrate_engine.begin() as conn:
        rows = conn.execute(
            text("""
            SELECT u.id AS user_id, t.id AS tenant_id FROM users u
            JOIN password_credentials c ON c.user_id = u.id
            LEFT JOIN tenants t ON t.name = 'Studio ' || u.email
            WHERE u.email = ANY(:emails)
            """),
            {"emails": emails},
        ).all()
    for row in rows:
        if row.tenant_id:
            with tenant_context(row.tenant_id) as session:
                session.execute(text("DELETE FROM memberships"))
    with migrate_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM password_credentials WHERE user_id = ANY(:ids)"),
            {"ids": [r.user_id for r in rows]},
        )
        conn.execute(text("DELETE FROM users WHERE email = ANY(:emails)"), {"emails": emails})
        conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:ids)"),
            {"ids": [r.tenant_id for r in rows if r.tenant_id]},
        )
    if bound:
        SessionLocal.configure(bind=None)


@pytest.mark.parametrize("table", ["password_credentials", "email_tokens"])
@pytest.mark.parametrize("statement", ["SELECT * FROM {}", "DELETE FROM {}"])
def test_the_app_role_cannot_touch_credentials_or_tokens(
    app_engine: Engine, table: str, statement: str
) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(statement.format(table)))
    assert isinstance(error.value.orig, InsufficientPrivilege)


@pytest.fixture
def broken_functions(migrate_engine: Engine) -> Iterator[None]:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            CREATE FUNCTION broken_no_path() RETURNS int LANGUAGE sql SECURITY DEFINER
              AS 'SELECT 1';
            REVOKE EXECUTE ON FUNCTION broken_no_path() FROM PUBLIC;
            -- Without pg_temp last, a caller's temp table shadows the real one.
            CREATE FUNCTION broken_temp_first() RETURNS int LANGUAGE sql SECURITY DEFINER
              SET search_path = public AS 'SELECT 1';
            REVOKE EXECUTE ON FUNCTION broken_temp_first() FROM PUBLIC;
            CREATE FUNCTION broken_public() RETURNS int LANGUAGE sql SECURITY DEFINER
              SET search_path = pg_catalog, public, pg_temp AS 'SELECT 1';
            """)
        )
    yield
    with migrate_engine.begin() as conn:
        conn.execute(text("DROP FUNCTION broken_no_path(), broken_temp_first(), broken_public()"))


def test_definer_functions_pin_their_search_path_and_are_not_public(
    migrate_engine: Engine,
) -> None:
    with migrate_engine.connect() as conn:
        assert list(conn.scalars(VIOLATIONS)) == []


def test_the_definer_check_catches_unsafe_functions(
    migrate_engine: Engine, broken_functions: None
) -> None:
    with migrate_engine.connect() as conn:
        assert list(conn.scalars(VIOLATIONS)) == [
            "broken_no_path",
            "broken_public",
            "broken_temp_first",
        ]


def test_sign_in_lookup_finds_the_oldest_membership_without_a_tenant(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    # The b membership has the older id, but the a row (owner) is written first: neither row order,
    # tenant order nor role order gives b.
    user_id = add_user(app_engine)
    older, newer = uuid.uuid7(), uuid.uuid7()
    for membership_id, tenant_id, role in ((newer, people.a, "owner"), (older, people.b, "worker")):
        with tenant_context(tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role) VALUES (:i, :t, :u, :r)"
                ),
                {"i": membership_id, "t": tenant_id, "u": user_id, "r": role},
            )
    try:
        with app_engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM account_by_email(:e)"), {"e": email_of(user_id)}
            ).one()
            unknown = conn.execute(
                text("SELECT * FROM account_by_email('nobody@example.com')")
            ).all()
        assert (row.user_id, row.password_hash, row.tenant_id) == (user_id, None, people.b)
        assert unknown == []
    finally:
        for tenant_id in (people.a, people.b):
            with tenant_context(tenant_id) as session:
                session.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": user_id})
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_sign_in_lookup_leaves_the_callers_settings_as_they_were(people: People) -> None:
    with tenant_context(people.b) as session:
        session.execute(text("SELECT * FROM account_by_email(:e)"), {"e": email_of(people.both)})
        assert session.scalar(text("SELECT current_setting('app.tenant_id')")) == str(people.b)
        assert session.scalar(text("SELECT current_setting('app.sign_in', true)")) != "on"


def test_data_migrations_still_see_one_tenants_memberships(
    people: People, migrate_engine: Engine
) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        tenants = set(conn.scalars(text("SELECT tenant_id FROM memberships")))
    assert tenants == {people.a}


def test_the_app_role_gains_nothing_by_setting_sign_in(people: People) -> None:
    with tenant_context(people.a) as session:
        session.execute(text("SELECT set_config('app.sign_in', 'on', true)"))
        tenants = set(session.scalars(text("SELECT tenant_id FROM memberships")))
    assert tenants == {people.a}


def complete_sign_up(engine: Engine, token: bytes, email: str) -> str:
    with engine.begin() as conn:
        outcome: str = conn.scalar(
            text("SELECT outcome FROM complete_sign_up(:h, :p, :n)"),
            {"h": digest(token), "p": HASH, "n": f"Studio {email}"},
        )
    return outcome


def test_completing_sign_up_creates_the_business_once(
    app_engine: Engine, migrate_engine: Engine, created: list[str]
) -> None:
    email = f"{uuid.uuid4()}@example.com"
    created.append(email)
    token = issue(app_engine, "sign_up", email, "pt")
    assert token is not None

    assert complete_sign_up(app_engine, token, email) == "created"
    assert complete_sign_up(app_engine, token, email) == "invalid_token"

    with migrate_engine.connect() as conn:
        user = conn.execute(
            text("""
            SELECT u.locale, c.hash, t.id AS tenant_id FROM users u
            JOIN password_credentials c ON c.user_id = u.id
            JOIN tenants t ON t.name = :name
            WHERE u.email = :e
            """),
            {"e": email, "name": f"Studio {email}"},
        ).one()
    with tenant_context(user.tenant_id) as session:
        role = session.scalar(text("SELECT role FROM memberships"))
    assert (user.locale, user.hash, role) == ("pt", HASH, "owner")


def test_an_expired_sign_up_link_creates_nothing(
    app_engine: Engine, migrate_engine: Engine, created: list[str]
) -> None:
    email = f"{uuid.uuid4()}@example.com"
    token = issue(app_engine, "sign_up", email)
    assert token is not None
    with migrate_engine.begin() as conn:
        conn.execute(
            text(EXPIRE),
            {"h": digest(token)},
        )

    assert complete_sign_up(app_engine, token, email) == "invalid_token"


def test_completing_sign_up_for_an_email_registered_since_changes_nothing(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    # The link went out while the email was free; an account with it appeared before it was used.
    email = f"{uuid.uuid4()}@example.com"
    token = issue(app_engine, "sign_up", email)
    assert token is not None
    user_id = add_user(app_engine, email)
    with migrate_engine.connect() as conn:
        tenants_before = conn.scalar(text("SELECT count(*) FROM tenants"))

    try:
        assert complete_sign_up(app_engine, token, email) == "already_registered"
        with migrate_engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM tenants")) == tenants_before
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM password_credentials WHERE user_id = :u"),
                    {"u": user_id},
                )
                == 0
            )
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def test_two_links_for_one_email_completed_together_create_one_account(
    app_engine: Engine, migrate_engine: Engine, created: list[str]
) -> None:
    email = f"{uuid.uuid4()}@example.com"
    created.append(email)
    first, second = issue(app_engine, "sign_up", email), issue(app_engine, "sign_up", email)
    assert first is not None and second is not None
    outcomes: dict[str, str] = {}

    def complete_second() -> None:
        outcomes["second"] = complete_sign_up(app_engine, second, email)

    with app_engine.begin() as conn:
        outcomes["first"] = conn.scalar(
            text("SELECT outcome FROM complete_sign_up(:h, :p, :n)"),
            {"h": digest(first), "p": HASH, "n": f"Studio {email}"},
        )
        # The second completion runs while the first is uncommitted, and waits on its user row.
        thread = threading.Thread(target=complete_second)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()
    thread.join(timeout=10)

    assert outcomes == {"first": "created", "second": "already_registered"}
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 1


def test_completing_sign_up_leaves_the_callers_tenant_as_it_was(
    people: People, app_engine: Engine, created: list[str]
) -> None:
    email = f"{uuid.uuid4()}@example.com"
    created.append(email)
    token = issue(app_engine, "sign_up", email)
    assert token is not None

    with tenant_context(people.a) as session:
        session.execute(
            text("SELECT complete_sign_up(:h, :p, :n)"),
            {"h": digest(token), "p": HASH, "n": f"Studio {email}"},
        )
        assert session.scalar(text("SELECT current_setting('app.tenant_id')")) == str(people.a)


def test_a_token_only_works_for_its_own_purpose(people: People, app_engine: Engine) -> None:
    reset = issue(app_engine, "password_reset", email_of(people.only_a))
    sign_up = issue(app_engine, "sign_up", f"{uuid.uuid4()}@example.com")
    assert reset is not None and sign_up is not None

    assert complete_sign_up(app_engine, reset, "x@example.com") == "invalid_token"
    with app_engine.begin() as conn:
        assert (
            conn.scalar(
                text("SELECT complete_password_reset(:h, :p)"),
                {"h": digest(sign_up), "p": NEW_HASH},
            )
            is False
        )


def test_a_password_reset_ends_every_session_and_every_other_reset_link(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    sessions = {}
    for tenant_id, user_id in (
        (people.a, people.both),
        (people.b, people.both),
        (people.a, people.only_a),
    ):
        with tenant_context(tenant_id) as session:
            sessions[(tenant_id, user_id)] = auth.create(session, user_id, ip=None, user_agent=None)
    used = issue(app_engine, "password_reset", email_of(people.both))
    other = issue(app_engine, "password_reset", email_of(people.both))
    assert used is not None and other is not None

    with app_engine.begin() as conn:
        done = conn.scalar(
            text("SELECT complete_password_reset(:h, :p)"), {"h": digest(used), "p": NEW_HASH}
        )
        again = conn.scalar(
            text("SELECT complete_password_reset(:h, :p)"), {"h": digest(other), "p": HASH}
        )

    assert (done, again) == (True, False)
    with migrate_engine.begin() as conn:
        assert (
            conn.scalar(
                text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.both}
            )
            == NEW_HASH
        )
        remaining = set(
            conn.scalars(
                text("SELECT user_id FROM sessions WHERE user_id = ANY(:u)"),
                {"u": [people.both, people.only_a]},
            )
        )
        assert remaining == {people.only_a}
        conn.execute(
            text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": people.both}
        )


def test_minting_sets_the_lifetime_and_skips_resets_for_unknown_emails(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    lifetimes = {}
    for purpose, email in (
        ("sign_up", f"{uuid.uuid4()}@example.com"),
        ("password_reset", email_of(people.only_a)),
    ):
        token = issue(app_engine, purpose, email)
        assert token is not None
        with migrate_engine.connect() as conn:
            lifetimes[purpose] = conn.scalar(
                text("SELECT expires_at - now() FROM email_tokens WHERE token_hash = :h"),
                {"h": digest(token)},
            )
    assert timedelta(hours=23, minutes=59) < lifetimes["sign_up"] <= timedelta(hours=24)
    assert timedelta(minutes=14) < lifetimes["password_reset"] <= timedelta(minutes=15)

    unknown = f"{uuid.uuid4()}@example.com"
    assert issue(app_engine, "password_reset", unknown) is None
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT count(*) FROM email_tokens WHERE email = :e"), {"e": unknown})
            == 0
        )


def test_minting_again_replaces_the_link_and_expired_requests_are_not_minted(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    H1, H2, H3 = (secrets.token_bytes(32) for _ in range(3))
    email = f"{uuid.uuid4()}@example.com"
    with app_engine.begin() as conn:
        token_id = conn.scalar(text("SELECT start_email_token('sign_up', :e, 'en')"), {"e": email})
        first = conn.execute(
            text("SELECT * FROM mint_email_token(:id, :h)"), {"id": token_id, "h": H1}
        ).one()
        conn.execute(text("SELECT * FROM mint_email_token(:id, :h)"), {"id": token_id, "h": H2})
        live_old = conn.scalar(text("SELECT email_token_live(:h, 'sign_up')"), {"h": H1})
        live_new = conn.scalar(text("SELECT email_token_live(:h, 'sign_up')"), {"h": H2})
        stale_id = conn.scalar(text("SELECT start_email_token('sign_up', :e, 'en')"), {"e": email})
    assert (first.registered, live_old, live_new) == (False, False, True)

    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE email_tokens SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": stale_id},
        )
    with app_engine.begin() as conn:
        assert (
            conn.execute(
                text("SELECT * FROM mint_email_token(:id, :h)"), {"id": stale_id, "h": H3}
            ).all()
            == []
        )


def test_minting_a_sign_up_for_a_registered_email_says_so_and_keeps_no_link(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    H4 = secrets.token_bytes(32)
    with app_engine.begin() as conn:
        token_id = conn.scalar(
            text("SELECT start_email_token('sign_up', :e, 'en')"), {"e": email_of(people.both)}
        )
        minted = conn.execute(
            text("SELECT * FROM mint_email_token(:id, :h)"), {"id": token_id, "h": H4}
        ).one()
    # both chose Dutch: their own language wins over the request's.
    assert (minted.registered, minted.locale) == (True, "nl")
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT count(*) FROM email_tokens WHERE id = :id"), {"id": token_id})
            == 0
        )


def test_a_link_is_live_only_unexpired_and_for_its_purpose(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    token = issue(app_engine, "password_reset", email_of(people.only_a))
    assert token is not None
    H9 = secrets.token_bytes(32)
    with app_engine.connect() as conn:
        checks = [
            conn.scalar(text("SELECT email_token_live(:h, :p)"), {"h": h, "p": p})
            for h, p in (
                (digest(token), "password_reset"),
                (digest(token), "sign_up"),
                (H9, "password_reset"),
            )
        ]
    with migrate_engine.begin() as conn:
        conn.execute(
            text(EXPIRE),
            {"h": digest(token)},
        )
    with app_engine.connect() as conn:
        expired = conn.scalar(
            text("SELECT email_token_live(:h, 'password_reset')"), {"h": digest(token)}
        )
    assert (checks, expired) == ([True, False, False], False)


def test_completing_a_reset_for_a_vanished_account_writes_nothing(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    token = secrets.token_bytes(32)
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO email_tokens (purpose, email, locale, token_hash, expires_at)
            VALUES ('password_reset', 'gone@example.com', 'en', :h, now() + interval '15 minutes')
            """),
            {"h": digest(token)},
        )
    with app_engine.begin() as conn:
        assert (
            conn.scalar(
                text("SELECT complete_password_reset(:h, :p)"), {"h": digest(token), "p": NEW_HASH}
            )
            is False
        )


def test_a_reset_link_can_not_be_forged_through_a_temporary_table(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    # With pg_temp first on the search path, this temp table would stand in for the real one.
    token = secrets.token_bytes(32)
    with app_engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TEMP TABLE email_tokens (purpose text, email text, locale text,
              token_hash bytea, expires_at timestamptz)
            """)
        )
        conn.execute(
            text("""
            INSERT INTO pg_temp.email_tokens
            VALUES ('password_reset', :e, 'en', :h, now() + interval '1 hour')
            """),
            {"e": email_of(people.only_a), "h": digest(token)},
        )
        forged = conn.scalar(
            text("SELECT complete_password_reset(:h, :p)"), {"h": digest(token), "p": NEW_HASH}
        )
    assert forged is False
