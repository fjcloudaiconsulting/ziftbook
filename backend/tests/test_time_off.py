"""GET/POST/PATCH/DELETE the time-off endpoints: blocks when a member can't be booked (ZIF-47)."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg.errors as pg_errors
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app import auth, members
from app.auth import SignedIn
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    events,
    failing,
    member_id,
    new_client,
    put_settings,
    signed_in,
    wait_until_blocked,
)

WIDE = {"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def path(member: uuid.UUID) -> str:
    return f"/api/members/{member}/time-off"


def block_path(time_off_id: Any) -> str:
    return f"/api/time-off/{time_off_id}"


def block(client: TestClient, member: uuid.UUID, **over: Any) -> Response:
    body = {
        "starts_at": "2026-10-01T08:00:00Z",
        "ends_at": "2026-10-03T08:00:00Z",
        "reason": "Surgery",
    }
    body.update(over)
    return client.post(path(member), json=body)


def rows(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    with tenant_context(tenant_id) as session:
        return [
            dict(r) for r in session.execute(text("SELECT * FROM time_off ORDER BY id")).mappings()
        ]


def insert_block(
    tenant_id: uuid.UUID,
    member: uuid.UUID,
    starts_at: str,
    ends_at: str,
    *,
    reason: str | None = None,
    source: str = "manual",
    external_id: str | None = None,
) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        found: uuid.UUID = session.scalar(
            text("""
            INSERT INTO time_off
                (tenant_id, member_id, starts_at, ends_at, reason, source, external_id)
            VALUES (:t, :m, :s, :e, :r, :src, :ext)
            RETURNING id
            """),
            {
                "t": tenant_id,
                "m": member,
                "s": starts_at,
                "e": ends_at,
                "r": reason,
                "src": source,
                "ext": external_id,
            },
        )
    return found


def google(tenant_id: uuid.UUID, member: uuid.UUID, external_id: str = "evt-1") -> uuid.UUID:
    return insert_block(
        tenant_id,
        member,
        "2026-11-01T00:00:00Z",
        "2026-11-02T00:00:00Z",
        source="google",
        external_id=external_id,
    )


# 1. fence: a worker fully manages their own block: create, edit, delete.
def test_worker_manages_their_own_block(people: People, app: FastAPI) -> None:
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)

    created = block(worker, only_a_member)

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body["member_id"] == str(only_a_member)
    assert body["starts_at"] == "2026-10-01T08:00:00Z"
    assert body["ends_at"] == "2026-10-03T08:00:00Z"
    assert body["reason"] == "Surgery"
    assert body["source"] == "manual"

    patched = worker.patch(block_path(body["id"]), json={"reason": "Dentist"})
    assert (patched.status_code, patched.json()["reason"]) == (200, "Dentist")
    assert patched.headers["cache-control"] == "no-store"

    deleted = worker.request("DELETE", block_path(body["id"]), json={})
    assert deleted.status_code == 204
    assert rows(people.a) == []


# 2. fence (parametrised): a worker may not touch another member's block, even in their own
# business, and gets owner_only. Nothing changes, nothing is recorded.
@pytest.mark.parametrize("op", ["POST", "PATCH", "DELETE"])
def test_a_worker_may_not_manage_another_members_block(
    people: People, app: FastAPI, migrate_engine: Engine, op: str
) -> None:
    both_member = member_id(people.a, people.both)
    owner = signed_in(app, people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)
    existing_id = block(owner, both_member).json()["id"]

    if op == "POST":
        response = block(worker, both_member)
    elif op == "PATCH":
        response = worker.patch(block_path(existing_id), json={"reason": "Dentist"})
    else:
        response = worker.request("DELETE", block_path(existing_id), json={})

    assert response.status_code == 403
    assert response.json() == {"code": "owner_only"}
    assert len(rows(people.a)) == 1
    assert (
        events(migrate_engine, tenant_id=people.a, action="time_off_changed")
        == events(migrate_engine, tenant_id=people.a, action="time_off_deleted")
        == []
    )


# fence: permission is checked before the times, so a worker's invalid body for a colleague still
# gets owner_only, not the validation error.
def test_a_worker_posting_invalid_times_for_a_colleague_gets_owner_only(
    people: People, app: FastAPI
) -> None:
    both_member = member_id(people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)

    response = block(
        worker, both_member, starts_at="2026-10-01T08:00:00Z", ends_at="2026-10-01T08:00:00Z"
    )

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})


# 3. fence: an owner manages anyone's block, including their own.
@pytest.mark.parametrize("who", ["only_a", "both"])
def test_owner_manages_any_members_block(people: People, app: FastAPI, who: str) -> None:
    owner = signed_in(app, people.a, people.both)
    target_user = people.only_a if who == "only_a" else people.both
    target_member = member_id(people.a, target_user)

    created = block(owner, target_member)
    assert created.status_code == 201
    patched = owner.patch(block_path(created.json()["id"]), json={"reason": "Dentist"})
    assert patched.status_code == 200
    deleted = owner.request("DELETE", block_path(created.json()["id"]), json={})
    assert deleted.status_code == 204


# 4. fence (parametrised over the four routes and callers): a member or a block of another
# business, or a random id, is 404 for both caller roles; b's row is unchanged, nothing recorded.
@pytest.mark.parametrize("caller", ["owner", "worker"])
@pytest.mark.parametrize("bogus", ["other_business", "random"])
def test_another_businesss_member_or_block_is_not_found(
    people: People, app: FastAPI, migrate_engine: Engine, caller: str, bogus: str
) -> None:
    b_member = member_id(people.b, people.only_b)
    b_block_id = insert_block(people.b, b_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")
    client = signed_in(app, people.a, people.both if caller == "owner" else people.only_a)
    member_target = b_member if bogus == "other_business" else uuid.uuid7()
    block_target = b_block_id if bogus == "other_business" else uuid.uuid7()

    get_r = client.get(path(member_target), params=WIDE)
    post_r = block(client, member_target)
    patch_r = client.patch(block_path(block_target), json={"reason": "Dentist"})
    delete_r = client.request("DELETE", block_path(block_target), json={})

    for response in (get_r, post_r, patch_r, delete_r):
        assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    assert len(rows(people.b)) == 1
    assert rows(people.b)[0]["id"] == b_block_id
    assert events(migrate_engine, tenant_id=people.a, action="time_off_created") == []


# 5. fence (parametrised): malformed instants give 422 invalid_request, with nothing stored.
BAD_INSTANTS = [
    pytest.param("2026-10-01T08:00:00", id="naive"),
    pytest.param("2026-10-01", id="date-only"),
    pytest.param(1789000000, id="epoch-int"),
    pytest.param("1789000000", id="epoch-string"),
    pytest.param("0001-01-01T00:00:00+01:00", id="year-1"),
    pytest.param("3000-01-01T00:00:00Z", id="year-3000"),
    pytest.param(None, id="null"),
    pytest.param("٢٠٢٦-10-01T08:00:00Z", id="arabic-digits"),
]


@pytest.mark.parametrize("bad", BAD_INSTANTS)
def test_a_malformed_instant_is_refused(people: People, app: FastAPI, bad: Any) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    post_r = block(owner, only_a_member, starts_at=bad)
    assert (post_r.status_code, post_r.json()) == (422, {"code": "invalid_request"})

    created = block(owner, only_a_member).json()
    patch_r = owner.patch(block_path(created["id"]), json={"starts_at": bad})
    assert (patch_r.status_code, patch_r.json()) == (422, {"code": "invalid_request"})

    if bad is not None:  # httpx drops a None param; that's just "missing from"
        get_r = owner.get(path(only_a_member), params={"from": bad, "to": WIDE["to"]})
        assert (get_r.status_code, get_r.json()) == (422, {"code": "invalid_request"})

    assert len(rows(people.a)) == 1  # only the one valid block from `created`


# 6. guard: an offset instant is stored as UTC, and the year bounds (2000, 2999) are accepted.
def test_offset_instants_are_stored_as_utc_and_year_bounds_are_accepted(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    created = block(
        owner,
        only_a_member,
        starts_at="2026-10-01T10:00:00+02:00",
        ends_at="2026-10-03T10:00:00+02:00",
    )
    assert created.status_code == 201
    assert created.json()["starts_at"] == "2026-10-01T08:00:00Z"
    assert created.json()["ends_at"] == "2026-10-03T08:00:00Z"

    lower = owner.get(
        path(only_a_member), params={"from": "2000-01-01T00:00:00Z", "to": "2000-01-02T00:00:00Z"}
    )
    assert lower.status_code == 200
    upper = owner.get(
        path(only_a_member), params={"from": "2999-12-30T00:00:00Z", "to": "2999-12-31T00:00:00Z"}
    )
    assert upper.status_code == 200


# fence (parametrised POST/PATCH): the reason has the same bounds at the API as the database.
@pytest.mark.parametrize(
    "bad_reason",
    [
        pytest.param("x" * 501, id="too-long"),
        pytest.param("nope\x07", id="control-char"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
@pytest.mark.parametrize("route", ["POST", "PATCH"])
def test_reason_bounds_are_enforced_at_the_api(
    people: People, app: FastAPI, route: str, bad_reason: str
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    existing = block(owner, only_a_member).json()

    if route == "POST":
        response = block(owner, only_a_member, reason=bad_reason)
    else:
        response = owner.patch(block_path(existing["id"]), json={"reason": bad_reason})

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


# guard: a reason's surrounding whitespace is trimmed, on POST and PATCH.
def test_reason_is_stripped_of_surrounding_whitespace(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    created = block(owner, only_a_member, reason="  Dentist  ")
    assert created.status_code == 201
    assert created.json()["reason"] == "Dentist"

    patched = owner.patch(block_path(created.json()["id"]), json={"reason": "  Surgery  "})
    assert patched.status_code == 200
    assert patched.json()["reason"] == "Surgery"


# 7. fence (parametrised POST, and PATCH of ends_at only): end must be strictly after start, and the
# merged values are what's checked.
def test_end_must_be_after_start(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    same = block(
        owner, only_a_member, starts_at="2026-10-01T08:00:00Z", ends_at="2026-10-01T08:00:00Z"
    )
    assert (same.status_code, same.json()) == (422, {"code": "end_not_after_start"})

    before = block(
        owner, only_a_member, starts_at="2026-10-01T08:00:00Z", ends_at="2026-10-01T07:00:00Z"
    )
    assert (before.status_code, before.json()) == (422, {"code": "end_not_after_start"})

    created = block(
        owner, only_a_member, starts_at="2026-10-01T08:00:00Z", ends_at="2026-10-03T08:00:00Z"
    )
    patched = owner.patch(
        block_path(created.json()["id"]), json={"ends_at": "2026-09-30T00:00:00Z"}
    )
    assert (patched.status_code, patched.json()) == (422, {"code": "end_not_after_start"})


# 8. fence (parametrised) + guard at the edge: at most 366 days, including over a DST change.
def test_a_block_may_not_exceed_366_days(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    accepted_hours = block(
        owner,
        only_a_member,
        starts_at=start.isoformat().replace("+00:00", "Z"),
        ends_at=(start + timedelta(hours=8761)).isoformat().replace("+00:00", "Z"),
    )
    assert accepted_hours.status_code == 201

    refused_hours = block(
        owner,
        only_a_member,
        starts_at=start.isoformat().replace("+00:00", "Z"),
        ends_at=(start + timedelta(hours=8785)).isoformat().replace("+00:00", "Z"),
    )
    assert (refused_hours.status_code, refused_hours.json()) == (422, {"code": "time_off_too_long"})

    exactly_366_days = block(
        owner,
        only_a_member,
        starts_at="2026-03-01T00:00:00+01:00",
        ends_at="2027-03-02T01:00:00+02:00",
    )
    assert exactly_366_days.status_code == 201

    moved_back = owner.patch(
        block_path(accepted_hours.json()["id"]),
        json={"starts_at": (start - timedelta(hours=25)).isoformat().replace("+00:00", "Z")},
    )
    assert (moved_back.status_code, moved_back.json()) == (422, {"code": "time_off_too_long"})


# 9. fence (window edges, half-open): P/Q/R/S seeded out of start order; the window edges and the
# ordering are both pinned.
def test_window_edges_are_half_open_and_ordered(people: People, app: FastAPI) -> None:
    only_a_member = member_id(people.a, people.only_a)
    # Inserted so that, within every retained subset below, insertion order differs from the
    # expected starts_at order: a missing ORDER BY (heap/insertion order) must be distinguishable
    # from the correct, sorted answer.
    q_id = insert_block(people.a, only_a_member, "2026-10-01T10:00:00Z", "2026-10-01T12:00:00Z")
    r_id = insert_block(people.a, only_a_member, "2026-10-01T12:00:00Z", "2026-10-01T14:00:00Z")
    p_id = insert_block(people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-01T10:00:00Z")
    s_id = insert_block(people.a, only_a_member, "2026-10-01T06:00:00Z", "2026-10-01T20:00:00Z")
    owner = signed_in(app, people.a, people.both)

    middle = owner.get(
        path(only_a_member), params={"from": "2026-10-01T10:00:00Z", "to": "2026-10-01T12:00:00Z"}
    )
    assert [b["id"] for b in middle.json()] == [str(s_id), str(q_id)]

    just_before = owner.get(
        path(only_a_member),
        params={"from": "2026-10-01T09:59:59.999999Z", "to": "2026-10-01T10:00:00Z"},
    )
    assert [b["id"] for b in just_before.json()] == [str(s_id), str(p_id)]

    late = owner.get(
        path(only_a_member), params={"from": "2026-10-01T14:00:00Z", "to": "2026-10-01T15:00:00Z"}
    )
    assert [b["id"] for b in late.json()] == [str(s_id)]
    assert r_id != s_id  # R was seeded (and excluded above); keeps the variable meaningful


# 10. fence (parametrised): the window itself is checked before the lookup.
def test_the_window_must_be_valid(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    equal = owner.get(
        path(only_a_member), params={"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}
    )
    assert (equal.status_code, equal.json()) == (422, {"code": "invalid_window"})

    reversed_ = owner.get(
        path(only_a_member), params={"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}
    )
    assert (reversed_.status_code, reversed_.json()) == (422, {"code": "invalid_window"})

    # The window is checked before the member is even looked up: an unknown id still gets
    # invalid_window, not not_found.
    unknown_member = owner.get(
        path(uuid.uuid7()), params={"from": "2026-02-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}
    )
    assert (unknown_member.status_code, unknown_member.json()) == (422, {"code": "invalid_window"})

    too_long = owner.get(
        path(only_a_member),
        params={"from": "2026-01-01T00:00:00Z", "to": "2027-01-02T00:00:00.000001Z"},
    )
    assert (too_long.status_code, too_long.json()) == (422, {"code": "invalid_window"})

    exactly_366 = owner.get(
        path(only_a_member), params={"from": "2026-01-01T00:00:00Z", "to": "2027-01-02T00:00:00Z"}
    )
    assert exactly_366.status_code == 200

    missing_from = owner.get(path(only_a_member), params={"to": "2026-12-31T00:00:00Z"})
    assert (missing_from.status_code, missing_from.json()) == (422, {"code": "invalid_request"})


# 11. fence: the reason is visible only to the block's own member, and to owners.
def test_reason_is_visible_only_to_the_member_and_owners(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    owners_block = block(owner, both_member, reason="Chemo")
    workers_block = block(worker, only_a_member, reason="Dentist")
    owners_block_id = owners_block.json()["id"]
    assert owners_block.json()["reason"] == "Chemo"
    assert workers_block.json()["reason"] == "Dentist"

    own_view = worker.get(path(only_a_member), params=WIDE)
    assert own_view.headers["cache-control"] == "no-store"
    assert any(b["reason"] == "Dentist" for b in own_view.json())

    owner_view_of_worker = owner.get(path(only_a_member), params=WIDE)
    assert any(b["reason"] == "Dentist" for b in owner_view_of_worker.json())
    owner_view_of_self = owner.get(path(both_member), params=WIDE)
    assert any(b["reason"] == "Chemo" for b in owner_view_of_self.json())

    workers_view_of_owner = worker.get(path(both_member), params=WIDE)
    body = workers_view_of_owner.json()
    # Redacted, not hidden: the block is listed, with its reason nulled.
    assert [(b["id"], b["reason"]) for b in body] == [(owners_block_id, None)]
    assert "Chemo" not in workers_view_of_owner.text


# 12. fence: Google rows are listed but never managed here, and never expose external_id.
def test_google_rows_are_listed_but_not_managed(people: People, app: FastAPI) -> None:
    only_a_member = member_id(people.a, people.only_a)
    google_id = google(people.a, only_a_member)
    owner = signed_in(app, people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)

    listed = owner.get(path(only_a_member), params=WIDE).json()
    (found,) = [b for b in listed if b["id"] == str(google_id)]
    assert found["source"] == "google"
    assert "external_id" not in found

    for client in (owner, worker):
        patched = client.patch(block_path(google_id), json={"reason": "nope"})
        assert (patched.status_code, patched.json()) == (404, {"code": "not_found"})
        deleted = client.request("DELETE", block_path(google_id), json={})
        assert (deleted.status_code, deleted.json()) == (404, {"code": "not_found"})
    assert len(rows(people.a)) == 1


# 13. fence: PATCH partial semantics: omitted vs null vs resent, and forbidden fields.
def test_patch_partial_semantics(people: People, app: FastAPI, migrate_engine: Engine) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    created = block(owner, only_a_member).json()
    block_id = created["id"]

    cleared = owner.patch(block_path(block_id), json={"reason": None})
    assert cleared.status_code == 200
    assert cleared.json()["reason"] is None
    assert cleared.json()["starts_at"] == created["starts_at"]

    moved = owner.patch(block_path(block_id), json={"starts_at": "2026-10-02T08:00:00Z"})
    assert moved.status_code == 200
    assert moved.json()["ends_at"] == created["ends_at"]
    assert moved.json()["reason"] is None

    for field in ("starts_at", "ends_at"):
        null_time = owner.patch(block_path(block_id), json={field: None})
        assert (null_time.status_code, null_time.json()) == (422, {"code": "invalid_request"})

    before = events(migrate_engine, tenant_id=people.a, action="time_off_changed")
    empty = owner.patch(block_path(block_id), json={})
    assert empty.status_code == 200
    resend = owner.patch(
        block_path(block_id),
        json={"starts_at": moved.json()["starts_at"], "ends_at": moved.json()["ends_at"]},
    )
    assert resend.status_code == 200
    after = events(migrate_engine, tenant_id=people.a, action="time_off_changed")
    assert len(after) == len(before)

    for bad_body in ({"source": "google"}, {"member_id": str(uuid.uuid7())}, {"reason": ""}):
        response = owner.patch(block_path(block_id), json=bad_body)
        assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


# 14. fence: audit records exactly what changed, with no reason and no times.
def test_audit_records_no_reason_or_times(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    created = block(
        owner,
        only_a_member,
        starts_at="2031-06-01T08:00:00Z",
        ends_at="2031-06-03T08:00:00Z",
        reason="Surgery",
    )
    block_id = created.json()["id"]
    assert owner.patch(block_path(block_id), json={"reason": "Dentist"}).status_code == 200
    assert owner.request("DELETE", block_path(block_id), json={}).status_code == 204

    recorded = events(migrate_engine, tenant_id=people.a)
    assert [e["action"] for e in recorded] == [
        "time_off_created",
        "time_off_changed",
        "time_off_deleted",
    ]
    for event in recorded:
        assert event["actor_user_id"] == people.both
        assert event["target"] == f"user:{people.only_a}"
        assert event["details"] is None

    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        dump = conn.execute(
            text("""
            SELECT string_agg(
                actor_user_id::text || action || coalesce(target, '')
                    || coalesce(details::text, ''),
                ' '
            ) FROM audit_events WHERE tenant_id = :a
            """),
            {"a": people.a},
        ).scalar()
    assert "Surgery" not in (dump or "")
    assert "Dentist" not in (dump or "")
    assert "2031-06-01" not in (dump or "")


# 15. fence (parametrised): a failed recording leaves the block unchanged.
@pytest.mark.parametrize("op", ["POST", "PATCH", "DELETE"])
def test_a_failed_recording_leaves_blocks_unchanged(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    existing_id: str | None = None
    if op != "POST":
        existing_id = block(owner, only_a_member).json()["id"]
    before = rows(people.a)
    failing(monkeypatch, auth, "record")

    if op == "POST":
        response = block(owner, only_a_member)
    elif op == "PATCH":
        response = owner.patch(block_path(existing_id), json={"reason": "Dentist"})
    else:
        response = owner.request("DELETE", block_path(existing_id), json={})

    assert (response.status_code, response.json()) == (500, {"code": "internal"})
    assert rows(people.a) == before


# 16. fence: removing a member cascades their time off, and only theirs.
def test_removing_a_member_cascades_their_time_off(people: People, app: FastAPI) -> None:
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)
    insert_block(people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")
    insert_block(people.a, both_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")
    owner = signed_in(app, people.a, people.both)

    response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

    assert response.status_code == 204
    with tenant_context(people.a) as session:
        remaining = session.scalar(
            text("SELECT count(*) FROM time_off WHERE member_id = :m"), {"m": only_a_member}
        )
    assert remaining == 0
    assert len(rows(people.a)) == 1


# 17. fence (lock order): a removal in flight for the block's member never lets a write through
# with a stale member row, and never deadlocks against it.
@pytest.mark.parametrize("op", ["POST", "PATCH"])
def test_a_write_waits_on_a_membership_removal_in_flight(
    people: People, app: FastAPI, app_engine: Engine, op: str
) -> None:
    only_a_member = member_id(people.a, people.only_a)
    existing_id = None
    if op == "PATCH":
        existing_id = insert_block(
            people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z"
        )
    owner = signed_in(app, people.a, people.both)
    results: list[Response] = []

    def request() -> None:
        if op == "POST":
            results.append(block(owner, only_a_member))
        else:
            results.append(owner.patch(block_path(existing_id), json={"reason": "Dentist"}))

    with app_engine.connect() as holder:
        with holder.begin():
            holder.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)}
            )
            holder.execute(text("DELETE FROM memberships WHERE id = :m"), {"m": only_a_member})
            thread = threading.Thread(target=request)
            thread.start()
            wait_until_blocked(app_engine, 1)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(results) == 1
    assert (results[0].status_code, results[0].json()) == (404, {"code": "not_found"})


def test_a_write_and_a_removal_of_the_same_member_never_deadlock(
    people: People, app: FastAPI, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    only_a_member = member_id(people.a, people.only_a)
    block_id = insert_block(people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")
    owner = signed_in(app, people.a, people.both)
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
    results: dict[str, Response] = {}

    def do_patch() -> None:
        results["patch"] = owner.patch(block_path(block_id), json={"reason": "Dentist"})

    def do_remove() -> None:
        results["remove"] = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

    patch_thread = threading.Thread(target=do_patch)
    patch_thread.start()
    assert locked.wait(10)
    remove_thread = threading.Thread(target=do_remove)
    remove_thread.start()
    wait_until_blocked(app_engine, 1)
    release.set()
    patch_thread.join(timeout=10)
    remove_thread.join(timeout=10)

    assert not patch_thread.is_alive()
    assert not remove_thread.is_alive()
    assert results["patch"].status_code == 200
    assert results["remove"].status_code == 204
    assert rows(people.a) == []


# 18. guard: no cookie is 401; a non-JSON write is 415; a non-UUID path is 422.
def test_auth_and_content_type_guards(people: People, app: FastAPI) -> None:
    only_a_member = member_id(people.a, people.only_a)
    block_id = insert_block(people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")
    anon = new_client(app)

    assert anon.get(path(only_a_member), params=WIDE).status_code == 401
    assert anon.post(path(only_a_member), json={}).status_code == 401
    assert anon.patch(block_path(block_id), json={}).status_code == 401
    assert anon.request("DELETE", block_path(block_id), json={}).status_code == 401

    owner = signed_in(app, people.a, people.both)
    plain = owner.post(path(only_a_member), content="x", headers={"content-type": "text/plain"})
    assert plain.status_code == 415
    plain_patch = owner.patch(
        block_path(block_id), content="x", headers={"content-type": "text/plain"}
    )
    assert plain_patch.status_code == 415
    plain_delete = owner.request(
        "DELETE", block_path(block_id), content="x", headers={"content-type": "text/plain"}
    )
    assert plain_delete.status_code == 415
    assert len(rows(people.a)) == 1

    bad_path = owner.get("/api/members/not-a-uuid/time-off", params=WIDE)
    assert bad_path.status_code == 422
    bad_block_path = owner.patch("/api/time-off/not-a-uuid", json={})
    assert bad_block_path.status_code == 422


# 19. guard: 20 blocks are all listed, in starts_at order, even seeded in reverse.
def test_twenty_blocks_are_all_listed_in_order(people: People, app: FastAPI) -> None:
    only_a_member = member_id(people.a, people.only_a)
    expected_ids: list[uuid.UUID] = []
    for day in range(20, 0, -1):
        starts = f"2026-03-{day:02d}T00:00:00Z"
        ends = f"2026-03-{day:02d}T01:00:00Z"
        expected_ids.append(insert_block(people.a, only_a_member, starts, ends))
    owner = signed_in(app, people.a, people.both)

    listed = owner.get(path(only_a_member), params=WIDE).json()

    assert len(listed) == 20
    assert {b["id"] for b in listed} == {str(i) for i in expected_ids}
    seen_starts = [b["starts_at"] for b in listed]
    assert seen_starts == sorted(seen_starts)


# 20. fence (DB, app role, tenant_context(a)): the checks the Python code relies on as backstops.
@pytest.mark.parametrize(
    ("starts", "ends", "reason", "source", "external_id", "constraint"),
    [
        (
            "2026-10-01T08:00:00Z",
            "2026-10-01T08:00:00Z",
            None,
            "manual",
            None,
            "ck_time_off_ends_after_start",
        ),
        (
            "2026-10-01T08:00:00Z",
            (datetime(2026, 10, 1, 8, tzinfo=UTC) + timedelta(days=366, seconds=1)).isoformat(),
            None,
            "manual",
            None,
            "ck_time_off_at_most_366_days",
        ),
        (
            "2026-10-01T08:00:00Z",
            "2026-10-02T08:00:00Z",
            "",
            "manual",
            None,
            "ck_time_off_reason_length",
        ),
        (
            "2026-10-01T08:00:00Z",
            "2026-10-02T08:00:00Z",
            "x" * 501,
            "manual",
            None,
            "ck_time_off_reason_length",
        ),
        # external_id set (not None), so only the source check is violated, not the pairing one.
        ("2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z", None, "ical", "x", "ck_time_off_source"),
        (
            "2026-10-01T08:00:00Z",
            "2026-10-02T08:00:00Z",
            None,
            "manual",
            "evt-1",
            "ck_time_off_external_id",
        ),
        (
            "2026-10-01T08:00:00Z",
            "2026-10-02T08:00:00Z",
            None,
            "google",
            None,
            "ck_time_off_external_id",
        ),
    ],
)
def test_direct_inserts_hit_the_database_checks(
    people: People,
    starts: str,
    ends: str,
    reason: str | None,
    source: str,
    external_id: str | None,
    constraint: str,
) -> None:
    only_a_member = member_id(people.a, people.only_a)

    with pytest.raises(IntegrityError) as exc_info:
        insert_block(
            people.a,
            only_a_member,
            starts,
            ends,
            reason=reason,
            source=source,
            external_id=external_id,
        )

    assert isinstance(exc_info.value.orig, pg_errors.CheckViolation)
    assert exc_info.value.orig.diag.constraint_name == constraint


DAY_ROW = """
INSERT INTO time_off (tenant_id, member_id, starts_at, ends_at, first_day, last_day)
VALUES (:t, :m, :s, :e, :f, :l)
"""


# 25. fence (DB, ZIF-101): raw INSERTs refused by ck_time_off_kind and ck_time_off_days, naming the
# right constraint. Kills app-only validation and a CHECK without the year bound.
@pytest.mark.parametrize(
    ("starts", "ends", "first_day", "last_day", "constraint"),
    [
        (  # both pairs
            "2026-10-01T08:00:00Z",
            "2026-10-02T08:00:00Z",
            "2026-10-01",
            "2026-10-02",
            "ck_time_off_kind",
        ),
        (None, None, "2026-10-01", None, "ck_time_off_kind"),  # half a pair, days
        ("2026-10-01T08:00:00Z", None, None, None, "ck_time_off_kind"),  # half a pair, instants
        (None, None, None, None, "ck_time_off_kind"),  # empty
        (None, None, "2026-10-05", "2026-10-04", "ck_time_off_days"),  # last_day < first_day
        (None, None, "2026-10-01", "2027-10-02", "ck_time_off_days"),  # 367 days
        (None, None, "1999-12-31", "2000-01-01", "ck_time_off_days"),  # below the year bound
        # A single day, well under the 366-day count bound: isolates the upper year bound alone.
        # 2999-12-31..9999-12-30 would also be refused by the day-count clause, making it vacuous.
        (None, None, "3000-01-01", "3000-01-01", "ck_time_off_days"),  # above the year bound
    ],
)
def test_direct_inserts_hit_the_zif_101_database_checks(
    people: People,
    starts: str | None,
    ends: str | None,
    first_day: str | None,
    last_day: str | None,
    constraint: str,
) -> None:
    only_a_member = member_id(people.a, people.only_a)

    with pytest.raises(IntegrityError) as exc_info, tenant_context(people.a) as session:
        session.execute(
            text(DAY_ROW),
            {
                "t": people.a,
                "m": only_a_member,
                "s": starts,
                "e": ends,
                "f": first_day,
                "l": last_day,
            },
        )

    assert isinstance(exc_info.value.orig, pg_errors.CheckViolation)
    assert exc_info.value.orig.diag.constraint_name == constraint


def test_a_second_google_row_with_the_same_external_id_is_refused(people: People) -> None:
    only_a_member = member_id(people.a, people.only_a)
    google(people.a, only_a_member, external_id="dup")

    with pytest.raises(IntegrityError) as exc_info:
        google(people.a, only_a_member, external_id="dup")

    assert isinstance(exc_info.value.orig, pg_errors.UniqueViolation)


def test_direct_inserts_that_should_succeed(people: People) -> None:
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    insert_block(people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")
    insert_block(
        people.a, only_a_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z"
    )  # identical, fine
    insert_block(
        people.a, only_a_member, "2026-10-01T09:00:00Z", "2026-10-01T11:00:00Z"
    )  # overlapping, fine
    google(people.a, only_a_member, external_id="shared")
    google(people.a, both_member, external_id="shared")  # same external_id, different member: fine

    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM time_off"))


def test_the_composite_fk_refuses_a_member_from_another_tenant(people: People) -> None:
    b_member = member_id(people.b, people.only_b)

    with pytest.raises(IntegrityError) as exc_info:
        insert_block(people.a, b_member, "2026-10-01T08:00:00Z", "2026-10-02T08:00:00Z")

    assert isinstance(exc_info.value.orig, pg_errors.ForeignKeyViolation)


# 21. guard: the index and the grants the lock and the cascade depend on are really there.
def test_schema_grants_and_index(migrate_engine: Engine) -> None:
    with migrate_engine.connect() as conn:
        index_columns: str | None = conn.execute(
            text("""
            SELECT indexdef FROM pg_indexes
            WHERE tablename = 'time_off' AND indexname = 'ix_time_off_tenant_id_member_id_starts_at'
            """)
        ).scalar()
        delete_priv = conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'time_off', 'DELETE')")
        )
        update_priv = conn.scalar(
            text("SELECT has_table_privilege('ziftbook_app', 'memberships', 'UPDATE')")
        )
    assert index_columns is not None
    assert "tenant_id" in index_columns
    assert "member_id" in index_columns
    assert "starts_at" in index_columns
    assert delete_priv is True
    assert update_priv is True
    with migrate_engine.connect() as conn:
        # Tripwire for a future column REVOKE (ZIF-101): the two new columns must stay writable
        # with no GRANT, since 0028 adds them under time_off's table-level default privileges
        # (0001:22) and grants no column list of its own.
        first_day_update = conn.scalar(
            text("SELECT has_column_privilege('ziftbook_app', 'time_off', 'first_day', 'UPDATE')")
        )
    assert first_day_update is True


def day_block(client: TestClient, member: uuid.UUID, **over: Any) -> Response:
    body = {"first_day": "2026-10-27", "last_day": "2026-11-02", "reason": "Holiday"}
    body.update(over)
    return client.post(path(member), json=body)


# 15. fence: POST days -> 201, response has first_day/last_day and starts_at = ends_at = null; the
# DB row has NULL instants (read as events() does: set_config('app.tenant_id', ...) first, or
# FORCE RLS raises 42704). Kills "midnight instants plus a flag" and emitting derived instants.
def test_post_days_returns_null_instants_and_stores_null_instants(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = day_block(owner, only_a_member)

    assert response.status_code == 201
    body = response.json()
    assert body["first_day"] == "2026-10-27"
    assert body["last_day"] == "2026-11-02"
    assert body["starts_at"] is None
    assert body["ends_at"] is None

    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        row = conn.execute(
            text("SELECT starts_at, ends_at, first_day, last_day FROM time_off WHERE id = :id"),
            {"id": body["id"]},
        ).one()
    assert row.starts_at is None
    assert row.ends_at is None
    assert row.first_day.isoformat() == "2026-10-27"
    assert row.last_day.isoformat() == "2026-11-02"


# 16. fence (parametrised): 422 invalid_request for malformed or mixed bodies. Kills a lax date
# field, a union that silently prefers one pair, and a missing year bound.
@pytest.mark.parametrize(
    "over",
    [
        {
            "first_day": "2026-10-27",
            "starts_at": "2026-10-27T09:00:00Z",
            "ends_at": "2026-10-27T10:00:00Z",
        },
        {"first_day": "2026-10-27"},  # half a pair
        {"starts_at": "2026-10-01T08:00:00Z"},  # half the other pair, no day fields
        {"first_day": None, "last_day": "2026-11-02"},
        {"first_day": "2026-10-03T00:00", "last_day": "2026-11-02"},  # a datetime, not a date
        {"first_day": 1788998400, "last_day": "2026-11-02"},  # an epoch number, midnight-aligned
        # Paired with itself: the day-count clause never fires, so only the year bound refuses it.
        {"first_day": "1999-12-31", "last_day": "1999-12-31"},  # below the year bound
        {"first_day": "3000-01-01", "last_day": "3000-01-01"},  # above the year bound
        {"first_day": "0001-01-01", "last_day": "0001-01-01"},  # never 500
        {"first_day": "2026-10-27", "last_day": "2026-11-02", "starts_at": None},  # explicit null
    ],
)
def test_a_malformed_or_mixed_days_body_is_refused(
    people: People, app: FastAPI, over: dict[str, Any]
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    # Not through day_block(): its defaults would silently fill in a field the case means to omit.
    response = owner.post(path(only_a_member), json=over)

    assert (response.status_code, response.json()["code"]) == (422, "invalid_request")


def test_neither_pair_is_sent_is_refused(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    result = owner.post(path(only_a_member), json={"reason": "x"})

    assert (result.status_code, result.json()["code"]) == (422, "invalid_request")


# 17. fence: last_day < first_day -> 422 last_day_before_first_day; first == last -> 201. Kills
# reusing checked()'s strict `>`.
def test_last_day_before_first_day_is_refused_but_a_single_day_is_accepted(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    backwards = day_block(owner, only_a_member, first_day="2026-10-05", last_day="2026-10-04")
    assert (backwards.status_code, backwards.json()["code"]) == (422, "last_day_before_first_day")

    single = day_block(owner, only_a_member, first_day="2026-10-05", last_day="2026-10-05")
    assert single.status_code == 201


# 18. fence + guard at the edge: at most 366 days (a count, not elapsed time), across two
# fall-backs and a spring-forward. Kills validating days by elapsed time / LONGEST.
def test_a_whole_day_block_may_not_exceed_366_days(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    ok = day_block(owner, only_a_member, first_day="2027-10-30", last_day="2028-10-29")
    assert ok.status_code == 201

    too_long = day_block(owner, only_a_member, first_day="2027-10-30", last_day="2028-10-30")
    assert (too_long.status_code, too_long.json()["code"]) == (422, "time_off_too_long")


# 19. fence: PATCH kind-switching. Kills a naive `before | change` merge (both pairs set would hit
# ck_time_off_kind and 500).
def test_patch_switches_kind_and_merges_within_a_kind(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    day_row = day_block(owner, only_a_member, first_day="2026-10-10", last_day="2026-10-12").json()
    day_id = day_row["id"]

    moved_end = owner.patch(block_path(day_id), json={"last_day": "2026-10-15"})
    assert moved_end.status_code == 200
    assert moved_end.json()["first_day"] == "2026-10-10"
    assert moved_end.json()["last_day"] == "2026-10-15"

    switched_to_partial = owner.patch(
        block_path(day_id),
        json={"starts_at": "2026-10-20T08:00:00Z", "ends_at": "2026-10-20T09:00:00Z"},
    )
    assert switched_to_partial.status_code == 200
    assert switched_to_partial.json()["first_day"] is None
    assert switched_to_partial.json()["last_day"] is None
    assert switched_to_partial.json()["starts_at"] == "2026-10-20T08:00:00Z"

    partial_id = switched_to_partial.json()["id"]
    half_pair_on_partial = owner.patch(block_path(partial_id), json={"last_day": "2026-10-25"})
    assert half_pair_on_partial.status_code == 422

    switched_to_day = owner.patch(
        block_path(partial_id),
        json={"first_day": "2026-11-01", "last_day": "2026-11-03"},
    )
    assert switched_to_day.status_code == 200
    assert switched_to_day.json()["starts_at"] is None
    assert switched_to_day.json()["ends_at"] is None
    assert switched_to_day.json()["first_day"] == "2026-11-01"

    day_id = switched_to_day.json()["id"]
    both_kinds = owner.patch(
        block_path(day_id),
        json={"starts_at": "2026-10-20T08:00:00Z", "first_day": "2026-11-01"},
    )
    assert both_kinds.status_code == 422

    # Half the instant pair on a day block: switching kind needs the complete new pair, never a
    # naive merge that would leave both pairs non-null and hit ck_time_off_kind (a 500).
    half_instant_pair_on_day = owner.patch(
        block_path(day_id), json={"starts_at": "2026-10-20T08:00:00Z"}
    )
    assert half_instant_pair_on_day.status_code == 422

    kept_kind = owner.patch(block_path(day_id), json={"reason": "Renamed"})
    assert kept_kind.status_code == 200
    assert kept_kind.json()["first_day"] == "2026-11-01"
    assert kept_kind.json()["reason"] == "Renamed"

    before = events(migrate_engine, tenant_id=people.a, action="time_off_changed")
    noop = owner.patch(block_path(day_id), json={})
    assert noop.status_code == 200
    same_values = owner.patch(
        block_path(day_id), json={"first_day": "2026-11-01", "last_day": "2026-11-03"}
    )
    assert same_values.status_code == 200
    after = events(migrate_engine, tenant_id=people.a, action="time_off_changed")
    assert len(after) == len(before)


# 20. fence: list window edges, half-open on the instant side, inclusive by local date on the day
# side. Kills to.astimezone(tz).date() without the 1 microsecond. The business's default timezone
# is Europe/Amsterdam (UTC+2 in early October): local midnight starting 2026-10-03 is
# 2026-10-02T22:00:00Z, and local midnight starting 2026-10-02 is 2026-10-01T22:00:00Z.
def test_list_window_edges_for_a_whole_day_block(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    day_d_minus_1 = day_block(
        owner, only_a_member, first_day="2026-10-02", last_day="2026-10-02"
    ).json()["id"]
    day_d = day_block(owner, only_a_member, first_day="2026-10-03", last_day="2026-10-03").json()[
        "id"
    ]

    # `to` is local midnight starting day D: D is excluded, D-1 is included.
    midnight_d = {"from": "2026-09-25T00:00:00Z", "to": "2026-10-02T22:00:00Z"}
    result = owner.get(path(only_a_member), params=midnight_d).json()
    assert [b["id"] for b in result] == [day_d_minus_1]

    # `from` is inside day D's own last day (still local 2026-10-03, though past UTC midnight).
    from_inside_last_day = {"from": "2026-10-03T12:00:00Z", "to": "2026-10-05T00:00:00Z"}
    included = owner.get(path(only_a_member), params=from_inside_last_day).json()
    assert [b["id"] for b in included] == [day_d]

    # `to` is UTC midnight starting day D, two hours EARLIER than day D's own local midnight
    # (2026-10-02T22:00:00Z): at that instant the business's local wall clock still reads
    # 2026-10-03T01:59:59.999999 (Amsterdam, UTC+2), so day D is still the correct local date and
    # must be included. Kills computing the window edge from the UTC calendar date instead of the
    # zone-converted one.
    utc_midnight_of_d = {"from": "2026-09-25T00:00:00Z", "to": "2026-10-03T00:00:00Z"}
    result_utc_midnight = owner.get(path(only_a_member), params=utc_midnight_of_d).json()
    assert day_d in [b["id"] for b in result_utc_midnight]


# 21. fence: list order by effective start (COALESCE(starts_at, midnight(first_day)) done in
# Python, never in SQL: AT TIME ZONE is banned). Never assert the server's own TimeZone default
# here: a correctly-behaving server on a non-UTC default must still pass this test. A SQL-side
# ORDER BY COALESCE regression is instead pinned with `SET LOCAL TimeZone = 'UTC'` at the point the
# wrong implementation is injected (see the spec's fence table), so that check doesn't leak into
# the committed test or constrain what a real deployment's server may be configured to.
def test_list_order_is_by_effective_start_amsterdam(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    put_settings(owner, {"timezone": "Europe/Amsterdam"})
    only_a_member = member_id(people.a, people.only_a)

    partial_10_02 = block(
        owner, only_a_member, starts_at="2026-10-02T07:00:00Z", ends_at="2026-10-02T08:00:00Z"
    ).json()["id"]
    day_10_03 = day_block(
        owner, only_a_member, first_day="2026-10-03", last_day="2026-10-03"
    ).json()["id"]
    # 00:30 local on 2026-10-03, i.e. 2026-10-02T22:30Z: before the day block's own effective start
    # (2026-10-02T22:00Z) but after the earlier partial block.
    partial_10_03_early = block(
        owner, only_a_member, starts_at="2026-10-02T22:30:00Z", ends_at="2026-10-02T23:00:00Z"
    ).json()["id"]
    partial_10_04 = block(
        owner, only_a_member, starts_at="2026-10-04T07:00:00Z", ends_at="2026-10-04T08:00:00Z"
    ).json()["id"]

    listed = owner.get(
        path(only_a_member),
        params={"from": "2026-10-01T00:00:00Z", "to": "2026-10-10T00:00:00Z"},
    ).json()

    assert [b["id"] for b in listed] == [
        partial_10_02,
        day_10_03,
        partial_10_03_early,
        partial_10_04,
    ]


# 22. guard: reason visibility, Google rows read-only, audit events and member-removal cascade are
# the same for a day row as for a partial one.
def test_a_day_block_behaves_like_a_partial_one_for_visibility_audit_and_cascade(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    created = day_block(owner, both_member).json()  # the owner's own block
    wide = {"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"}

    as_worker = worker.get(path(both_member), params=wide)
    assert as_worker.json()[0]["reason"] is None  # not the block's own member, not an owner

    as_owner = owner.get(path(both_member), params=wide)
    assert as_owner.json()[0]["reason"] == "Holiday"

    cascaded = day_block(owner, only_a_member).json()  # this one belongs to the removed member

    google_id = google(people.a, only_a_member, external_id="day-evt")
    with tenant_context(people.a) as session:
        session.execute(
            text(
                "UPDATE time_off SET first_day = '2026-12-01', last_day = '2026-12-02', "
                "starts_at = NULL, ends_at = NULL WHERE id = :id"
            ),
            {"id": google_id},
        )
    assert owner.patch(block_path(google_id), json={"reason": "nope"}).status_code == 404
    assert owner.request("DELETE", block_path(google_id), json={}).status_code == 404

    before = events(migrate_engine, tenant_id=people.a, action="time_off_created")
    assert len(before) >= 1

    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM memberships WHERE id = :id"), {"id": only_a_member})
        removed = session.execute(
            text("SELECT count(*) FROM time_off WHERE id = :id"), {"id": cascaded["id"]}
        ).scalar()
        untouched = session.execute(
            text("SELECT count(*) FROM time_off WHERE id = :id"), {"id": created["id"]}
        ).scalar()
    assert removed == 0
    assert untouched == 1
