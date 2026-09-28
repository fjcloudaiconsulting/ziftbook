import re
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from psycopg.errors import CheckViolation, InsufficientPrivilege
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.conftest import People, fresh_email, issue_link, new_client, wait_until_blocked
from tests.test_sign_up import app as _app  # noqa: F401 (fixtures)
from tests.test_sign_up import businesses as _businesses  # noqa: F401
from tests.test_sign_up import complete, created


def test_app_role_creates_tenants_with_uuidv7_ids(app_engine: Engine) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        tenant_id = conn.scalar(text("INSERT INTO tenants (name) VALUES ('Nail bar') RETURNING id"))
    assert tenant_id.version == 7


def test_a_new_business_defaults_to_the_netherlands(app_engine: Engine) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        business = conn.execute(
            text("INSERT INTO tenants (name) VALUES ('Nail bar') RETURNING country, currency")
        ).one()
    assert (business.country, business.currency) == ("NL", "EUR")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tenants SET currency = 'USD' WHERE id = :a",
        "UPDATE tenants SET country = 'BR' WHERE id = :a",
        "DELETE FROM tenants WHERE id = :a",
    ],
)
def test_the_app_role_cannot_rewrite_currency_country_or_delete_a_business(
    people: People, app_engine: Engine, statement: str
) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text(statement), {"a": people.a})
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_the_app_role_can_rename_a_business_and_lock_its_row(
    people: People, app_engine: Engine
) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        renamed = conn.execute(
            text("UPDATE tenants SET name = 'renamed' WHERE id = :a"), {"a": people.a}
        )
        assert renamed.rowcount == 1
        locked = conn.scalar(
            text("SELECT id FROM tenants WHERE id = :a FOR NO KEY UPDATE"), {"a": people.a}
        )
    assert locked == people.a


@pytest.mark.parametrize("country", ["nl", "N", "NLD", "NL\n"])
def test_a_business_country_must_be_two_uppercase_letters(app_engine: Engine, country: str) -> None:
    with pytest.raises(IntegrityError) as error, app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (name, country, currency) VALUES ('x', :c, 'EUR')"),
            {"c": country},
        )
    assert isinstance(error.value.orig, CheckViolation)


@pytest.mark.parametrize("currency", ["eur", "EU", "EURO", "EUR\n"])
def test_a_business_currency_must_be_three_uppercase_letters(
    app_engine: Engine, currency: str
) -> None:
    with pytest.raises(IntegrityError) as error, app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (name, country, currency) VALUES ('x', 'NL', :c)"),
            {"c": currency},
        )
    assert isinstance(error.value.orig, CheckViolation)


# ZIF-56: tenants.slug, derived by the tenants_default_slug trigger. Every name carries a fresh tag,
# so no test depends on what else is in tenants; every outcome is asserted, never raised.

TIMEOUT = text("SET LOCAL statement_timeout = '5s'")


def slug_of(conn: Connection, name: str, slug: str | None = None) -> str:
    """Insert a business; its slug, or the SQLSTATE of the refusal."""
    try:
        with conn.begin_nested():
            found: str = conn.scalar(
                text("INSERT INTO tenants (name, slug) VALUES (:n, :s) RETURNING slug"),
                {"n": name, "s": slug},
            )
            return found
    except DBAPIError as error:
        return str(getattr(error.orig, "sqlstate", error))


def tag() -> str:
    return uuid.uuid4().hex[:8]


def drop_tenants(migrate_engine: Engine, slugs: list[str]) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": slugs})


# 1. fence
def test_sign_up_derives_an_ascii_slug_from_the_business_name(
    _app: FastAPI,  # noqa: F811
    app_engine: Engine,
    _businesses: list[dict[str, Any]],  # noqa: F811
) -> None:
    t = tag()
    token = issue_link(app_engine, "sign_up", fresh_email())
    assert token is not None
    response = complete(new_client(_app), token, business=f"Salão da Ana {t}", country="NL")
    assert response.status_code == 201, response.content
    session = created(response, _businesses)
    with app_engine.connect() as conn:
        slug = conn.scalar(
            text("SELECT slug FROM tenants WHERE id = :t"), {"t": session["tenant_id"]}
        )
    assert slug == f"salao-da-ana-{t}"


# 2. fence
def test_two_businesses_with_one_name_get_x_then_x_2(app_engine: Engine) -> None:
    t = tag()
    with app_engine.connect() as conn:  # rolled back on close
        conn.execute(TIMEOUT)
        outcomes = [slug_of(conn, f"Nail bar {t}") for _ in range(2)]
    assert outcomes == [f"nail-bar-{t}", f"nail-bar-{t}-2"]


# 3. fence
def test_the_derivation_table(app_engine: Engine) -> None:
    t = tag()
    long = t + "x" * 52
    with app_engine.connect() as conn:  # rolled back on close
        conn.execute(TIMEOUT)
        a, calendar, bang, kanji, letters, long1, long2 = (
            slug_of(conn, n)
            for n in ("a", "Calendar", "!!!", "日本", f"Straße Øre Łódź {t}", long, long)
        )
        results = [a, calendar, bang, kanji, letters, long1, long2]
        valid = [conn.scalar(text("SELECT slug_ok(:s)"), {"s": r}) for r in results]
    assert re.fullmatch(r"a-\d+", a), a
    assert re.fullmatch(r"calendar-\d+", calendar), calendar
    assert re.fullmatch(r"business(-\d+)?", bang), bang
    assert re.fullmatch(r"business(-\d+)?", kanji), kanji
    assert letters == f"strasse-ore-lodz-{t}"
    assert len(long1) <= 40 and len(long2) <= 40, (long1, long2)
    assert long2 == f"{long1}-2"
    assert valid == [True] * 7


# 4. fence
def test_the_probe_gives_up_after_a_thousand_candidates(app_engine: Engine) -> None:
    t = tag()
    with app_engine.connect() as conn:  # rolled back on close
        conn.execute(TIMEOUT)
        conn.execute(
            text("""
            INSERT INTO tenants (name, slug)
            SELECT 'x', CASE WHEN n = 1 THEN :b ELSE :b || '-' || n END
            FROM generate_series(1, 1001) n
            """),
            {"b": f"c{t}"},
        )
        outcome = slug_of(conn, f"c{t}")
    assert outcome == "P0001"


def concurrent(app_engine: Engine, migrate_engine: Engine, a_name: str, b_name: str) -> str:
    """A inserts and holds; B inserts and blocks; A commits. B's outcome."""
    results: list[str] = []

    def b() -> None:
        with app_engine.begin() as conn:
            conn.execute(TIMEOUT)
            results.append(slug_of(conn, b_name))

    with app_engine.connect() as a:
        a.begin()
        a.execute(TIMEOUT)
        slug_of(a, a_name)
        thread = threading.Thread(target=b)
        thread.start()
        try:
            wait_until_blocked(migrate_engine, 1)
        finally:
            a.commit()
            thread.join()
    return results[0] if results else "no outcome"


# 5. fence
def test_two_concurrent_sign_ups_with_one_name_both_succeed(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    t = tag()
    try:
        outcome = concurrent(app_engine, migrate_engine, f"Studio Anna {t}", f"Studio Anna {t}")
        assert outcome == f"studio-anna-{t}-2"
    finally:
        drop_tenants(migrate_engine, [f"studio-anna-{t}", f"studio-anna-{t}-2"])


# 6. fence
def test_two_concurrent_sign_ups_meeting_on_one_candidate_both_succeed(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    t = tag()
    try:
        with app_engine.begin() as conn:
            assert slug_of(conn, f"Nail bar {t}") == f"nail-bar-{t}"
        outcome = concurrent(app_engine, migrate_engine, f"Nail bar {t} 2", f"Nail bar {t}")
        assert outcome == f"nail-bar-{t}-3"
    finally:
        drop_tenants(migrate_engine, [f"nail-bar-{t}", f"nail-bar-{t}-2", f"nail-bar-{t}-3"])


# 7. fence
@pytest.mark.parametrize("slug", ["Upper", "api", "calendar", "a--b", "-ab", "ab-", "ab", "a" * 41])
def test_a_malformed_or_reserved_slug_is_refused(app_engine: Engine, slug: str) -> None:
    with app_engine.connect() as conn:  # rolled back on close
        conn.execute(TIMEOUT)
        assert slug_of(conn, "Foo", slug) == "23514"


LOCALE_DIR = Path(__file__).parents[2] / "frontend" / "app" / "[locale]"


def routes() -> list[str]:
    return sorted(
        d.name
        for parent in (LOCALE_DIR, LOCALE_DIR / "(console)")
        for d in parent.iterdir()
        if d.is_dir() and not d.name.startswith(("_", "(", "["))
    )


# 8. fence
def test_every_top_level_web_route_is_reserved(app_engine: Engine) -> None:
    assert "booking" in routes() and "calendar" in routes()
    with app_engine.connect() as conn:
        taken = [r for r in routes() if conn.scalar(text("SELECT slug_ok(:s)"), {"s": r})]
    assert taken == []


# 9. fence
def test_the_app_role_cannot_change_a_slug(people: People, app_engine: Engine) -> None:
    with pytest.raises(ProgrammingError) as error, app_engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET slug = 'x-y-z' WHERE id = :a"), {"a": people.a})
    assert isinstance(error.value.orig, InsufficientPrivilege)
    assert error.value.orig.sqlstate == "42501"


# 10. fence
def test_an_explicit_valid_slug_is_kept(app_engine: Engine) -> None:
    t = tag()
    with app_engine.connect() as conn:  # rolled back on close
        conn.execute(TIMEOUT)
        assert slug_of(conn, f"Foo {t}", slug=f"bar-{t}") == f"bar-{t}"
