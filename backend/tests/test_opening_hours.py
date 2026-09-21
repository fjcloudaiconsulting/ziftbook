"""GET/PUT /api/opening-hours: when a business is open, and narrows every member's working hours
(ZIF-105)."""

import threading
from typing import Any

import psycopg.errors as pg_errors
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schedule
from app.auth import SignedIn
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, events, member_id, new_client, signed_in, wait_until_blocked
from tests.test_working_hours import INVALID_BODIES, hours_path, seed, stored

open_path = "/api/opening-hours"

INSERT_OPENING = """
INSERT INTO opening_hours (tenant_id, weekday, starts_at, ends_at)
VALUES (:t, :w, :s, :e)
"""


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def seed_opening(tenant_id: Any, rows: list[tuple[int, str, str]]) -> None:
    """Insert opening hours directly, as the app role, in tenant_context."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text(INSERT_OPENING),
            [{"t": tenant_id, "w": w, "s": s, "e": e} for w, s, e in rows],
        )


def stored_opening(tenant_id: Any) -> list[tuple[int, str, str]]:
    """A business's opening hours, read the same way the app reads them."""
    with tenant_context(tenant_id) as session:
        rows = (
            session.execute(
                text(
                    "SELECT weekday, starts_at, ends_at FROM opening_hours "
                    "ORDER BY weekday, starts_at"
                )
            )
            .tuples()
            .all()
        )
    return [(w, f"{s:%H:%M}", f"{e:%H:%M}") for w, s, e in rows]


# G1: seeded out of order, GET and PUT both come back sorted Monday first.
def test_the_week_is_returned_sorted_monday_first(people: People, app: FastAPI) -> None:
    seed_opening(people.a, [(2, "10:00", "11:00"), (1, "13:00", "14:00"), (1, "09:00", "10:00")])
    owner = signed_in(app, people.a, people.both)

    response = owner.get(open_path)

    assert response.json() == [
        {"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"},
        {"weekday": 1, "starts_at": "13:00", "ends_at": "14:00"},
        {"weekday": 2, "starts_at": "10:00", "ends_at": "11:00"},
    ]

    replaced = owner.put(
        open_path,
        json=[
            {"weekday": 2, "starts_at": "08:00", "ends_at": "09:00"},
            {"weekday": 1, "starts_at": "07:00", "ends_at": "08:00"},
        ],
    )
    assert replaced.json() == [
        {"weekday": 1, "starts_at": "07:00", "ends_at": "08:00"},
        {"weekday": 2, "starts_at": "08:00", "ends_at": "09:00"},
    ]
    assert replaced.headers["cache-control"] == "no-store"


# G2: a subset of test_working_hours.py's INVALID_BODIES, pinning that Shift, HH_MM and MAX_SHIFTS
# are reused, not re-declared.
_OPENING_INVALID_IDS = {
    "weekday-0",
    "weekday-8",
    "weekday-string",
    "starts-no-leading-zero",
    "starts-24",
    "starts-arabic-digits",
    "extra-key",
    "object-body",
    "too-many-rows",
}
OPENING_INVALID_BODIES = [p for p in INVALID_BODIES if p.id in _OPENING_INVALID_IDS]
assert len(OPENING_INVALID_BODIES) == len(_OPENING_INVALID_IDS)


@pytest.mark.parametrize("body", OPENING_INVALID_BODIES)
def test_a_malformed_body_is_refused(people: People, app: FastAPI, body: Any) -> None:
    seed_opening(people.a, [(4, "08:00", "09:00")])
    owner = signed_in(app, people.a, people.both)

    response = owner.put(open_path, json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored_opening(people.a) == [(4, "08:00", "09:00")]


# G3: each business holds its own week; neither sees the other's.
def test_each_business_has_its_own_opening_hours(people: People) -> None:
    seed_opening(people.a, [(1, "09:00", "10:00")])
    seed_opening(people.b, [(2, "11:00", "12:00")])

    assert stored_opening(people.a) == [(1, "09:00", "10:00")]
    assert stored_opening(people.b) == [(2, "11:00", "12:00")]


# G4: no member_id column, the right column types and unique indexes, and only the privileges a
# wholesale DELETE + INSERT needs. FENCE for the REVOKE: drop it and UPDATE comes back.
def test_the_schema_has_no_member_id_and_the_right_types(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        columns = set(
            conn.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'opening_hours'"
                )
            )
        )
        types = dict(
            conn.execute(
                text("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = 'opening_hours'
                  AND column_name IN ('weekday', 'starts_at', 'ends_at')
                """)
            )
            .tuples()
            .all()
        )
        unique_indexes = sorted(
            conn.execute(
                text("""
                SELECT ic.relname FROM pg_index i
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_class tc ON tc.oid = i.indrelid
                WHERE tc.relname = 'opening_hours' AND i.indisunique
                """)
            ).scalars()
        )
        granted = {
            privilege: conn.scalar(
                text("SELECT has_table_privilege('ziftbook_app', 'opening_hours', :priv)"),
                {"priv": privilege},
            )
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
    assert granted == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": True}
    assert columns == {"id", "tenant_id", "weekday", "starts_at", "ends_at"}
    assert types == {
        "weekday": "smallint",
        "starts_at": "time without time zone",
        "ends_at": "time without time zone",
    }
    assert unique_indexes == ["pk_opening_hours", "uq_opening_hours_tenant_id_id"]


# G5: the database's own checks, direct inserts, as the app role.
@pytest.mark.parametrize(
    ("weekday", "starts", "ends", "constraint"),
    [
        (0, "09:00", "10:00", "ck_opening_hours_iso_weekday"),
        (8, "09:00", "10:00", "ck_opening_hours_iso_weekday"),
        (1, "10:00", "09:00", "ck_opening_hours_ends_after_start"),
    ],
)
def test_direct_inserts_hit_the_database_checks(
    people: People, weekday: int, starts: str, ends: str, constraint: str
) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        with tenant_context(people.a) as session:
            session.execute(
                text(INSERT_OPENING), {"t": people.a, "w": weekday, "s": starts, "e": ends}
            )

    assert isinstance(exc_info.value.orig, pg_errors.CheckViolation)
    assert exc_info.value.orig.diag.constraint_name == constraint


# G6: the contract names both operations, and Error still requires only "code".
def test_the_contract_names_both_opening_hours_operations() -> None:
    doc = create_app().openapi()

    paths = doc["paths"]["/api/opening-hours"]
    assert paths["get"]["operationId"] == "opening-hours-read"
    assert paths["put"]["operationId"] == "opening-hours-replace"
    assert doc["components"]["schemas"]["Error"]["required"] == ["code"]


# G9: the empty-week refusal is the OPENING week's alone. A working week may still be cleared - its
# emptiness means "this member works no hours", not "never configured".
def test_a_worker_may_still_clear_their_week_while_opening_hours_exist(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    seed_opening(people.a, [(1, "09:00", "12:00")])
    seed(people.a, people.only_a, [(1, "09:00", "10:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(hours_path(only_a_member), json=[])

    assert response.status_code == 200
    assert stored(people.a, people.only_a) == []
    assert len(events(migrate_engine, tenant_id=people.a, action="working_hours_changed")) == 1


# F5: two touching opening rows are joined before the narrowing check, so a shift spanning the
# join fits.
def test_a_shift_spanning_two_touching_opening_rows_is_accepted(
    people: People, app: FastAPI
) -> None:
    seed_opening(people.a, [(1, "09:00", "12:00"), (1, "12:00", "15:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(
        hours_path(only_a_member),
        json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "15:00"}],
    )

    assert response.status_code == 200
    assert stored(people.a, people.only_a) == [(1, "09:00", "15:00")]


# F8: the empty week is refused, not stored - row count is the table's only sentinel.
def test_an_empty_week_is_refused_and_keeps_the_stored_week(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    saved = owner.put(open_path, json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}])
    assert saved.status_code == 200

    response = owner.put(open_path, json=[])

    assert (response.status_code, response.json()) == (422, {"code": "opening_hours_required"})
    assert stored_opening(people.a) == [(1, "09:00", "12:00")]
    assert len(events(migrate_engine, tenant_id=people.a, action="opening_hours_changed")) == 1


# F9: with the Python overlap check patched out, the exclusion constraint still answers 422 keyed
# on OUR OWN constraint name, not working_hours'.
def test_the_opening_overlap_mapping_only_matches_our_own_constraint(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_opening(people.a, [(3, "09:00", "12:00")])
    monkeypatch.setattr(schedule, "overlapping", lambda rows: False)
    owner = signed_in(app, people.a, people.both)

    response = owner.put(
        open_path,
        json=[
            {"weekday": 3, "starts_at": "09:00", "ends_at": "12:00"},
            {"weekday": 3, "starts_at": "11:00", "ends_at": "13:00"},
        ],
    )

    assert (response.status_code, response.json()) == (422, {"code": "overlapping_hours"})
    assert stored_opening(people.a) == [(3, "09:00", "12:00")]


# F10: the exclusion constraint really exists. Touching rows commit; the same times for another
# business commit too.
def test_direct_overlapping_opening_inserts_hit_the_exclusion_constraint(people: People) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        with tenant_context(people.a) as session:
            session.execute(
                text(INSERT_OPENING), {"t": people.a, "w": 1, "s": "09:00", "e": "12:00"}
            )
            session.execute(
                text(INSERT_OPENING), {"t": people.a, "w": 1, "s": "11:00", "e": "13:00"}
            )

    assert isinstance(exc_info.value.orig, pg_errors.ExclusionViolation)
    assert exc_info.value.orig.diag.constraint_name == "ex_opening_hours_overlap"

    with tenant_context(people.a) as session:
        session.execute(text(INSERT_OPENING), {"t": people.a, "w": 1, "s": "09:00", "e": "12:00"})
        session.execute(text(INSERT_OPENING), {"t": people.a, "w": 1, "s": "12:00", "e": "15:00"})
    assert stored_opening(people.a) == [(1, "09:00", "12:00"), (1, "12:00", "15:00")]

    with tenant_context(people.b) as session:
        session.execute(text(INSERT_OPENING), {"t": people.b, "w": 1, "s": "09:00", "e": "12:00"})
        session.execute(text(INSERT_OPENING), {"t": people.b, "w": 1, "s": "12:00", "e": "15:00"})
    assert stored_opening(people.b) == [(1, "09:00", "12:00"), (1, "12:00", "15:00")]


# F11: a working-hours shift outside the envelope is refused naming the earliest offending day.
def test_a_working_hour_outside_the_envelope_is_refused_naming_the_day(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    seed_opening(people.a, [(1, "09:00", "17:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    monday = owner.put(
        hours_path(only_a_member), json=[{"weekday": 1, "starts_at": "08:00", "ends_at": "12:00"}]
    )
    assert (monday.status_code, monday.json()) == (
        422,
        {"code": "outside_opening_hours", "weekday": 1},
    )

    sunday = owner.put(
        hours_path(only_a_member), json=[{"weekday": 7, "starts_at": "09:00", "ends_at": "17:00"}]
    )
    assert (sunday.status_code, sunday.json()) == (
        422,
        {"code": "outside_opening_hours", "weekday": 7},
    )

    assert stored(people.a, people.only_a) == []
    assert events(migrate_engine, tenant_id=people.a, action="working_hours_changed") == []


# F12: an error naming no weekday carries no weekday key at all - not even a null one.
def test_an_error_that_names_no_weekday_carries_no_weekday_key(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)

    bad_range = owner.put(
        open_path, json=[{"weekday": 3, "starts_at": "17:00", "ends_at": "09:00"}]
    )
    assert bad_range.json() == {"code": "end_not_after_start"}

    worker = signed_in(app, people.a, people.only_a)
    refused = worker.put(open_path, json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"}])
    assert refused.json() == {"code": "owner_only"}


# F13: authorise first, then do the work. A worker forbidden to edit this week gets owner_only, not
# the outside_opening_hours their body would also have earned.
def test_the_envelope_is_checked_only_after_the_403(people: People, app: FastAPI) -> None:
    seed_opening(people.a, [(1, "09:00", "12:00")])
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)

    response = worker.put(
        hours_path(only_a_member), json=[{"weekday": 1, "starts_at": "08:00", "ends_at": "20:00"}]
    )

    assert response.status_code == 403
    assert response.json() == {"code": "owner_only"}


# F14: narrowing the envelope never touches working_hours; the next save of the unchanged week is
# the 422 that names it (the acceptance criterion, not a bug).
def test_narrowing_the_envelope_leaves_working_hours_untouched(
    people: People, app: FastAPI
) -> None:
    seed_opening(people.a, [(1, "09:00", "17:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    assert (
        owner.put(
            hours_path(only_a_member),
            json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "17:00"}],
        ).status_code
        == 200
    )

    narrowed = owner.put(open_path, json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}])
    assert narrowed.status_code == 200
    assert stored(people.a, people.only_a) == [(1, "09:00", "17:00")]

    resave = owner.put(
        hours_path(only_a_member),
        json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "17:00"}],
    )
    assert (resave.status_code, resave.json()) == (
        422,
        {"code": "outside_opening_hours", "weekday": 1},
    )


# F15: recorded once, no details, no target - the exact shape ZIF-46 pins for working_hours_changed.
def test_a_change_to_the_week_is_recorded_once_with_no_details(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = owner.put(open_path, json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}])

    assert response.status_code == 200
    recorded = events(migrate_engine, tenant_id=people.a, action="opening_hours_changed")
    assert len(recorded) == 1
    assert recorded[0]["details"] is None
    assert recorded[0]["target"] is None
    assert recorded[0]["actor_user_id"] == people.both


# F16: only a save that actually changes the week is recorded.
def test_an_unchanged_week_records_nothing(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    rows = [
        {"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"},
        {"weekday": 2, "starts_at": "13:00", "ends_at": "17:00"},
    ]

    assert owner.put(open_path, json=rows).status_code == 200
    assert len(events(migrate_engine, tenant_id=people.a, action="opening_hours_changed")) == 1

    reordered = list(reversed(rows))
    assert owner.put(open_path, json=reordered).status_code == 200
    assert len(events(migrate_engine, tenant_id=people.a, action="opening_hours_changed")) == 1

    different = [{"weekday": 3, "starts_at": "08:00", "ends_at": "09:00"}]
    assert owner.put(open_path, json=different).status_code == 200
    assert len(events(migrate_engine, tenant_id=people.a, action="opening_hours_changed")) == 2


# F17: any member reads; only an owner replaces; anonymous is 401; a non-JSON write is 415.
def test_a_worker_reads_the_envelope_but_only_an_owner_replaces_it(
    people: People, app: FastAPI
) -> None:
    seed_opening(people.a, [(1, "09:00", "12:00")])
    worker = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)

    read = worker.get(open_path)
    assert read.status_code == 200
    assert read.json() == [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]
    assert read.headers["cache-control"] == "no-store"

    denied = worker.put(open_path, json=[{"weekday": 2, "starts_at": "09:00", "ends_at": "10:00"}])
    assert (denied.status_code, denied.json()) == (403, {"code": "owner_only"})
    assert stored_opening(people.a) == [(1, "09:00", "12:00")]

    anon = new_client(app)
    assert anon.get(open_path).status_code == 401

    plain = owner.put(open_path, content="x", headers={"content-type": "text/plain"})
    assert plain.status_code == 415
    assert stored_opening(people.a) == [(1, "09:00", "12:00")]


# F19: a seeded opening_hours row is read back, and (with conftest's teardown edit) the fixture
# tears down cleanly instead of erroring on DELETE FROM tenants.
def test_a_saved_week_is_read_back(people: People, app: FastAPI) -> None:
    seed_opening(people.a, [(1, "09:00", "12:00")])
    owner = signed_in(app, people.a, people.both)

    response = owner.get(open_path)

    assert response.json() == [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]


def _another_owner_replaces(
    session: Session, people: People, rows: list[tuple[int, str, str]]
) -> None:
    """A second owner's replace, in a transaction the test can hold open past the INSERT.

    Held by hand only because a real request cannot be frozen between its INSERT and its commit.
    The lock is the route's own schedule.lock_week, not a copy of its SQL, so weakening that lock
    (a shared one, say) fails the test below instead of sliding past a substring match.
    """
    schedule.lock_week(SignedIn(people.both, people.a, "owner", session))
    session.execute(text("DELETE FROM opening_hours"))
    session.execute(
        text(INSERT_OPENING), [{"t": people.a, "w": w, "s": s, "e": e} for w, s, e in rows]
    )


# F20a: two owners saving at once, one of them the real PUT. With the lock this is genuine
# last-writer-wins; with it stubbed out (the positive control) the same race stores the union of
# both weeks, which is exactly the stale-hours hole the envelope exists to close.
@pytest.mark.parametrize("with_lock", [True, False])
def test_two_concurrent_saves_never_store_a_union_of_both_weeks(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    with_lock: bool,
) -> None:
    if not with_lock:
        monkeypatch.setattr(schedule, "lock_week", lambda current: None)
    seed_opening(people.a, [(1, "09:00", "12:00")])
    owner = signed_in(app, people.a, people.both)
    week_a = [(2, "08:00", "09:00")]
    week_b = [{"weekday": 3, "starts_at": "10:00", "ends_at": "11:00"}]
    statuses: list[int] = []

    def put() -> None:
        statuses.append(owner.put(open_path, json=week_b).status_code)

    with tenant_context(people.a) as held:
        _another_owner_replaces(held, people, week_a)
        thread = threading.Thread(target=put)
        thread.start()
        # With the lock, the request waits on it; without it, on the row the held session deleted.
        wait_until_blocked(app_engine, 1)
        # held commits here, letting the request through.
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert statuses == [200]
    if with_lock:
        # The request deletes and inserts only after the other owner's commit is visible, so
        # whoever commits last wins outright.
        assert stored_opening(people.a) == [(3, "10:00", "11:00")]
    else:
        # The request's DELETE woke to find the row it blocked on already gone, and its snapshot
        # predates the other week's INSERT: it deletes nothing and adds its own rows.
        assert stored_opening(people.a) == [(2, "08:00", "09:00"), (3, "10:00", "11:00")]


# F20b: the route takes OUR lock - exclusive, in our namespace - before touching the table.
def test_the_put_takes_the_tenant_lock_before_touching_the_table(
    people: People, app: FastAPI, app_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    executed: list[tuple[str, Any]] = []

    def count(conn: object, cursor: object, statement: str, parameters: Any, *args: Any) -> None:
        executed.append((statement, parameters))

    event.listen(app_engine, "before_cursor_execute", count)
    try:
        response = owner.put(
            open_path, json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]
        )
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    assert response.status_code == 200
    # startswith, not `in`: "pg_advisory_xact_lock" is also a prefix of the SHARED variant, which
    # two owners can hold at once and so would not serialise them at all.
    locks = [
        (i, p) for i, (s, p) in enumerate(executed) if s.startswith("SELECT pg_advisory_xact_lock(")
    ]
    deletes = [i for i, (s, _) in enumerate(executed) if s.startswith("DELETE FROM opening_hours")]
    assert len(locks) == 1
    assert len(deletes) == 1
    assert locks[0][0] < deletes[0]
    assert locks[0][1] == {"key": schedule.OPENING_LOCK}
