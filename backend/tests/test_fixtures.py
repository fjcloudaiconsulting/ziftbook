from sqlalchemy import Engine, text

from app.db import tenant_context
from tests.conftest import People, email_of


def test_people_seed_keeps_its_rows_and_tenant_visibility(
    people: People, migrate_engine: Engine
) -> None:
    users = (people.only_a, people.only_b, people.both)
    assert len(set(users)) == 3
    assert all(user.version == 7 for user in users)
    assert people.a != people.b
    with migrate_engine.connect() as conn:
        assert set(
            conn.execute(
                text("SELECT id, name FROM tenants WHERE id IN (:a, :b)"),
                {"a": people.a, "b": people.b},
            )
        ) == {(people.a, "a"), (people.b, "b")}
        assert set(
            conn.execute(
                text("SELECT id, email, locale, name FROM users WHERE id IN (:x, :y, :z)"),
                {"x": people.only_a, "y": people.only_b, "z": people.both},
            )
        ) == {
            (user, email_of(user), locale, "Test Person")
            for user, locale in ((people.only_a, None), (people.only_b, None), (people.both, "nl"))
        }
    for tenant, expected in (
        (people.a, {(people.a, people.only_a, "worker"), (people.a, people.both, "owner")}),
        (people.b, {(people.b, people.only_b, "worker"), (people.b, people.both, "worker")}),
    ):
        with tenant_context(tenant) as session:
            assert session.scalar(text("SELECT current_user")) == "ziftbook_app"
            assert (
                set(session.execute(text("SELECT tenant_id, user_id, role FROM memberships")))
                == expected
            )
