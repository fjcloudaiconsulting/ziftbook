"""invites and accept_invite: the table's constraints, its tenant isolation, and the definer
function the invite-accept endpoint (PR 7) will call."""

import hashlib
import secrets
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from psycopg.errors import CheckViolation, UniqueViolation
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal, tenant_context
from tests.conftest import People, add_password, email_of, fresh_email
from tests.test_password_auth_db import HASH, wait_until_blocked


def pending(tenant_id: uuid.UUID, email: str, expires: str = "1 hour") -> uuid.UUID:
    """An unsent invite: expires_at set from `expires`, so a missing reset in the job shows."""
    with tenant_context(tenant_id) as session:
        invite_id: uuid.UUID = session.scalar(
            text("""
            INSERT INTO invites (tenant_id, email, expires_at)
            VALUES (current_setting('app.tenant_id')::uuid, :email,
                    now() + CAST(:expires AS interval))
            RETURNING id
            """),
            {"email": email, "expires": expires},
        )
    return invite_id


def minted(tenant_id: uuid.UUID, email: str) -> tuple[uuid.UUID, str]:
    """A sent invite: token_hash = sha256(secret), a 7-day expiry. Returns (id, secret)."""
    secret = secrets.token_urlsafe(32)
    with tenant_context(tenant_id) as session:
        invite_id: uuid.UUID = session.scalar(
            text("""
            INSERT INTO invites (tenant_id, email, token_hash, expires_at)
            VALUES (current_setting('app.tenant_id')::uuid, :email, :hash,
                    now() + interval '7 days')
            RETURNING id
            """),
            {"email": email, "hash": hashlib.sha256(secret.encode()).digest()},
        )
    return invite_id, secret


@dataclass(frozen=True)
class NewAccount:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


@pytest.fixture
def new_accounts(people: People, migrate_engine: Engine) -> Iterator[list[NewAccount]]:
    """Accounts accept_invite creates in a test. Torn down before `people`'s own teardown (this
    fixture depends on `people`, so pytest tears it down first): their memberships, then, as
    migrate, their password_credentials, then their users rows."""
    accounts: list[NewAccount] = []
    yield accounts
    for account in accounts:
        with tenant_context(account.tenant_id) as session:
            session.execute(
                text("DELETE FROM memberships WHERE user_id = :u"), {"u": account.user_id}
            )
    with migrate_engine.begin() as conn:
        ids = [account.user_id for account in accounts]
        conn.execute(text("DELETE FROM password_credentials WHERE user_id = ANY(:u)"), {"u": ids})
        conn.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": ids})


def call(tenant_id: uuid.UUID, token_hash: bytes | None, password_hash: str | None) -> Any:
    with tenant_context(tenant_id) as session:
        return session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"), {"h": token_hash, "p": password_hash}
        ).one()


def digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def _insert(conn: Connection, tenant_id: uuid.UUID, email: str, token_hash: bytes | None) -> None:
    conn.execute(
        text("""
        INSERT INTO invites (tenant_id, email, token_hash, expires_at)
        VALUES (:t, :e, :h, now() + interval '1 hour')
        """),
        {"t": tenant_id, "e": email, "h": token_hash},
    )


# T1 fence: missing CHECKs, or a UNIQUE without tenant_id, let a violation through, or refuse a
# combination the schema should accept.
def test_invites_check_and_unique_constraints(app_engine: Engine, people: People) -> None:
    hash32 = secrets.token_bytes(32)
    violations: list[tuple[type[Exception], list[tuple[uuid.UUID, str, bytes | None]]]] = [
        (CheckViolation, [(people.a, fresh_email().upper(), None)]),
        (CheckViolation, [(people.a, fresh_email(), secrets.token_bytes(31))]),
        (
            UniqueViolation,
            [(people.a, e := fresh_email(), None), (people.a, e, None)],
        ),
        (
            UniqueViolation,
            [(people.a, fresh_email(), hash32), (people.a, fresh_email(), hash32)],
        ),
    ]
    for error_type, rows in violations:
        with app_engine.connect() as conn:
            with conn.begin() as tx:
                conn.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)}
                )
                caught: IntegrityError | None = None
                try:
                    for tenant_id, email, token_hash in rows:
                        _insert(conn, tenant_id, email, token_hash)
                except IntegrityError as error:
                    caught = error
                finally:
                    tx.rollback()
            assert caught is not None and isinstance(caught.orig, error_type)

    accepted: list[list[tuple[uuid.UUID, str, bytes | None]]] = [
        [(people.a, e1 := fresh_email(), None), (people.b, e1, None)],
        [(people.a, fresh_email(), hash32), (people.b, fresh_email(), hash32)],
        [(people.a, fresh_email(), None), (people.a, fresh_email(), None)],
    ]
    for rows in accepted:
        with app_engine.connect() as conn:
            with conn.begin() as tx:
                for tenant_id, email, token_hash in rows:
                    conn.execute(
                        text("SELECT set_config('app.tenant_id', :t, true)"),
                        {"t": str(tenant_id)},
                    )
                    _insert(conn, tenant_id, email, token_hash)
                tx.rollback()


def test_invites_are_isolated_by_tenant(people: People) -> None:
    pending(people.a, fresh_email())
    with tenant_context(people.b) as session:
        assert session.scalar(text("SELECT count(*) FROM invites")) == 0


def test_accept_invite_is_a_pinned_and_non_public_definer(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        assert conn.scalar(
            text(
                "SELECT has_function_privilege("
                "'ziftbook_app', 'accept_invite(bytea,text)', 'EXECUTE')"
            )
        )


# A1 fence: a fresh email, accepted with a password, becomes an account, a worker membership, and
# uses up the invite.
def test_accept_creates_an_account_and_a_membership(
    migrate_engine: Engine, people: People, new_accounts: list[NewAccount]
) -> None:
    email = fresh_email()
    invite_id, secret = minted(people.a, email)

    with tenant_context(people.a) as session:
        result = session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"), {"h": digest(secret), "p": HASH}
        ).one()
        # A9 guard: accept_invite never changes the caller's tenant.
        assert session.scalar(text("SELECT current_setting('app.tenant_id')")) == str(people.a)
    assert result.outcome == "accepted"
    new_accounts.append(NewAccount(people.a, result.account_id))

    with migrate_engine.connect() as conn:
        user = conn.execute(
            text("SELECT email, locale FROM users WHERE id = :u"), {"u": result.account_id}
        ).one()
        cred = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"),
            {"u": result.account_id},
        )
    assert (user.email, user.locale) == (email, None)
    assert cred == HASH
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": result.account_id}
        )
        gone = session.scalar(text("SELECT count(*) FROM invites WHERE id = :i"), {"i": invite_id})
    assert role == "worker"
    assert gone == 0


# A2 fence: no live invite (replayed, expired, or unsent) is ever accepted, and nothing is written.
def test_accept_refuses_a_replayed_expired_or_unsent_invite(
    migrate_engine: Engine, people: People, new_accounts: list[NewAccount]
) -> None:
    email = fresh_email()
    invite_id, secret = minted(people.a, email)
    with tenant_context(people.a) as session:
        first = session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"), {"h": digest(secret), "p": HASH}
        ).one()
    assert first.outcome == "accepted"
    new_accounts.append(NewAccount(people.a, first.account_id))

    replay = call(people.a, digest(secret), HASH)
    assert replay.outcome == "invalid_token"

    expired_email = fresh_email()
    expired_id, expired_secret = minted(people.a, expired_email)
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("UPDATE invites SET expires_at = now() - interval '1 second' WHERE id = :i"),
            {"i": expired_id},
        )
    expired = call(people.a, digest(expired_secret), HASH)
    with tenant_context(people.a) as session:
        still_there = session.scalar(
            text("SELECT count(*) FROM invites WHERE id = :i"), {"i": expired_id}
        )
    assert expired.outcome == "invalid_token"
    assert still_there == 1

    unsent_id = pending(people.a, fresh_email())
    random_try = call(people.a, secrets.token_bytes(32), HASH)
    null_try = call(people.a, None, HASH)
    with tenant_context(people.a) as session:
        unsent_still_there = session.scalar(
            text("SELECT count(*) FROM invites WHERE id = :i"), {"i": unsent_id}
        )
    assert random_try.outcome == "invalid_token"
    assert null_try.outcome == "invalid_token"
    assert unsent_still_there == 1


# A3 fence: a token only works under the tenant id it was minted for.
def test_accept_binds_to_its_own_tenant(migrate_engine: Engine, people: People) -> None:
    email = fresh_email()
    invite_id, secret = minted(people.a, email)

    swapped = call(people.b, digest(secret), HASH)
    assert swapped.outcome == "invalid_token"
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 0
    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT count(*) FROM invites WHERE id = :i"), {"i": invite_id})

    with pytest.raises(DBAPIError), SessionLocal.begin() as session:
        session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"), {"h": digest(secret), "p": HASH}
        )


# A4 fence: an invite never sets an existing account's password.
def test_accept_never_touches_an_existing_accounts_password(
    migrate_engine: Engine, people: People
) -> None:
    add_password(migrate_engine, people.only_b, "short")
    with migrate_engine.connect() as conn:
        original = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    invite_id, secret = minted(people.a, email_of(people.only_b))

    outcome = call(people.a, digest(secret), HASH)
    assert outcome.outcome == "account_exists"

    with migrate_engine.connect() as conn:
        unchanged = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    assert unchanged == original
    with tenant_context(people.a) as session:
        membership = session.scalar(
            text("SELECT count(*) FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
        still_there = session.scalar(
            text("SELECT count(*) FROM invites WHERE id = :i"), {"i": invite_id}
        )
    assert membership == 0
    assert still_there == 1


# A5 fence: an existing account accepting with no password only gains a membership.
def test_accept_for_an_existing_account_only_adds_a_membership(
    migrate_engine: Engine, people: People
) -> None:
    add_password(migrate_engine, people.only_b, "short")
    with migrate_engine.connect() as conn:
        original = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    invite_id, secret = minted(people.a, email_of(people.only_b))

    outcome = call(people.a, digest(secret), None)
    assert (outcome.outcome, outcome.account_id) == ("accepted", people.only_b)

    with tenant_context(people.a) as session:
        role_a = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
        gone = session.scalar(text("SELECT count(*) FROM invites WHERE id = :i"), {"i": invite_id})
    with tenant_context(people.b) as session:
        role_b = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    with migrate_engine.connect() as conn:
        unchanged = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    assert role_a == "worker"
    assert role_b == "worker"
    assert gone == 0
    assert unchanged == original


# A6 fence: accepting for someone already a member is a no-op on their role, and uses up the link.
@pytest.mark.parametrize("attr,expected_role", [("only_a", "worker"), ("both", "owner")])
def test_accept_for_an_already_member_leaves_the_role_unchanged(
    people: People, attr: str, expected_role: str
) -> None:
    user_id: uuid.UUID = getattr(people, attr)
    invite_id, secret = minted(people.a, email_of(user_id))

    outcome = call(people.a, digest(secret), None)
    assert (outcome.outcome, outcome.account_id) == ("already_member", user_id)

    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": user_id}
        )
        gone = session.scalar(text("SELECT count(*) FROM invites WHERE id = :i"), {"i": invite_id})
    assert role == expected_role
    assert gone == 0


# A7 fence: two accepts of the same new-account link serialize on FOR UPDATE; the loser finds
# nothing, and only one account is ever created.
def test_concurrent_accepts_of_the_same_link_serialize(
    migrate_engine: Engine, people: People, new_accounts: list[NewAccount]
) -> None:
    email = fresh_email()
    invite_id, secret = minted(people.a, email)
    results: list[object] = []

    def accept() -> None:
        try:
            results.append(call(people.a, digest(secret), HASH))
        except Exception as error:  # a deadlock or unexpected error lands here too
            results.append(error)

    with SessionLocal(info={"tenant_id": people.a}) as session, session.begin():
        first = session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"), {"h": digest(secret), "p": HASH}
        ).one()
        thread = threading.Thread(target=accept)
        thread.start()
        wait_until_blocked(migrate_engine, 1)
    thread.join(timeout=10)

    assert first.outcome == "accepted"
    new_accounts.append(NewAccount(people.a, first.account_id))
    assert len(results) == 1
    assert getattr(results[0], "outcome", None) == "invalid_token"
    with migrate_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM users WHERE email = :e"), {"e": email}) == 1
