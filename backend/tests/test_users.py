import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from psycopg.errors import CheckViolation, UniqueViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal, tenant_context


@dataclass(frozen=True)
class People:
    a: uuid.UUID  # tenant
    b: uuid.UUID  # tenant
    only_a: uuid.UUID  # user, worker in a
    only_b: uuid.UUID  # user, worker in b
    both: uuid.UUID  # user, owner in a and worker in b


def add_user(engine: Engine, email: str | None = None, locale: str | None = None) -> uuid.UUID:
    # The id comes from the app: a new user is invisible to its own policy, so RETURNING would fail.
    user_id = uuid.uuid7()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email, locale) VALUES (:id, :email, :locale)"),
            {"id": user_id, "email": email or f"{user_id}@example.com", "locale": locale},
        )
    return user_id


def add_membership(tenant_id: uuid.UUID, user_id: uuid.UUID, role: str = "worker") -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r)"),
            {"t": tenant_id, "u": user_id, "r": role},
        )


@pytest.fixture
def people(app_engine: Engine, migrate_engine: Engine) -> Iterator[People]:
    SessionLocal.configure(bind=app_engine)
    with app_engine.begin() as conn:
        a = conn.scalar(text("INSERT INTO tenants (name) VALUES ('a') RETURNING id"))
        b = conn.scalar(text("INSERT INTO tenants (name) VALUES ('b') RETURNING id"))
    people = People(
        a=a,
        b=b,
        only_a=add_user(app_engine),
        only_b=add_user(app_engine),
        both=add_user(app_engine, locale="nl"),
    )
    add_membership(a, people.only_a)
    add_membership(b, people.only_b)
    add_membership(a, people.both, "owner")
    add_membership(b, people.both)
    yield people
    for tenant_id in (a, b):
        with tenant_context(tenant_id) as session:
            session.execute(text("DELETE FROM memberships"))
    # The app role can't delete users; the test cleans up as the migrate role.
    with migrate_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM users WHERE id IN (:x, :y, :z)"),
            {"x": people.only_a, "y": people.only_b, "z": people.both},
        )
        conn.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": a, "b": b})
    SessionLocal.configure(bind=None)


@pytest.mark.parametrize(
    ("email", "locale"), [("Someone@example.com", None), ("someone@example.com", "de")]
)
def test_users_hold_a_lowercase_email_and_a_supported_locale(
    app_engine: Engine, email: str, locale: str | None
) -> None:
    with pytest.raises(IntegrityError) as error:
        add_user(app_engine, f"{uuid.uuid4()}{email}", locale)
    assert isinstance(error.value.orig, CheckViolation)


def test_an_email_belongs_to_one_user(people: People, app_engine: Engine) -> None:
    with pytest.raises(IntegrityError) as error:
        add_user(app_engine, f"{people.only_a}@example.com")
    assert isinstance(error.value.orig, UniqueViolation)


def test_a_membership_has_a_known_role_and_exists_once_per_tenant(people: People) -> None:
    with pytest.raises(IntegrityError) as error:
        add_membership(people.a, people.only_b, "admin")
    assert isinstance(error.value.orig, CheckViolation)

    with pytest.raises(IntegrityError) as error:
        add_membership(people.a, people.only_a, "owner")
    assert isinstance(error.value.orig, UniqueViolation)


def test_a_tenant_sees_only_its_own_members(people: People) -> None:
    with tenant_context(people.a) as session:
        visible = set(
            session.scalars(
                text("SELECT id FROM users WHERE id IN (:x, :y, :z)"),
                {"x": people.only_a, "y": people.only_b, "z": people.both},
            )
        )
    assert visible == {people.only_a, people.both}


def test_a_tenant_cannot_change_or_delete_a_user_it_shares(people: People) -> None:
    # A user's email is their identity in every tenant: tenant a must not be able to redirect it.
    with tenant_context(people.a) as session:
        params = {"id": people.both}
        session.execute(
            text("UPDATE users SET email = 'attacker@example.com' WHERE id = :id"), params
        )
        session.execute(text("DELETE FROM users WHERE id = :id"), params)

    with tenant_context(people.b) as session:
        row = session.execute(
            text("SELECT email, locale FROM users WHERE id = :id"), {"id": people.both}
        ).one()
    assert tuple(row) == (f"{people.both}@example.com", "nl")


def test_reading_users_without_a_tenant_context_raises(people: People, app_engine: Engine) -> None:
    # Rows exist (the fixture): on an empty table Postgres may never evaluate the policy.
    with app_engine.connect() as conn, pytest.raises(DBAPIError):
        conn.execute(text("SELECT id FROM users"))
