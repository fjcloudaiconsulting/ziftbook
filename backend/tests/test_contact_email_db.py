"""contact_email_requests and its four functions (ZIF-232, 0086): the address an owner typed waits
here, and only a link the worker minted makes it the business's contact email."""

import hashlib
import secrets
import threading
import uuid
from typing import Any

import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation
from sqlalchemy import Engine, Row, create_engine, make_url, text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.pool import NullPool

from app import business_settings
from app.db import SessionLocal, tenant_context
from tests.conftest import (
    ADMIN_DATABASE_URL,
    People,
    migrate_tenant_context,
    save_setting,
    saved_settings,
    wait_until_blocked,
)


def digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def change(tenant_id: uuid.UUID, email: str | None, send: bool = False) -> Row[Any]:
    with tenant_context(tenant_id) as session:
        return session.execute(
            text("SELECT * FROM change_contact_email(:e, :s)"), {"e": email, "s": send}
        ).one()


def resend(tenant_id: uuid.UUID) -> uuid.UUID | None:
    with tenant_context(tenant_id) as session:
        found: uuid.UUID | None = session.scalar(text("SELECT resend_contact_email()"))
        return found


def mint(worker_engine: Engine, tenant_id: uuid.UUID, request_id: object) -> str | None:
    """As the worker job does: the secret of a freshly minted link, None if nothing was minted."""
    secret = secrets.token_urlsafe(32)
    with worker_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        row = conn.execute(
            text("SELECT * FROM mint_contact_email_token(:id, :h)"),
            {"id": request_id, "h": digest(secret)},
        ).first()
    return secret if row else None


def confirm(tenant_id: uuid.UUID, token_hash: bytes | None) -> Row[Any] | None:
    with tenant_context(tenant_id) as session:
        return session.execute(
            text("SELECT * FROM confirm_contact_email(:h)"), {"h": token_hash}
        ).first()


def request_of(migrate_engine: Engine, tenant_id: uuid.UUID) -> Row[Any] | None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        return conn.execute(
            text(
                "SELECT id, email, token_hash, expires_at, expires_at > now() + interval '23 hours'"
                " AS a_day FROM contact_email_requests"
            )
        ).first()


def in_effect(tenant_id: uuid.UUID) -> Any:
    return saved_settings(tenant_id).get("contact_email")


def set_in_effect(migrate_engine: Engine, tenant_id: uuid.UUID, email: str) -> None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        conn.execute(
            text(
                "INSERT INTO settings (tenant_id, key, value) VALUES"
                " (current_setting('app.tenant_id')::uuid, 'contact_email',"
                " to_jsonb(CAST(:e AS text)))"
            ),
            {"e": email},
        )


def expire(migrate_engine: Engine, tenant_id: uuid.UUID) -> None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        conn.execute(
            text("UPDATE contact_email_requests SET expires_at = now() - interval '1 second'")
        )


def sent(worker_engine: Engine, tenant_id: uuid.UUID, email: str) -> tuple[uuid.UUID, str]:
    """A request for email with a link minted: its id and the link's secret."""
    row = change(tenant_id, email, send=True)
    secret = mint(worker_engine, tenant_id, row.request_id)
    assert secret is not None
    return row.request_id, secret


# F1.1 FENCE. Wrong impl: no one-row rule, or a CHECK left out.
@pytest.mark.parametrize(
    ("email", "token_hash", "error"),
    [
        pytest.param("Shop@x.nl", None, CheckViolation, id="uppercase"),
        pytest.param("a" * 250 + "@x.nl", None, CheckViolation, id="longer than 254"),
        pytest.param("shop@x.nl", b"\x00" * 31, CheckViolation, id="31-byte hash"),
    ],
)
def test_a_request_holds_a_lowercase_address_and_a_whole_hash(
    people: People, migrate_engine: Engine, email: str, token_hash: bytes | None, error: type
) -> None:
    with (
        pytest.raises(IntegrityError) as raised,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO contact_email_requests (tenant_id, email, token_hash)"
                " VALUES (current_setting('app.tenant_id')::uuid, :e, :h)"
            ),
            {"e": email, "h": token_hash},
        )
    assert isinstance(raised.value.orig, error)


def test_a_business_has_one_request_at_most(people: People, migrate_engine: Engine) -> None:
    insert = text(
        "INSERT INTO contact_email_requests (tenant_id, email)"
        " VALUES (current_setting('app.tenant_id')::uuid, :e)"
    )
    with (
        pytest.raises(IntegrityError) as raised,
        migrate_tenant_context(migrate_engine, people.a) as conn,
    ):
        conn.execute(insert, {"e": "one@x.nl"})
        conn.execute(insert, {"e": "two@x.nl"})
    assert isinstance(raised.value.orig, UniqueViolation)


APP_PRIVILEGES = text("""
SELECT a.attname, p.privilege FROM pg_attribute a,
  unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'REFERENCES']) p(privilege)
WHERE a.attrelid = 'contact_email_requests'::regclass AND a.attnum > 0 AND NOT a.attisdropped
  AND has_column_privilege('ziftbook_app', a.attrelid, a.attnum, p.privilege)
UNION ALL
SELECT '*', p.privilege FROM unnest(ARRAY['DELETE', 'TRUNCATE', 'TRIGGER']) p(privilege)
WHERE has_table_privilege('ziftbook_app', 'contact_email_requests', p.privilege)
ORDER BY 1, 2
""")


# F1.2 FENCE. Wrong impl: 0001's default DML left on the app role, or SELECT on token_hash.
def test_the_app_role_reads_all_but_the_hash_and_writes_nothing(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        held = [tuple(row) for row in conn.execute(APP_PRIVILEGES)]
    assert held == [
        ("email", "SELECT"),
        ("expires_at", "SELECT"),
        ("id", "SELECT"),
        ("tenant_id", "SELECT"),
    ]


# F1.4 FENCE (ZB-08). Wrong impl: the mint granted to the app role.
def test_the_app_role_cannot_mint(people: People) -> None:
    row = change(people.a, "shop@x.nl", send=True)
    with pytest.raises(ProgrammingError) as raised, tenant_context(people.a) as session:
        session.execute(
            text("SELECT * FROM mint_contact_email_token(:id, :h)"),
            {"id": row.request_id, "h": digest("guess")},
        )
    assert isinstance(raised.value.orig, InsufficientPrivilege)


# F1.6 FENCE. Wrong impl: a lookup without the expiry or the tenant, or IS NOT DISTINCT FROM.
def test_a_dead_link_confirms_nothing(
    people: People, migrate_engine: Engine, worker_engine: Engine
) -> None:
    _, other = sent(worker_engine, people.b, "other@x.nl")
    never_minted = change(people.a, "never@x.nl")
    assert never_minted.outcome == "requested"

    assert confirm(people.a, None) is None  # the never-minted request's hash is NULL too
    assert confirm(people.a, digest("unknown")) is None
    assert confirm(people.a, digest(other)) is None  # another business's link, under this id
    _, lapsed = sent(worker_engine, people.a, "lapsed@x.nl")
    expire(migrate_engine, people.a)
    assert confirm(people.a, digest(lapsed)) is None

    assert (in_effect(people.a), in_effect(people.b)) == (None, None)
    assert request_of(migrate_engine, people.a) is not None


# F1.7 FENCE. Wrong impl: a confirm that leaves the request (replayable) or writes another key.
def test_a_live_link_makes_its_address_the_one_in_effect_once(
    people: People, migrate_engine: Engine, worker_engine: Engine
) -> None:
    set_in_effect(migrate_engine, people.a, "old@x.nl")
    _, secret = sent(worker_engine, people.a, "new@x.nl")

    confirmed = confirm(people.a, digest(secret))

    assert confirmed is not None and tuple(confirmed) == ("new@x.nl", "old@x.nl")
    assert saved_settings(people.a) == {"contact_email": "new@x.nl"}
    assert request_of(migrate_engine, people.a) is None
    assert confirm(people.a, digest(secret)) is None


# F1.8 FENCE. Wrong impl: requesting again on every call (a save of other keys kills a sent link),
# or a clear that leaves the address in effect.
def test_what_a_change_does(people: People, migrate_engine: Engine, worker_engine: Engine) -> None:
    set_in_effect(migrate_engine, people.a, "old@x.nl")

    first = change(people.a, "New@X.nl")
    assert (first.outcome, first.previous) == ("requested", "old@x.nl")
    waiting = request_of(migrate_engine, people.a)
    assert waiting is not None
    assert (waiting.email, waiting.token_hash, waiting.expires_at) == ("new@x.nl", None, None)

    sent_id, secret = sent(worker_engine, people.a, "other@x.nl")
    assert sent_id != first.request_id
    assert request_of(migrate_engine, people.a).a_day  # type: ignore[union-attr]

    again = change(people.a, "other@x.nl", send=True)  # the waiting address, its link live
    assert (again.outcome, again.request_id) == ("unchanged", sent_id)
    replaced = change(people.a, "third@x.nl")
    assert replaced.outcome == "requested" and replaced.request_id != sent_id
    assert confirm(people.a, digest(secret)) is None  # the old link died with the change

    kept = change(people.a, "old@x.nl")
    assert (kept.outcome, request_of(migrate_engine, people.a)) == ("kept", None)
    assert in_effect(people.a) == "old@x.nl"

    change(people.a, "fourth@x.nl")
    cleared = change(people.a, None)
    assert (cleared.outcome, cleared.previous) == ("cleared", "old@x.nl")
    assert (in_effect(people.a), request_of(migrate_engine, people.a)) == (None, None)


# NIT 6. Wrong impl: a PUT of the waiting address never sends a link when none is live (a migrated
# address could then only be sent by the resend button).
def test_sending_the_waiting_address_with_no_live_link_gives_a_new_link(
    people: People, migrate_engine: Engine, worker_engine: Engine
) -> None:
    unsent = change(people.a, "shop@x.nl")

    first = change(people.a, "shop@x.nl", send=True)
    assert first.outcome == "requested" and first.request_id != unsent.request_id
    _, secret = sent(worker_engine, people.a, "shop@x.nl")  # live again: unchanged, minted
    expire(migrate_engine, people.a)
    lapsed = change(people.a, "shop@x.nl", send=True)

    assert lapsed.outcome == "requested"
    assert request_of(migrate_engine, people.a).a_day  # type: ignore[union-attr]
    assert confirm(people.a, digest(secret)) is None


# F1.8, resend. Wrong impl: a resend that keeps the id or the hash.
def test_a_resend_kills_the_old_link(
    people: People, migrate_engine: Engine, worker_engine: Engine
) -> None:
    assert resend(people.a) is None
    request_id, secret = sent(worker_engine, people.a, "shop@x.nl")

    fresh = resend(people.a)

    assert fresh is not None and fresh != request_id
    assert confirm(people.a, digest(secret)) is None
    assert mint(worker_engine, people.a, request_id) is None  # a job for the old id sends nothing
    new_secret = mint(worker_engine, people.a, fresh)
    assert new_secret is not None and confirm(people.a, digest(new_secret)) is not None


# F1.10 FENCE. Wrong impl: a mint that ignores expires_at, or keeps several hashes.
def test_the_mint_gives_a_link_only_to_a_request_due_one(
    people: People, migrate_engine: Engine, worker_engine: Engine
) -> None:
    assert mint(worker_engine, people.a, uuid.uuid4()) is None
    unsent = change(people.a, "shop@x.nl")
    assert mint(worker_engine, people.a, unsent.request_id) is None  # never queued
    queued = change(people.a, "shop@x.nl", send=True)
    first = mint(worker_engine, people.a, queued.request_id)
    second = mint(worker_engine, people.a, queued.request_id)
    assert first is not None and second is not None
    assert request_of(migrate_engine, people.a).a_day  # type: ignore[union-attr]
    assert mint(worker_engine, people.b, queued.request_id) is None  # under another business
    assert confirm(people.a, digest(first)) is None
    assert confirm(people.a, digest(second)) is not None
    late = change(people.a, "late@x.nl", send=True)
    expire(migrate_engine, people.a)  # queued a day ago and never minted
    assert mint(worker_engine, people.a, late.request_id) is None


def test_the_mint_answers_the_address_and_the_business(
    people: People, worker_engine: Engine
) -> None:
    queued = change(people.a, "shop@x.nl", send=True)
    with worker_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        row = conn.execute(
            text("SELECT * FROM mint_contact_email_token(:id, :h)"),
            {"id": queued.request_id, "h": digest("x")},
        ).one()
    assert tuple(row) == ("shop@x.nl", "a")


# F1.9 FENCE. Wrong impl: a change that locks the setting before the request (a deadlock), or one
# that reads the request without a lock (the confirmed address is lost or the request is).
def test_a_confirm_and_a_change_take_turns(
    people: People, migrate_engine: Engine, worker_engine: Engine, app_engine: Engine
) -> None:
    _, secret = sent(worker_engine, people.a, "first@x.nl")
    results: list[object] = []

    def change_meanwhile() -> None:
        try:
            results.append(change(people.a, "second@x.nl").outcome)
        except Exception as error:  # a deadlock lands here
            results.append(error)

    with SessionLocal(info={"tenant_id": people.a}) as session, session.begin():
        confirmed = session.execute(
            text("SELECT * FROM confirm_contact_email(:h)"), {"h": digest(secret)}
        ).one()
        thread = threading.Thread(target=change_meanwhile)
        thread.start()
        wait_until_blocked(app_engine, 1)
    thread.join()

    assert (confirmed.confirmed, results) == ("first@x.nl", ["requested"])
    assert in_effect(people.a) == "first@x.nl"
    assert request_of(migrate_engine, people.a).email == "second@x.nl"  # type: ignore[union-attr]


def test_a_change_and_a_confirm_take_turns(
    people: People, migrate_engine: Engine, worker_engine: Engine, app_engine: Engine
) -> None:
    _, secret = sent(worker_engine, people.a, "first@x.nl")
    results: list[object] = []

    def confirm_meanwhile() -> None:
        try:
            results.append(confirm(people.a, digest(secret)))
        except Exception as error:
            results.append(error)

    with SessionLocal(info={"tenant_id": people.a}) as session, session.begin():
        session.execute(text("SELECT * FROM change_contact_email('second@x.nl', false)")).one()
        thread = threading.Thread(target=confirm_meanwhile)
        thread.start()
        wait_until_blocked(app_engine, 1)
    thread.join()

    assert results == [None]
    assert in_effect(people.a) is None
    assert request_of(migrate_engine, people.a).email == "second@x.nl"  # type: ignore[union-attr]


def test_a_definer_sees_only_the_callers_business(people: People, migrate_engine: Engine) -> None:
    change(people.b, "b@x.nl")
    assert change(people.a, None).outcome == "cleared"
    assert resend(people.a) is None
    assert request_of(migrate_engine, people.b) is not None


def test_without_a_business_the_functions_refuse(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(text("SELECT * FROM change_contact_email('a@x.nl', false)"))


# 0087. The writes an older release, a bug or a compromised API could try.
WRITES = {
    "save": "INSERT INTO settings (tenant_id, key, value) VALUES"
    " (current_setting('app.tenant_id')::uuid, 'contact_email', '\"a@x.nl\"')",
    "save null": "INSERT INTO settings (tenant_id, key, value) VALUES"
    " (current_setting('app.tenant_id')::uuid, 'contact_email', 'null')",
    "update": "UPDATE settings SET value = '\"b@x.nl\"' WHERE key = 'contact_email'",
    "rename": "UPDATE settings SET key = 'contact_email' WHERE key = 'language'",
}


def refused(engine: Engine, tenant_id: uuid.UUID, statement: str, role: str | None = None) -> bool:
    try:
        with engine.begin() as conn:
            if role:
                conn.execute(text(f"SET LOCAL ROLE {role}"))
            conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )
            conn.execute(text(statement))
    except ProgrammingError as error:
        assert isinstance(error.orig, InsufficientPrivilege)
        return True
    return False


# F2.1 FENCE. Wrong impl: a deny-list on ziftbook_app (the worker and every other role write it),
# a check on OLD.key only (a rename passes), or a trigger on INSERT only.
@pytest.mark.parametrize("write", WRITES)
def test_no_runtime_role_writes_the_contact_email_setting(
    people: People, migrate_engine: Engine, app_engine: Engine, worker_engine: Engine, write: str
) -> None:
    set_in_effect(migrate_engine, people.a, "old@x.nl")
    save_setting(people.a, "language", "nl")
    if write != "update":  # an insert, or a rename onto a key the business has not saved
        with migrate_tenant_context(migrate_engine, people.a) as conn:
            conn.execute(text("DELETE FROM settings WHERE key = 'contact_email'"))

    assert refused(app_engine, people.a, WRITES[write])
    assert refused(worker_engine, people.a, WRITES[write])
    admin = create_engine(
        make_url(ADMIN_DATABASE_URL).set(database=migrate_engine.url.database), poolclass=NullPool
    )
    try:
        assert refused(admin, people.a, WRITES[write])
        assert not refused(admin, people.a, WRITES[write], role="ziftbook_migrate")
    finally:
        admin.dispose()


# S7: an older release's PUT of the key is refused, and saves nothing else with it (one
# transaction). It can only refuse: it never makes an unconfirmed address the one in effect.
def test_an_older_releases_save_of_the_key_is_refused(people: People) -> None:
    with pytest.raises(ProgrammingError) as raised, tenant_context(people.a) as session:
        business_settings.save(session, "language", "nl")
        business_settings.save(session, "contact_email", "a@x.nl")
    assert isinstance(raised.value.orig, InsufficientPrivilege)
    assert saved_settings(people.a) == {}


# F2.2 FENCE. Wrong impl: a guard keyed on session_user, which refuses the confirm too.
def test_the_confirm_writes_the_setting_through_the_guard(
    people: People, worker_engine: Engine
) -> None:
    _, secret = sent(worker_engine, people.a, "shop@x.nl")
    assert confirm(people.a, digest(secret)) is not None
    assert in_effect(people.a) == "shop@x.nl"


def test_a_runtime_role_still_deletes_it(people: People, migrate_engine: Engine) -> None:
    set_in_effect(migrate_engine, people.a, "old@x.nl")
    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM settings WHERE key = 'contact_email'"))
    assert in_effect(people.a) is None
