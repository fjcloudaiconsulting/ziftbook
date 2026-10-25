"""ZIF-102: a person's language is their own (users.locale), set by themselves, and the business's
language is only the fallback. PUT /api/session/locale writes it through set_own_locale (0060)."""

import uuid
from typing import get_args

import pytest
from fastapi import FastAPI
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from app.business_settings import Locale
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, end_membership, save_setting, signed_in
from tests.test_mail import clean_outbox, send_hello, subjects_sent_to  # noqa: F401
from tests.test_names import set_user_name


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def locale_of(engine: Engine, user_id: uuid.UUID) -> str | None:
    with engine.connect() as conn:
        found: str | None = conn.scalar(
            text("SELECT locale FROM users WHERE id = :u"), {"u": user_id}
        )
    return found


# 1: FENCE. Wrong impl: no route, or a SessionOut that does not carry the person's own language.
def test_a_person_sets_their_own_language(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client = signed_in(app, people.a, people.only_a)

    response = client.put("/api/session/locale", json={"locale": "pt"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["locale"] == "pt"
    assert client.get("/api/session").json()["locale"] == "pt"
    assert locale_of(migrate_engine, people.only_a) == "pt"


# 2: FENCE. Wrong impl: the language stored per membership or as a business setting.
def test_the_language_follows_the_person_into_their_other_business(
    people: People, app: FastAPI
) -> None:
    signed_in(app, people.a, people.both).put("/api/session/locale", json={"locale": "pt"})

    assert signed_in(app, people.b, people.both).get("/api/session").json()["locale"] == "pt"


# 3: FENCE. Wrong impl: writing the business's language setting.
def test_a_person_s_language_never_changes_the_business_language(
    people: People, app: FastAPI
) -> None:
    save_setting(people.a, "language", "nl")
    client = signed_in(app, people.a, people.both)

    client.put("/api/session/locale", json={"locale": "pt"})

    assert client.get("/api/settings").json()["language"] == "nl"


# 4: FENCE. Wrong impl: null refused, or stored as a language ("en"), so the business's language
# never applies again.
def test_null_clears_the_preference_and_email_falls_back_to_the_business(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    clean_outbox: None,  # noqa: F811
) -> None:
    save_setting(people.a, "language", "pt")
    client = signed_in(app, people.a, people.both)  # both chose nl themselves

    response = client.put("/api/session/locale", json={"locale": None})

    assert (response.status_code, response.json()["locale"]) == (200, None)
    assert locale_of(migrate_engine, people.both) is None
    send_hello(people.a, people.both)
    assert subjects_sent_to(people.both) == ["Olá do ziftbook"]


# 5: guard. Every supported language is accepted, read from the one list (ruling 5).
@pytest.mark.parametrize("locale", get_args(Locale))
def test_every_supported_language_is_accepted(
    people: People, app: FastAPI, migrate_engine: Engine, locale: str
) -> None:
    response = signed_in(app, people.a, people.only_a).put(
        "/api/session/locale", json={"locale": locale}
    )

    assert response.status_code == 200
    assert locale_of(migrate_engine, people.only_a) == locale


# 6: FENCE. Wrong impl: a lax model (case-folding, a default when the field is missing) or one that
# accepts a target user.
@pytest.mark.parametrize(
    "body",
    [
        {"locale": "de"},
        {"locale": "PT"},
        {"locale": ""},
        {},
        {"locale": "pt", "user_id": "00000000-0000-0000-0000-000000000000"},
    ],
    ids=["unsupported", "uppercase", "empty", "missing", "user_id"],
)
def test_anything_else_is_refused(
    people: People, app: FastAPI, migrate_engine: Engine, body: dict[str, str]
) -> None:
    response = signed_in(app, people.a, people.both).put("/api/session/locale", json=body)

    assert response.status_code == 422
    assert locale_of(migrate_engine, people.both) == "nl"


def set_own_locale(tenant_id: uuid.UUID, user_id: uuid.UUID, *, sign_in: bool = False) -> bool:
    with tenant_context(tenant_id) as session:
        if sign_in:
            session.execute(text("SELECT set_config('app.sign_in', 'on', true)"))
        written: bool = session.scalar(text("SELECT set_own_locale(:u, 'pt')"), {"u": user_id})
    return written


# 7a: FENCE. Wrong impl: set_own_locale without its live-membership gate (UPDATE users WHERE id =
# p_user): any business could set anyone's language.
def test_set_own_locale_cannot_reach_someone_outside_this_business(
    people: People, migrate_engine: Engine
) -> None:
    assert set_own_locale(people.a, people.only_b) is False
    assert locale_of(migrate_engine, people.only_b) is None


# 7b: FENCE. Wrong impl: a gate that counts any membership of the person with no tenant filter,
# which the memberships policy sign_in opens to every tenant once the caller sets app.sign_in.
def test_presetting_sign_in_does_not_widen_set_own_locale(
    people: People, migrate_engine: Engine
) -> None:
    assert set_own_locale(people.a, people.only_b, sign_in=True) is False
    assert locale_of(migrate_engine, people.only_b) is None


# 7c: FENCE. Wrong impl: the gate counting a removed membership.
def test_set_own_locale_cannot_reach_a_removed_member(
    people: People, migrate_engine: Engine
) -> None:
    end_membership(people.a, people.only_a)

    assert set_own_locale(people.a, people.only_a) is False
    assert locale_of(migrate_engine, people.only_a) is None


# Positive control for 7a-7c: a live member of the caller's business is written, even when the value
# does not change (a repeat PUT is not a 401).
def test_set_own_locale_writes_a_live_member_of_this_business(
    people: People, migrate_engine: Engine
) -> None:
    assert set_own_locale(people.a, people.only_a) is True
    assert set_own_locale(people.a, people.only_a) is True
    assert locale_of(migrate_engine, people.only_a) == "pt"


# 8: FENCE. Wrong impl: GRANT UPDATE (locale) ON users. Asserted on the privilege, and on the
# statement raising, because a granted UPDATE with no UPDATE policy changes 0 rows quietly.
@pytest.mark.parametrize("role", ["ziftbook_app", "ziftbook_worker"])
def test_no_runtime_role_can_update_a_users_column(migrate_engine: Engine, role: str) -> None:
    with migrate_engine.connect() as conn:
        columns = conn.scalars(
            text("""
            SELECT attname FROM pg_attribute
            WHERE attrelid = 'users'::regclass AND attnum > 0 AND NOT attisdropped
            """)
        ).all()
        granted = [
            c
            for c in columns
            if conn.scalar(
                text("SELECT has_column_privilege(:r, 'users', :c, 'UPDATE')"),
                {"r": role, "c": c},
            )
        ]
    assert "locale" in columns and granted == []


def test_the_app_role_cannot_update_its_own_members_language(people: People) -> None:
    with pytest.raises(ProgrammingError) as error, tenant_context(people.a) as session:
        session.execute(text("UPDATE users SET locale = 'pt' WHERE id = :u"), {"u": people.only_a})
    assert isinstance(error.value.orig, InsufficientPrivilege)


# 11: FENCE. Wrong impl: the route on AnySession, open to a person who has not given a name.
def test_a_nameless_person_cannot_set_a_language(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_user_name(migrate_engine, people.both, None)

    response = signed_in(app, people.a, people.both).put(
        "/api/session/locale", json={"locale": "pt"}
    )

    assert (response.status_code, response.json()) == (403, {"code": "name_required"})
    assert locale_of(migrate_engine, people.both) == "nl"


# A worker (only_a) sets their own language too: it is a person's setting, not an owner's.
def test_a_worker_sets_their_own_language(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    response = signed_in(app, people.a, people.only_a).put(
        "/api/session/locale", json={"locale": "nl"}
    )

    assert response.status_code == 200
    assert locale_of(migrate_engine, people.only_a) == "nl"
