"""The audit log's table: append-only for the app, isolated by business, readable by operators."""

import uuid
from typing import Any

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.conftest import People
from tests.test_password_auth_db import HASH, NEW_HASH, digest, email_of, issue


def in_business(conn: Connection, tenant_id: uuid.UUID) -> None:
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})


def record(conn: Connection, target: str, **columns: Any) -> None:
    names = ", ".join(["action", "target", *columns])
    values = ", ".join([":action", ":target", *(f":{name}" for name in columns)])
    conn.execute(
        text(f"INSERT INTO audit_events ({names}) VALUES ({values})"),
        {"action": "test", "target": target, **columns},
    )


def reviewed(conn: Connection, target: str) -> list[uuid.UUID | None]:
    """The tenants of a test's events, read as an operator in the transaction already open."""
    conn.execute(text("SET LOCAL app.audit_review = 'on'"))
    return list(
        conn.scalars(
            text("SELECT tenant_id FROM audit_events WHERE target = :t ORDER BY id"), {"t": target}
        )
    )


@pytest.fixture
def target() -> str:
    """Rows can't be deleted, so each test's events carry their own marker."""
    return f"test:{uuid.uuid4()}"


@pytest.fixture
def events(people: People, app_engine: Engine, target: str) -> str:
    """One event in business a, one in b, and one belonging to no business."""
    for tenant_id in (people.a, people.b, None):
        with app_engine.begin() as conn:
            if tenant_id:
                in_business(conn, tenant_id)
            record(conn, target)
    return target


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events", "TRUNCATE audit_events"],
)
def test_the_app_can_not_change_or_remove_events(app_engine: Engine, statement: str) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(statement))
    assert isinstance(error.value.orig, InsufficientPrivilege)


@pytest.mark.parametrize("column", ["created_at", "id"])
def test_the_app_can_not_backdate_an_event(app_engine: Engine, target: str, column: str) -> None:
    value = "2020-01-01T00:00:00Z" if column == "created_at" else str(uuid.uuid4())
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO audit_events (action, target, {column}) VALUES ('test', :t, :v)"),
            {"t": target, "v": value},
        )
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_a_business_sees_only_its_own_events(
    people: People, app_engine: Engine, events: str
) -> None:
    with app_engine.begin() as conn:
        in_business(conn, people.a)
        seen = list(
            conn.scalars(
                text("SELECT tenant_id FROM audit_events WHERE target = :t"), {"t": events}
            )
        )
    assert seen == [people.a]

    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(text("SELECT count(*) FROM audit_events"))


@pytest.mark.parametrize("written_for", ["other_business", "no_business"])
def test_an_event_can_not_be_written_for_another_business(
    people: People, app_engine: Engine, target: str, written_for: str
) -> None:
    tenant_id = people.b if written_for == "other_business" else None
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        in_business(conn, people.a)
        record(conn, target, tenant_id=tenant_id)
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_without_a_business_an_event_can_only_belong_to_none(
    people: People, app_engine: Engine, target: str
) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        record(conn, target, tenant_id=people.a)
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_a_reused_connection_records_an_event_for_no_business(
    people: People, app_engine: Engine, migrate_engine: Engine, target: str
) -> None:
    # After a business transaction the setting is '' rather than missing: it must still mean none.
    with app_engine.connect() as conn:
        with conn.begin():
            in_business(conn, people.a)
            record(conn, target)
        with conn.begin():
            record(conn, target)

    with migrate_engine.begin() as conn:
        assert reviewed(conn, target) == [people.a, None]


def test_operators_read_every_event_only_when_reviewing(
    people: People, app_engine: Engine, migrate_engine: Engine, events: str
) -> None:
    with migrate_engine.connect() as conn:
        with conn.begin():
            hidden = conn.scalar(
                text("SELECT count(*) FROM audit_events WHERE target = :t"), {"t": events}
            )
        with conn.begin():  # the same connection again: the tenant setting is now ''
            in_business(conn, people.a)
        with conn.begin():
            assert reviewed(conn, events) == [people.a, people.b, None]
    assert hidden == 0

    with app_engine.begin() as conn:  # the app role gains nothing by asking for review
        in_business(conn, people.a)
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        assert list(
            conn.scalars(
                text("SELECT tenant_id FROM audit_events WHERE target = :t"), {"t": events}
            )
        ) == [people.a]


def test_a_password_reset_says_whose_password_it_changed(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO password_credentials VALUES (:u, :h)"),
            {"u": people.only_a, "h": HASH},
        )
    first = issue(app_engine, "password_reset", email_of(people.only_a))
    second = issue(app_engine, "password_reset", email_of(people.only_b))
    assert first is not None and second is not None

    with app_engine.begin() as conn:
        reset = conn.scalar(
            text("SELECT reset_password(:h, :p)"), {"h": digest(first), "p": NEW_HASH}
        )
        replayed = conn.scalar(
            text("SELECT reset_password(:h, :p)"), {"h": digest(first), "p": NEW_HASH}
        )
        # The old name still answers true or false, for app processes deployed before this one.
        old_name = conn.scalar(
            text("SELECT complete_password_reset(:h, :p)"), {"h": digest(second), "p": NEW_HASH}
        )

    assert (reset, replayed, old_name) == (people.only_a, None, True)
