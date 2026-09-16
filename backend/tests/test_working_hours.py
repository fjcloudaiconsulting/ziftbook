"""GET/PUT /api/members/{id}/working-hours: split shifts, saved as local times (ZIF-46)."""

import threading
import uuid
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import psycopg.errors as pg_errors
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import auth, members, schedule
from app.auth import SignedIn
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    events,
    failing,
    member_id,
    new_client,
    save_setting,
    signed_in,
    wait_until_blocked,
)

INSERT_HOURS = """
INSERT INTO working_hours (tenant_id, member_id, weekday, starts_at, ends_at)
VALUES (:t, :m, :w, :s, :e)
"""


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def hours_path(member: uuid.UUID) -> str:
    return f"/api/members/{member}/working-hours"


def seed(tenant_id: uuid.UUID, user_id: uuid.UUID, rows: list[tuple[int, str, str]]) -> None:
    """Insert working hours directly, as the app role, in tenant_context."""
    target = member_id(tenant_id, user_id)
    with tenant_context(tenant_id) as session:
        session.execute(
            text(INSERT_HOURS),
            [{"t": tenant_id, "m": target, "w": w, "s": s, "e": e} for w, s, e in rows],
        )


def stored(tenant_id: uuid.UUID, user_id: uuid.UUID) -> list[tuple[int, str, str]]:
    """A member's working hours, read the same way the app reads them."""
    target = member_id(tenant_id, user_id)
    with tenant_context(tenant_id) as session:
        rows = (
            session.execute(
                text(
                    "SELECT weekday, starts_at, ends_at FROM working_hours "
                    "WHERE member_id = :m ORDER BY weekday, starts_at"
                ),
                {"m": target},
            )
            .tuples()
            .all()
        )
    return [(w, f"{s:%H:%M}", f"{e:%H:%M}") for w, s, e in rows]


def shift(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"}
    base.update(overrides)
    return base


def valid_shifts(n: int) -> list[dict[str, Any]]:
    """n distinct, non-overlapping 30-minute shifts, spread across the week."""
    out: list[dict[str, Any]] = []
    day, minute = 1, 0
    for _ in range(n):
        start_h, start_m = divmod(minute, 60)
        end_h, end_m = divmod(minute + 30, 60)
        out.append(
            {
                "weekday": day,
                "starts_at": f"{start_h:02d}:{start_m:02d}",
                "ends_at": f"{end_h:02d}:{end_m:02d}",
            }
        )
        minute += 60
        if minute >= 24 * 60:
            minute = 0
            day += 1
    return out


# Split shifts across weekdays are saved sorted by (weekday, starts_at); everyone reads the same
# week back.
def test_split_shifts_across_weekdays_are_saved_sorted(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(
        hours_path(only_a_member),
        json=[
            {"weekday": 1, "starts_at": "13:00", "ends_at": "17:30"},
            {"weekday": 2, "starts_at": "10:00", "ends_at": "14:00"},
            {"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"},
        ],
    )

    expected = [
        {"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"},
        {"weekday": 1, "starts_at": "13:00", "ends_at": "17:30"},
        {"weekday": 2, "starts_at": "10:00", "ends_at": "14:00"},
    ]
    assert response.status_code == 200
    assert response.json() == expected
    assert response.headers["cache-control"] == "no-store"
    assert worker.get(hours_path(only_a_member)).json() == expected
    assert owner.get(hours_path(only_a_member)).json() == expected


# Rows seeded out of order still come back sorted Monday first.
def test_the_week_is_returned_sorted_even_when_seeded_out_of_order(
    people: People, app: FastAPI
) -> None:
    seed(
        people.a,
        people.only_a,
        [(2, "10:00", "11:00"), (1, "13:00", "14:00"), (1, "09:00", "10:00")],
    )
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.get(hours_path(only_a_member))

    assert response.json() == [
        {"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"},
        {"weekday": 1, "starts_at": "13:00", "ends_at": "14:00"},
        {"weekday": 2, "starts_at": "10:00", "ends_at": "11:00"},
    ]


# The schema has no extra unique index, and the right column types.
def test_working_hours_schema_has_no_extra_unique_index(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        unique_indexes = sorted(
            conn.execute(
                text("""
                SELECT ic.relname FROM pg_index i
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_class tc ON tc.oid = i.indrelid
                WHERE tc.relname = 'working_hours' AND i.indisunique
                """)
            ).scalars()
        )
        types = dict(
            conn.execute(
                text("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = 'working_hours'
                  AND column_name IN ('weekday', 'starts_at', 'ends_at')
                """)
            )
            .tuples()
            .all()
        )
    assert unique_indexes == ["pk_working_hours", "uq_working_hours_tenant_id_id"]
    assert types == {
        "weekday": "smallint",
        "starts_at": "time without time zone",
        "ends_at": "time without time zone",
    }


# Touching shifts don't overlap, checked with both the real `overlapping` and one monkeypatched to
# always say False.
@pytest.mark.parametrize("patch_overlapping", [False, True])
def test_touching_shifts_do_not_overlap(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch, patch_overlapping: bool
) -> None:
    if patch_overlapping:
        monkeypatch.setattr(schedule, "overlapping", lambda rows: False)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(
        hours_path(only_a_member),
        json=[
            {"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"},
            {"weekday": 1, "starts_at": "12:00", "ends_at": "15:00"},
        ],
    )

    assert response.status_code == 200
    assert stored(people.a, people.only_a) == [(1, "09:00", "12:00"), (1, "12:00", "15:00")]


# Overlapping shifts on one weekday are refused, nothing is stored, nothing recorded.
@pytest.mark.parametrize(
    "shifts",
    [
        [
            {"weekday": 3, "starts_at": "09:00", "ends_at": "12:00"},
            {"weekday": 3, "starts_at": "11:00", "ends_at": "13:00"},
        ],
        [
            {"weekday": 3, "starts_at": "09:00", "ends_at": "12:00"},
            {"weekday": 3, "starts_at": "09:00", "ends_at": "12:00"},
        ],
        [
            {"weekday": 3, "starts_at": "09:00", "ends_at": "17:00"},
            {"weekday": 3, "starts_at": "10:00", "ends_at": "11:00"},
        ],
        [
            {"weekday": 3, "starts_at": "11:00", "ends_at": "12:30"},
            {"weekday": 3, "starts_at": "13:00", "ends_at": "14:00"},
            {"weekday": 3, "starts_at": "09:00", "ends_at": "12:00"},
        ],
    ],
    ids=["adjacent-overlap", "duplicate", "containing", "unsorted-non-neighbour"],
)
def test_overlapping_shifts_on_one_weekday_are_refused(
    people: People, app: FastAPI, migrate_engine: Engine, shifts: list[dict[str, Any]]
) -> None:
    seed(people.a, people.only_a, [(3, "08:00", "09:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(hours_path(only_a_member), json=shifts)

    assert (response.status_code, response.json()) == (422, {"code": "overlapping_hours"})
    assert stored(people.a, people.only_a) == [(3, "08:00", "09:00")]
    assert events(migrate_engine, tenant_id=people.a, action="working_hours_changed") == []


# overlapping() is a sorted-neighbour check, checked directly (pure), unsorted input included.
@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([(3, time(9), time(12)), (3, time(11), time(13))], True),
        ([(3, time(9), time(12)), (3, time(9), time(12))], True),
        ([(3, time(9), time(17)), (3, time(10), time(11))], True),
        ([(3, time(11), time(12, 30)), (3, time(13), time(14)), (3, time(9), time(12))], True),
        # Same weekday, out of order and not neighbours before sorting.
        ([(1, time(9), time(12)), (2, time(9), time(12)), (1, time(11), time(13))], True),
        ([(1, time(13), time(14)), (1, time(9), time(12))], False),
        # The same times, but on two different weekdays: never an overlap.
        ([(1, time(9), time(12)), (2, time(9), time(12))], False),
    ],
)
def test_overlapping_is_a_pure_sorted_neighbour_check(
    rows: list[tuple[int, time, time]], expected: bool
) -> None:
    assert schedule.overlapping(rows) is expected


# With the Python check patched out, the exclusion constraint still answers 422 overlapping_hours;
# an unrelated exclusion violation stays a 500.
def test_the_overlap_mapping_only_matches_our_own_constraint(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(people.a, people.only_a, [(3, "09:00", "12:00")])
    monkeypatch.setattr(schedule, "overlapping", lambda rows: False)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(
        hours_path(only_a_member),
        json=[
            {"weekday": 3, "starts_at": "09:00", "ends_at": "12:00"},
            {"weekday": 3, "starts_at": "11:00", "ends_at": "13:00"},
        ],
    )

    assert (response.status_code, response.json()) == (422, {"code": "overlapping_hours"})
    assert stored(people.a, people.only_a) == [(3, "09:00", "12:00")]


def test_the_overlap_mapping_does_not_catch_an_unrelated_exclusion_violation(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            CREATE FUNCTION zz_refuse_hours() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'refused for testing'
                USING ERRCODE = '23P01', CONSTRAINT = 'other_rule';
            END $$;
            CREATE TRIGGER zz_refuse_hours BEFORE INSERT ON working_hours
            FOR EACH ROW EXECUTE FUNCTION zz_refuse_hours()
            """)
        )
    try:
        owner = signed_in(app, people.a, people.both)
        only_a_member = member_id(people.a, people.only_a)

        response = owner.put(
            hours_path(only_a_member),
            json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"}],
        )

        assert (response.status_code, response.json()) == (500, {"code": "internal"})
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DROP TRIGGER zz_refuse_hours ON working_hours"))
            conn.execute(text("DROP FUNCTION zz_refuse_hours()"))


# A shift that doesn't end after it starts is refused with its own code.
@pytest.mark.parametrize(
    ("starts", "ends"), [("22:00", "02:00"), ("09:00", "09:00"), ("17:00", "09:00")]
)
def test_a_shift_must_end_after_it_starts(
    people: People, app: FastAPI, starts: str, ends: str
) -> None:
    seed(people.a, people.only_a, [(3, "08:00", "09:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(
        hours_path(only_a_member),
        json=[{"weekday": 3, "starts_at": starts, "ends_at": ends}],
    )

    assert (response.status_code, response.json()) == (422, {"code": "end_not_after_start"})
    assert stored(people.a, people.only_a) == [(3, "08:00", "09:00")]


# A malformed body, at any level, is refused with invalid_request and changes nothing.
INVALID_BODIES = [
    pytest.param([shift(weekday=0)], id="weekday-0"),
    pytest.param([shift(weekday=8)], id="weekday-8"),
    pytest.param([shift(weekday="1")], id="weekday-string"),
    pytest.param([shift(weekday=True)], id="weekday-bool"),
    pytest.param([shift(weekday=1.5)], id="weekday-float"),
    pytest.param([shift(starts_at="9:00")], id="starts-no-leading-zero"),
    pytest.param([shift(starts_at="09:00:00")], id="starts-seconds"),
    pytest.param([shift(starts_at="09:00:00.5")], id="starts-fractional"),
    pytest.param([shift(starts_at="24:00")], id="starts-24"),
    pytest.param([shift(starts_at="09:60")], id="starts-60"),
    pytest.param([shift(starts_at="٠٩:٠٠")], id="starts-arabic-digits"),
    pytest.param([shift(starts_at="09:00\n")], id="starts-trailing-newline"),
    pytest.param([shift(starts_at=900)], id="starts-int"),
    pytest.param([shift(starts_at=None)], id="starts-null"),
    pytest.param([{**shift(), "id": "extra"}], id="extra-key"),
    pytest.param([{"weekday": 1, "starts_at": "09:00"}], id="missing-ends-at"),
    pytest.param({}, id="object-body"),
    pytest.param(valid_shifts(51), id="too-many-rows"),
]


@pytest.mark.parametrize("body", INVALID_BODIES)
def test_a_malformed_body_is_refused(people: People, app: FastAPI, body: Any) -> None:
    seed(people.a, people.only_a, [(4, "08:00", "09:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(hours_path(only_a_member), json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a, people.only_a) == [(4, "08:00", "09:00")]


def test_a_non_uuid_path_is_refused(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)

    response = owner.put(
        "/api/members/not-a-uuid/working-hours",
        json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"}],
    )

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


# 50 non-overlapping rows are accepted and returned sorted; pins the boundary of 51.
def test_fifty_rows_is_the_accepted_boundary(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    rows = valid_shifts(50)

    response = owner.put(hours_path(only_a_member), json=rows)

    assert response.status_code == 200
    assert response.json() == sorted(rows, key=lambda r: (r["weekday"], r["starts_at"]))


# A worker may edit their own hours only once the business allows it.
def test_a_worker_may_edit_their_own_hours_only_once_the_setting_allows_it(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)
    body = [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]

    refused = worker.put(hours_path(only_a_member), json=body)
    assert (refused.status_code, refused.json()) == (403, {"code": "owner_only"})
    assert stored(people.a, people.only_a) == []

    save_setting(people.a, "workers_edit_own_hours", True)
    allowed = worker.put(hours_path(only_a_member), json=body)

    assert allowed.status_code == 200
    assert stored(people.a, people.only_a) == [(1, "09:00", "12:00")]
    recorded = events(migrate_engine, tenant_id=people.a, action="working_hours_changed")
    assert len(recorded) == 1
    assert recorded[0]["actor_user_id"] == people.only_a
    assert recorded[0]["target"] == f"user:{people.only_a}"


# The setting only ever lets a worker change their own hours, never another member's.
@pytest.mark.parametrize("hours_allowed", [False, True])
def test_a_worker_may_not_edit_another_members_hours(
    people: People, app: FastAPI, hours_allowed: bool
) -> None:
    seed(people.a, people.both, [(2, "08:00", "09:00")])
    if hours_allowed:
        save_setting(people.a, "workers_edit_own_hours", True)
    worker = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    response = worker.put(
        hours_path(both_member), json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]
    )

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert stored(people.a, people.both) == [(2, "08:00", "09:00")]


# An owner is never bound by the worker setting, for anyone's hours, their own included.
def test_an_owner_is_never_bound_by_the_worker_setting(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    other = owner.put(
        hours_path(only_a_member), json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]
    )
    own = owner.put(
        hours_path(both_member), json=[{"weekday": 2, "starts_at": "09:00", "ends_at": "12:00"}]
    )

    assert other.status_code == 200
    assert own.status_code == 200


# A member outside the business, or a random id, is 404 for both GET and PUT, whoever calls;
# nothing changes, nothing is recorded.
@pytest.mark.parametrize("caller", ["owner", "worker"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("bogus", ["other_business", "random"])
def test_a_member_outside_the_business_is_not_found(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    caller: str,
    method: str,
    bogus: str,
) -> None:
    save_setting(people.a, "workers_edit_own_hours", True)
    seed(people.b, people.only_b, [(1, "09:00", "10:00")])
    client = signed_in(app, people.a, people.both if caller == "owner" else people.only_a)
    target = member_id(people.b, people.only_b) if bogus == "other_business" else uuid.uuid7()

    if method == "GET":
        response = client.get(hours_path(target))
    else:
        response = client.put(
            hours_path(target), json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"}]
        )

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    assert stored(people.b, people.only_b) == [(1, "09:00", "10:00")]
    assert events(migrate_engine, tenant_id=people.a, action="working_hours_changed") == []


# A second save waits for the first's row lock, not for a merged read.
def test_two_concurrent_saves_of_one_member_serialize_through_the_row_lock(
    people: People,
    app: FastAPI,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_1 = signed_in(app, people.a, people.both)
    owner_2 = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    locked = threading.Event()
    release = threading.Event()
    real_member_user = members.member_user
    first_lock_seen: list[bool] = []

    def wrapper(current: SignedIn, target: uuid.UUID, *, lock: bool) -> uuid.UUID:
        result = real_member_user(current, target, lock=lock)
        if lock and not first_lock_seen:
            first_lock_seen.append(True)
            locked.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(members, "member_user", wrapper)
    results: list[Response] = []

    def put(client: TestClient, body: list[dict[str, Any]]) -> None:
        results.append(client.put(hours_path(only_a_member), json=body))

    thread_1 = threading.Thread(
        target=put, args=(owner_1, [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}])
    )
    thread_1.start()
    assert locked.wait(10)
    thread_2 = threading.Thread(
        target=put, args=(owner_2, [{"weekday": 2, "starts_at": "10:00", "ends_at": "11:00"}])
    )
    thread_2.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    thread_1.join(timeout=10)
    thread_2.join(timeout=10)

    assert not thread_1.is_alive()
    assert not thread_2.is_alive()
    assert [r.status_code for r in results] == [200, 200]
    assert stored(people.a, people.only_a) == [(2, "10:00", "11:00")]
    assert len(events(migrate_engine, tenant_id=people.a, action="working_hours_changed")) == 2


# Removing a member cascades their working hours, and only theirs.
def test_removing_a_member_cascades_their_working_hours(people: People, app: FastAPI) -> None:
    seed(people.a, people.only_a, [(1, "09:00", "10:00")])
    seed(people.a, people.both, [(2, "09:00", "10:00")])
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

    assert response.status_code == 204
    with tenant_context(people.a) as session:
        remaining = session.scalar(
            text("SELECT count(*) FROM working_hours WHERE member_id = :m"), {"m": only_a_member}
        )
    assert remaining == 0
    assert stored(people.a, people.both) == [(2, "09:00", "10:00")]


# A save that changes the week is recorded once; an unchanged or already-empty save writes
# nothing.
def test_a_save_that_changes_the_week_is_recorded_once(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)
    rows = [
        {"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"},
        {"weekday": 2, "starts_at": "13:00", "ends_at": "17:00"},
    ]

    assert owner.put(hours_path(only_a_member), json=rows).status_code == 200
    recorded = events(migrate_engine, tenant_id=people.a, action="working_hours_changed")
    assert len(recorded) == 1
    assert recorded[0]["actor_user_id"] == people.both
    assert recorded[0]["target"] == f"user:{people.only_a}"
    assert recorded[0]["details"] is None

    # Same week, another order: nothing new.
    reordered = list(reversed(rows))
    assert owner.put(hours_path(only_a_member), json=reordered).status_code == 200
    assert len(events(migrate_engine, tenant_id=people.a, action="working_hours_changed")) == 1

    # An empty PUT on an already-empty week: nothing new.
    assert owner.put(hours_path(both_member), json=[]).status_code == 200
    assert len(events(migrate_engine, tenant_id=people.a, action="working_hours_changed")) == 1

    # An empty PUT on only_a's non-empty week: one more event.
    assert owner.put(hours_path(only_a_member), json=[]).status_code == 200
    assert len(events(migrate_engine, tenant_id=people.a, action="working_hours_changed")) == 2


# A save whose event fails to record leaves the week unchanged.
def test_a_failed_recording_leaves_the_week_unchanged(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    failing(monkeypatch, auth, "record")

    response = owner.put(
        hours_path(only_a_member), json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]
    )

    assert (response.status_code, response.json()) == (500, {"code": "internal"})
    assert stored(people.a, people.only_a) == []


# Any member may read; no cookie is 401; a non-JSON write is 415 and changes nothing.
def test_any_member_may_read_but_only_json_writes_are_accepted(
    people: People, app: FastAPI
) -> None:
    seed(people.a, people.both, [(1, "09:00", "10:00")])
    worker = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    response = worker.get(hours_path(both_member))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == [{"weekday": 1, "starts_at": "09:00", "ends_at": "10:00"}]

    anon = new_client(app)
    assert anon.get(hours_path(both_member)).status_code == 401

    owner = signed_in(app, people.a, people.both)
    plain = owner.put(hours_path(both_member), content="x", headers={"content-type": "text/plain"})
    assert plain.status_code == 415
    assert stored(people.a, people.both) == [(1, "09:00", "10:00")]


# to_utc converts with the business's own clock, DST included (pure, parametrised).
@pytest.mark.parametrize(
    ("zone", "day", "expected"),
    [
        ("Europe/Amsterdam", date(2026, 3, 28), datetime(2026, 3, 28, 8, tzinfo=UTC)),
        ("Europe/Amsterdam", date(2026, 3, 29), datetime(2026, 3, 29, 7, tzinfo=UTC)),
        ("Europe/Amsterdam", date(2026, 10, 24), datetime(2026, 10, 24, 7, tzinfo=UTC)),
        ("Europe/Amsterdam", date(2026, 10, 25), datetime(2026, 10, 25, 8, tzinfo=UTC)),
        ("America/New_York", date(2026, 3, 7), datetime(2026, 3, 7, 14, tzinfo=UTC)),
        ("America/New_York", date(2026, 3, 8), datetime(2026, 3, 8, 13, tzinfo=UTC)),
        ("America/New_York", date(2026, 10, 31), datetime(2026, 10, 31, 13, tzinfo=UTC)),
        ("America/New_York", date(2026, 11, 1), datetime(2026, 11, 1, 14, tzinfo=UTC)),
        ("America/Sao_Paulo", date(2026, 3, 29), datetime(2026, 3, 29, 12, tzinfo=UTC)),
    ],
)
def test_to_utc_converts_with_the_businesss_own_clock(
    zone: str, day: date, expected: datetime
) -> None:
    result = schedule.to_utc(day, time(9), zone)

    assert result == expected
    assert result.tzinfo is UTC
    assert result.astimezone(ZoneInfo(zone)).time() == time(9)


# The AC's DST test, through the API: the same stored local time converts differently once the
# business's timezone changes.
def test_the_dst_acceptance_case_runs_through_the_api(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    assert (
        owner.put(
            hours_path(only_a_member),
            json=[
                {"weekday": 6, "starts_at": "09:00", "ends_at": "17:00"},  # Saturday
                {"weekday": 7, "starts_at": "09:00", "ends_at": "17:00"},  # Sunday
            ],
        ).status_code
        == 200
    )

    amsterdam_cases = [
        (date(2026, 3, 28), datetime(2026, 3, 28, 8, tzinfo=UTC)),
        (date(2026, 3, 29), datetime(2026, 3, 29, 7, tzinfo=UTC)),
        (date(2026, 10, 24), datetime(2026, 10, 24, 7, tzinfo=UTC)),
        (date(2026, 10, 25), datetime(2026, 10, 25, 8, tzinfo=UTC)),
    ]
    week = owner.get(hours_path(only_a_member)).json()
    zone = owner.get("/api/settings").json()["timezone"]
    for day, expected in amsterdam_cases:
        row = next(r for r in week if r["weekday"] == day.isoweekday())
        assert schedule.to_utc(day, time.fromisoformat(row["starts_at"]), zone) == expected

    assert owner.put("/api/settings", json={"timezone": "America/New_York"}).status_code == 200
    week = owner.get(hours_path(only_a_member)).json()
    zone = owner.get("/api/settings").json()["timezone"]
    ny_cases = [
        (date(2026, 3, 7), datetime(2026, 3, 7, 14, tzinfo=UTC)),
        (date(2026, 3, 8), datetime(2026, 3, 8, 13, tzinfo=UTC)),
    ]
    for day, expected in ny_cases:
        row = next(r for r in week if r["weekday"] == day.isoweekday())
        assert schedule.to_utc(day, time.fromisoformat(row["starts_at"]), zone) == expected


# A shift starting inside a skipped hour converts to an interval that ends before it starts;
# ZIF-48 must drop or clamp such an interval. Pins the DST edges it inherits from to_utc.
def test_to_utc_pins_the_dst_edges_zif_48_inherits() -> None:
    assert schedule.to_utc(date(2026, 3, 29), time(2, 30), "Europe/Amsterdam") == datetime(
        2026, 3, 29, 1, 30, tzinfo=UTC
    )
    assert schedule.to_utc(date(2026, 10, 25), time(2, 30), "Europe/Amsterdam") == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )


# The range type is owned by ziftbook_migrate, and usable by the app role with no grant;
# memberships' UPDATE, which the row lock needs, is still granted.
def test_the_migrate_role_owns_the_range_type_the_app_role_may_use_it(
    migrate_engine: Engine, app_engine: Engine
) -> None:
    with migrate_engine.connect() as conn:
        owner = conn.scalar(
            text("""
            SELECT r.rolname FROM pg_type t JOIN pg_roles r ON r.oid = t.typowner
            WHERE t.typname = 'timerange'
            """)
        )
        usage = conn.scalar(text("SELECT has_type_privilege('ziftbook_app', 'timerange', 'USAGE')"))
        update_priv = conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'memberships', 'UPDATE')")
        )
    assert owner == "ziftbook_migrate"
    assert usage is True
    assert update_priv is True
    with app_engine.connect() as conn:
        overlap = conn.scalar(
            text("SELECT timerange('09:00', '12:00') && timerange('12:00', '15:00')")
        )
    assert overlap is False


# As the app role, in tenant_context(a): the database checks the Python code relies on as backstops
# are really there. Each failing statement rolls back on its own; the one insert that succeeds
# commits and is removed by the membership-cascade teardown.
@pytest.mark.parametrize(
    ("weekday", "starts", "ends", "constraint"),
    [
        (0, "09:00", "10:00", "ck_working_hours_iso_weekday"),
        (8, "09:00", "10:00", "ck_working_hours_iso_weekday"),
        (1, "10:00", "09:00", "ck_working_hours_ends_after_start"),
    ],
)
def test_direct_inserts_hit_the_database_checks(
    people: People, weekday: int, starts: str, ends: str, constraint: str
) -> None:
    only_a_member = member_id(people.a, people.only_a)

    with pytest.raises(IntegrityError) as exc_info:
        with tenant_context(people.a) as session:
            session.execute(
                text(INSERT_HOURS),
                {"t": people.a, "m": only_a_member, "w": weekday, "s": starts, "e": ends},
            )

    assert isinstance(exc_info.value.orig, pg_errors.CheckViolation)
    assert exc_info.value.orig.diag.constraint_name == constraint


def test_direct_overlapping_inserts_hit_the_exclusion_constraint(people: People) -> None:
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    with pytest.raises(IntegrityError) as exc_info:
        with tenant_context(people.a) as session:
            session.execute(
                text(INSERT_HOURS),
                {"t": people.a, "m": only_a_member, "w": 1, "s": "09:00", "e": "12:00"},
            )
            session.execute(
                text(INSERT_HOURS),
                {"t": people.a, "m": only_a_member, "w": 1, "s": "10:00", "e": "13:00"},
            )

    assert isinstance(exc_info.value.orig, pg_errors.ExclusionViolation)
    assert exc_info.value.orig.diag.constraint_name == schedule.OVERLAP

    # The same times for another member of the same business succeed.
    with tenant_context(people.a) as session:
        session.execute(
            text(INSERT_HOURS),
            {"t": people.a, "m": both_member, "w": 1, "s": "09:00", "e": "12:00"},
        )


def test_the_composite_fk_refuses_a_member_from_another_tenant(people: People) -> None:
    b_member = member_id(people.b, people.only_b)

    with pytest.raises(IntegrityError) as exc_info:
        with tenant_context(people.a) as session:
            session.execute(
                text(INSERT_HOURS),
                {"t": people.a, "m": b_member, "w": 1, "s": "09:00", "e": "10:00"},
            )

    assert isinstance(exc_info.value.orig, pg_errors.ForeignKeyViolation)
