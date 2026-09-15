import uuid

import pytest
from psycopg.errors import CheckViolation, UniqueViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import tenant_context
from tests.conftest import People, add_membership, add_user


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
