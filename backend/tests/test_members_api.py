"""/api/members: owners list, promote, demote and remove members; anyone sets their own
display name."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import auth
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_membership,
    add_password,
    email_of,
    events,
    failing,
    member_id,
    new_client,
    set_role,
    signed_in,
    wait_until_blocked,
)
from tests.test_availability_api import new_service
from tests.test_bookings_db import book, seed_service
from tests.test_clients_db import seed_client
from tests.test_invites_db import call, digest, minted


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def unauthenticated(response: Response) -> bool:
    return response.status_code == 401 and response.json() == {"code": "unauthenticated"}


# The listing, ordered by member id, scoped to the business, with the joined email.
def test_the_owner_lists_members_of_their_business(people: People, app: FastAPI) -> None:
    # A third member, added after the other two, so an ordering by email or by user id (both
    # assigned in a different sequence than the memberships below) would be caught.
    add_membership(people.a, people.only_b)
    owner = signed_in(app, people.a, people.both)

    response = owner.get("/api/members")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    expected = sorted(
        [
            {
                "member_id": str(member_id(people.a, people.only_a)),
                "user_id": str(people.only_a),
                "email": email_of(people.only_a),
                "role": "worker",
                "display_name": None,
            },
            {
                "member_id": str(member_id(people.a, people.both)),
                "user_id": str(people.both),
                "email": email_of(people.both),
                "role": "owner",
                "display_name": None,
            },
            {
                "member_id": str(member_id(people.a, people.only_b)),
                "user_id": str(people.only_b),
                "email": email_of(people.only_b),
                "role": "worker",
                "display_name": None,
            },
        ],
        key=lambda m: str(m["member_id"]),
    )
    assert response.json() == expected


# A worker is refused every endpoint, whatever it sends.
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/members", None),
        ("PATCH", "/api/members/{both}", {"role": "worker"}),
        ("PATCH", "/api/members/{both}", {"role": "nope"}),
        ("PATCH", "/api/members/{both}", []),
        ("PATCH", "/api/members/not-a-uuid", {"role": "worker"}),
        ("DELETE", "/api/members/{both}", {}),
        ("DELETE", "/api/members/{both}", []),
    ],
)
def test_a_worker_is_refused_every_members_endpoint(
    people: People, app: FastAPI, method: str, path: str, body: Any
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    owner_session = signed_in(app, people.a, people.both)
    assert owner_session.get("/api/session").status_code == 200
    path = path.format(both=member_id(people.a, people.both))

    response = worker.request(method, path, json=body)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    with tenant_context(people.a) as session:
        roles: dict[uuid.UUID, str] = dict(
            session.execute(text("SELECT user_id, role FROM memberships")).all()
        )
    assert roles == {people.only_a: "worker", people.both: "owner"}
    assert owner_session.get("/api/session").status_code == 200


# No cookie is 401 on every endpoint; a non-JSON write is 415 before auth.
def test_without_a_cookie_every_endpoint_is_unauthenticated(people: People, app: FastAPI) -> None:
    client = new_client(app)
    both_member = member_id(people.a, people.both)

    for method, path, body in [
        ("GET", "/api/members", None),
        ("PATCH", f"/api/members/{both_member}", {"role": "worker"}),
        ("DELETE", f"/api/members/{both_member}", {}),
        ("PUT", f"/api/members/{both_member}/display-name", {"display_name": "Ada"}),
    ]:
        response = client.request(method, path, json=body)
        assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})


@pytest.mark.parametrize(
    ("method", "suffix"), [("PATCH", ""), ("DELETE", ""), ("PUT", "/display-name")]
)
def test_a_non_json_write_is_refused_before_auth(
    people: People, app: FastAPI, method: str, suffix: str
) -> None:
    # No cookie at all: this proves 415 fires before auth, not merely before an owner check.
    client = new_client(app)
    only_a_member = member_id(people.a, people.only_a)

    response = client.request(
        method,
        f"/api/members/{only_a_member}{suffix}",
        content="x",
        headers={"content-type": "text/plain"},
    )

    assert (response.status_code, response.json()) == (415, {"code": "unsupported_media_type"})
    with tenant_context(people.a) as session:
        roles: dict[uuid.UUID, str] = dict(
            session.execute(text("SELECT user_id, role FROM memberships")).all()
        )
    assert roles == {people.only_a: "worker", people.both: "owner"}


# Demoting ends the member's sessions in this business only.
def test_demoting_a_member_ends_their_sessions_in_this_business_only(
    people: People, app: FastAPI
) -> None:
    set_role(people.a, people.only_a, "owner")
    both_in_a = signed_in(app, people.a, people.both)
    both_in_b = signed_in(app, people.b, people.both)
    owner = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    response = owner.patch(f"/api/members/{both_member}", json={"role": "worker"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["role"] == "worker"
    assert unauthenticated(both_in_a.get("/api/session"))
    assert both_in_b.get("/api/session").status_code == 200
    new_session = signed_in(app, people.a, people.both)
    assert new_session.get("/api/session").json()["role"] == "worker"


# Removing ends the member's sessions in this business only (the DELETE twin of the above).
def test_removing_a_member_ends_their_sessions_in_this_business_only(
    people: People, app: FastAPI
) -> None:
    set_role(people.a, people.only_a, "owner")
    both_in_a = signed_in(app, people.a, people.both)
    both_in_b = signed_in(app, people.b, people.both)
    owner = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    response = owner.request("DELETE", f"/api/members/{both_member}", json={})

    assert response.status_code == 204
    assert unauthenticated(both_in_a.get("/api/session"))
    assert both_in_b.get("/api/session").status_code == 200


# Deterministic race: two owners acting on each other serialize through target()'s
# caller-plus-target locks, taken in one ORDER BY id statement.
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_two_owners_acting_on_each_other_serialize_through_the_member_locks(
    people: People, app: FastAPI, app_engine: Engine, migrate_engine: Engine, method: str
) -> None:
    set_role(people.a, people.only_a, "owner")
    both_client = signed_in(app, people.a, people.both)
    only_a_client = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    results: list[object] = []

    def act(client: TestClient, member: uuid.UUID) -> None:
        try:
            if method == "PATCH":
                response = client.patch(f"/api/members/{member}", json={"role": "worker"})
            else:
                response = client.request("DELETE", f"/api/members/{member}", json={})
            results.append(response)
        except Exception as error:  # would be a bug in the endpoint, not the expected outcome
            results.append(error)

    with app_engine.begin() as holder:
        holder.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        holder.execute(text("SELECT FROM memberships FOR UPDATE"))
        threads = [
            threading.Thread(target=act, args=(both_client, only_a_member)),
            threading.Thread(target=act, args=(only_a_client, both_member)),
        ]
        for thread in threads:
            thread.start()
        wait_until_blocked(app_engine, 2)
        # holder commits here, releasing both memberships and letting the two requests proceed.
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)

    assert [isinstance(r, Exception) for r in results] == [False, False]
    responses = [r for r in results if isinstance(r, Response)]
    statuses = sorted(r.status_code for r in responses)
    with tenant_context(people.a) as session:
        remaining = session.execute(
            text("SELECT user_id, role FROM memberships WHERE role <> 'removed'")
        ).all()

    if method == "DELETE":
        assert statuses == [204, 401]
        assert len(remaining) == 1
        assert remaining[0].role == "owner"
        assert len(events(migrate_engine, tenant_id=people.a, action="member_removed")) == 1
    else:
        assert statuses == [200, 403]
        forbidden = next(r for r in responses if r.status_code == 403)
        assert forbidden.json() == {"code": "owner_only"}
        owners = [row.user_id for row in remaining if row.role == "owner"]
        assert len(owners) == 1
        assert len(events(migrate_engine, tenant_id=people.a, action="member_role_changed")) == 1


# Deterministic race: a stale owner is rechecked after the lock, not before.
@pytest.mark.parametrize("holder_action", ["demoted", "ended"])
def test_a_stale_owner_is_rechecked_after_the_lock(
    people: People, app: FastAPI, app_engine: Engine, migrate_engine: Engine, holder_action: str
) -> None:
    # The holder locks only both's row, never the whole table (the test passes whichever of the
    # two rows target()'s "ORDER BY m.id FOR UPDATE" happens to lock first): it blocks on both's
    # row regardless, since that is the one row the holder holds.
    set_role(people.a, people.only_a, "owner")  # so the holder's change below passes the trigger
    both_client = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    both_member = member_id(people.a, people.both)
    results: list[object] = []

    def promote() -> None:
        try:
            results.append(
                both_client.patch(f"/api/members/{only_a_member}", json={"role": "owner"})
            )
        except Exception as error:  # would be a bug, not the expected outcome
            results.append(error)

    with app_engine.begin() as holder:
        holder.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        holder.execute(
            text("SELECT FROM memberships WHERE id = :id FOR UPDATE"), {"id": both_member}
        )
        thread = threading.Thread(target=promote)
        thread.start()
        wait_until_blocked(app_engine, 1)
        holder.execute(
            text("DELETE FROM sessions WHERE tenant_id = :t AND user_id = :u"),
            {"t": people.a, "u": people.both},
        )
        if holder_action == "demoted":
            holder.execute(
                text("UPDATE memberships SET role = 'worker' WHERE id = :id"), {"id": both_member}
            )
        else:
            holder.execute(
                text("UPDATE memberships SET role = 'removed', removed_at = now() WHERE id = :id"),
                {"id": both_member},
            )
        # holder commits here.
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert len(results) == 1
    response = results[0]
    assert isinstance(response, Response)
    if holder_action == "demoted":
        assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    else:
        assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_a}
        )
    assert role == "owner"  # unchanged by the blocked PATCH
    assert events(migrate_engine, tenant_id=people.a, action="member_role_changed") == []


# Promoting a member rotates their session and grants owner access immediately.
def test_promoting_a_member_rotates_their_session(people: People, app: FastAPI) -> None:
    only_a_client = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.patch(f"/api/members/{only_a_member}", json={"role": "owner"})

    assert response.status_code == 200
    assert response.json()["role"] == "owner"
    assert unauthenticated(only_a_client.get("/api/session"))
    new_session = signed_in(app, people.a, people.only_a)
    assert new_session.get("/api/session").json()["role"] == "owner"
    assert new_session.get("/api/members").status_code == 200


# The unchanged-role no-op doesn't rotate sessions or record anything.
def test_setting_the_same_role_is_a_no_op(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    worker_client = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.patch(f"/api/members/{only_a_member}", json={"role": "worker"})

    assert response.status_code == 200
    assert response.json() == {
        "member_id": str(only_a_member),
        "user_id": str(people.only_a),
        "email": email_of(people.only_a),
        "role": "worker",
        "display_name": None,
    }
    assert worker_client.get("/api/session").status_code == 200
    assert events(migrate_engine, tenant_id=people.a, action="member_role_changed") == []


# Removal ends the membership, keeps the row, and ends the member's sessions.
def test_removing_a_member_ends_the_membership_not_the_account(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    only_a_client = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

    assert response.status_code == 204
    assert response.content == b""
    assert unauthenticated(only_a_client.get("/api/session"))
    listing = owner.get("/api/members")
    assert listing.status_code == 200
    assert str(people.only_a) not in {m["user_id"] for m in listing.json()}
    with migrate_engine.connect() as conn:
        alive = conn.scalar(text("SELECT count(*) FROM users WHERE id = :u"), {"u": people.only_a})
    assert alive == 1
    assert removed_row(people.a, only_a_member).role == "removed"


def removed_row(tenant_id: uuid.UUID, member: uuid.UUID) -> Any:
    with tenant_context(tenant_id) as session:
        return session.execute(
            text("SELECT role, removed_at FROM memberships WHERE id = :id"), {"id": member}
        ).one()


def delete(client: TestClient, member: uuid.UUID) -> Response:
    return client.request("DELETE", f"/api/members/{member}", json={})


# 4: a removed member cannot be changed or removed again.
def test_a_removed_member_cannot_be_changed_or_removed_again(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    assert delete(owner, only_a_member).status_code == 204

    patched = owner.patch(f"/api/members/{only_a_member}", json={"role": "worker"})
    again = delete(owner, only_a_member)

    assert (patched.status_code, patched.json()) == (404, {"code": "not_found"})
    assert (again.status_code, again.json()) == (404, {"code": "not_found"})
    assert removed_row(people.a, only_a_member).role == "removed"
    assert len(events(migrate_engine, tenant_id=people.a, action="member_removed")) == 1


# 5: a removed member gets no hours, time off, name or services.
def test_a_removed_member_gets_no_hours_time_off_name_or_services(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    service_id = new_service(owner)
    assert delete(owner, only_a_member).status_code == 204
    hours = f"/api/members/{only_a_member}/working-hours"

    answers = [
        owner.get(hours),
        owner.put(hours, json=[{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]),
        owner.post(
            f"/api/members/{only_a_member}/time-off",
            json={"starts_at": "2026-10-01T08:00:00Z", "ends_at": "2026-10-03T08:00:00Z"},
        ),
        owner.put(f"/api/members/{only_a_member}/display-name", json={"display_name": "Ada"}),
    ]

    for answer in answers:
        assert (answer.status_code, answer.json()) == (404, {"code": "not_found"})
    assigned = owner.put(f"/api/services/{service_id}/workers", json=[str(only_a_member)])
    assert (assigned.status_code, assigned.json()) == (422, {"code": "unknown_member"})


# 6: removal touches nothing outside this membership.
def test_removal_touches_nothing_outside_this_membership(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_role(people.a, people.only_a, "owner")
    seed_client(people.a, user_id=people.both)
    seed_client(people.b, user_id=people.both)
    both_in_b = signed_in(app, people.b, people.both)
    owner = signed_in(app, people.a, people.only_a)

    def snapshot() -> list[Any]:
        out: list[Any] = []
        with migrate_engine.begin() as conn:
            out.append(
                conn.execute(text("SELECT * FROM users WHERE id = :u"), {"u": people.both}).all()
            )
            for tenant_id in (people.a, people.b):
                conn.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
                )
                out.append(conn.execute(text("SELECT * FROM clients ORDER BY id")).all())
            out.append(
                conn.execute(
                    text("SELECT * FROM memberships WHERE tenant_id = :t ORDER BY id"),
                    {"t": people.b},
                ).all()
            )
        return out

    before = snapshot()
    assert delete(owner, member_id(people.a, people.both)).status_code == 204

    assert snapshot() == before
    assert both_in_b.get("/api/session").status_code == 200


# ZIF-112 30. fence: removal drops the worker's booking holds (0034), and only theirs. Kills: the
# DELETE FROM booking_holds going missing. Positive control: the other worker's hold stays.
def test_removing_a_member_deletes_their_booking_holds(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_role(people.a, people.only_a, "owner")
    service = seed_service(people.a)
    start = datetime.now(UTC) + timedelta(days=3)
    held: dict[str, uuid.UUID] = {}
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        for who in (people.both, people.only_a):
            held[str(who)] = conn.scalar(
                text(
                    "INSERT INTO booking_holds (tenant_id, service_id, worker_id, anyone,"
                    " starts_at, ends_at, email, secret_hash, expires_at)"
                    " VALUES (:t, :s, :w, false, :a, :b, :e, :h, now() + interval '1 hour')"
                    " RETURNING id"
                ),
                {
                    "t": people.a,
                    "s": service,
                    "w": member_id(people.a, who),
                    "a": start,
                    "b": start + timedelta(hours=1),
                    "e": f"{uuid.uuid4()}@example.com",
                    "h": uuid.uuid4().bytes * 2,
                },
            )

    def holds() -> set[uuid.UUID]:
        with migrate_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
            return set(conn.scalars(text("SELECT id FROM booking_holds")))

    assert holds() == set(held.values())
    owner = signed_in(app, people.a, people.only_a)
    assert delete(owner, member_id(people.a, people.both)).status_code == 204

    assert holds() == {held[str(people.only_a)]}


# ZIF-112 31. guard: signing in again with a same-business cookie while that person is being removed
# must not deadlock. The holder does what remove() does (member row FOR UPDATE, then delete their
# sessions, then end the row) once the sign-in is waiting on the member row. Kills: start() deleting
# the old cookie's session BEFORE create(): the sign-in then holds that row while it waits, the
# removal waits on it, and Postgres aborts one of them (40P01).
def test_signing_in_with_an_old_cookie_does_not_deadlock_with_a_removal(
    people: People, app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    set_role(people.a, people.both, "owner")
    add_password(migrate_engine, people.only_a, PASSWORD)
    client = signed_in(app, people.a, people.only_a)
    results: list[object] = []

    def sign_in() -> None:
        try:
            results.append(
                client.post(
                    "/api/session", json={"email": email_of(people.only_a), "password": PASSWORD}
                )
            )
        except Exception as error:
            results.append(error)

    thread = threading.Thread(target=sign_in)
    with app_engine.begin() as holder:
        holder.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        holder.execute(
            text("SELECT 1 FROM memberships WHERE user_id = :u FOR UPDATE"), {"u": people.only_a}
        )
        thread.start()
        wait_until_blocked(app_engine, 1)
        holder.execute(
            text("DELETE FROM sessions WHERE tenant_id = :t AND user_id = :u"),
            {"t": people.a, "u": people.only_a},
        )
        holder.execute(
            text("UPDATE memberships SET role = 'removed', removed_at = now() WHERE user_id = :u"),
            {"u": people.only_a},
        )
    thread.join(timeout=10)
    assert not thread.is_alive()

    # The sign-in lost the race cleanly: its membership was gone when it got the row.
    assert [r.status_code if isinstance(r, Response) else r for r in results] == [403]


# 8: a removal takes the booking lock before any member row.
def test_a_removal_takes_the_booking_lock_before_any_member_row(
    people: People, app: FastAPI, app_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    ids = [member_id(people.a, people.both), member_id(people.a, people.only_a)]
    results: list[object] = []

    def remove() -> None:
        try:
            results.append(delete(owner, ids[1]))
        except Exception as error:  # would be a bug, not the expected outcome
            results.append(error)

    with app_engine.begin() as holder:
        holder.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        holder.execute(
            text("SELECT pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')))")
        )
        thread = threading.Thread(target=remove)
        thread.start()
        wait_until_blocked(app_engine, 1)
        # Blocked on lock 51 and holding no member row yet: NOWAIT must get both.
        holder.execute(
            text("SELECT FROM memberships WHERE id = ANY(:ids) FOR UPDATE NOWAIT"), {"ids": ids}
        )
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert len(results) == 1
    assert isinstance(results[0], Response)
    assert results[0].status_code == 204


# A member id outside the business, or a random one, is 404, never a leak.
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.parametrize("bogus", ["other_business", "random"])
def test_a_member_outside_the_business_is_not_found(
    people: People, app: FastAPI, migrate_engine: Engine, method: str, bogus: str
) -> None:
    only_b_client = signed_in(app, people.b, people.only_b)
    owner = signed_in(app, people.a, people.both)
    target_id = member_id(people.b, people.only_b) if bogus == "other_business" else uuid.uuid7()

    if method == "PATCH":
        response = owner.patch(f"/api/members/{target_id}", json={"role": "owner"})
    else:
        response = owner.request("DELETE", f"/api/members/{target_id}", json={})

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    with tenant_context(people.b) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert role == "worker"
    assert only_b_client.get("/api/session").status_code == 200
    assert events(migrate_engine, tenant_id=people.a, action="member_role_changed") == []
    assert events(migrate_engine, tenant_id=people.a, action="member_removed") == []


# An owner can't change or remove their own membership through these endpoints.
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_an_owner_cannot_change_their_own_membership(
    people: People, app: FastAPI, method: str
) -> None:
    set_role(people.a, people.only_a, "owner")
    both_client = signed_in(app, people.a, people.both)
    both_member = member_id(people.a, people.both)

    if method == "PATCH":
        response = both_client.patch(f"/api/members/{both_member}", json={"role": "worker"})
    else:
        response = both_client.request("DELETE", f"/api/members/{both_member}", json={})

    assert (response.status_code, response.json()) == (403, {"code": "own_membership"})
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.both}
        )
    assert role == "owner"
    assert both_client.get("/api/session").status_code == 200


# A malformed role, extra keys, or a non-UUID path are all 422, nothing changed.
@pytest.mark.parametrize(
    "body",
    [
        {"role": "admin"},
        {"role": "Owner"},
        {"role": None},
        {"role": 1},
        {},
        {"role": "owner", "user_id": "will-be-filled"},
        [],
    ],
)
def test_a_malformed_role_change_is_refused(people: People, app: FastAPI, body: Any) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    if isinstance(body, dict) and body.get("user_id") == "will-be-filled":
        body = {**body, "user_id": str(people.both)}

    response = owner.patch(f"/api/members/{only_a_member}", json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_a}
        )
    assert role == "worker"


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_a_non_uuid_path_is_refused(people: People, app: FastAPI, method: str) -> None:
    owner = signed_in(app, people.a, people.both)

    if method == "PATCH":
        response = owner.patch("/api/members/not-a-uuid", json={"role": "owner"})
    else:
        response = owner.request("DELETE", "/api/members/not-a-uuid", json={})

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})


# A role change is recorded with the user's id, and the old and new role.
def test_a_role_change_is_recorded_with_the_users_id_and_old_new_values(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    assert owner.patch(f"/api/members/{only_a_member}", json={"role": "owner"}).status_code == 200

    recorded = events(migrate_engine, tenant_id=people.a, action="member_role_changed")
    assert len(recorded) == 1
    assert recorded[0]["actor_user_id"] == people.both
    assert recorded[0]["target"] == f"user:{people.only_a}"
    assert recorded[0]["details"] == {"old": "worker", "new": "owner"}


# A removal is recorded with the user's id and no details.
def test_a_removal_is_recorded_with_no_details(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    assert owner.request("DELETE", f"/api/members/{only_a_member}", json={}).status_code == 204

    recorded = events(migrate_engine, tenant_id=people.a, action="member_removed")
    assert len(recorded) == 1
    assert recorded[0]["actor_user_id"] == people.both
    assert recorded[0]["target"] == f"user:{people.only_a}"
    assert recorded[0]["details"] is None


# A change whose event fails to record leaves nothing changed.
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_a_change_whose_event_fails_leaves_nothing_changed(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    only_a_client = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    failing(monkeypatch, auth, "record")

    if method == "PATCH":
        response = owner.patch(f"/api/members/{only_a_member}", json={"role": "owner"})
    else:
        response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

    assert (response.status_code, response.json()) == (500, {"code": "internal"})
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_a}
        )
    assert role == "worker"
    assert only_a_client.get("/api/session").status_code == 200


# The 409 mapping matches only the trigger's own last_owner constraint.
@pytest.mark.parametrize(("constraint", "expected"), [("last_owner", 409), ("other_rule", 500)])
def test_the_409_mapping_only_matches_last_owner(
    people: People, app: FastAPI, migrate_engine: Engine, constraint: str, expected: int
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    with migrate_engine.begin() as conn:
        conn.execute(
            text(f"""
            CREATE FUNCTION zz_refuse() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'refused for testing'
                USING ERRCODE = '23514', CONSTRAINT = '{constraint}';
            END $$;
            CREATE TRIGGER zz_refuse BEFORE UPDATE ON memberships
            FOR EACH ROW EXECUTE FUNCTION zz_refuse()
            """)
        )
    try:
        response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

        expected_body = {"code": "last_owner" if constraint == "last_owner" else "internal"}
        assert (response.status_code, response.json()) == (expected, expected_body)
        with tenant_context(people.a) as session:
            still_there = session.scalar(
                text("SELECT count(*) FROM memberships WHERE user_id = :u AND role = 'worker'"),
                {"u": people.only_a},
            )
        assert still_there == 1
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DROP TRIGGER zz_refuse ON memberships"))
            conn.execute(text("DROP FUNCTION zz_refuse()"))


# PUT /api/members/{member_id}/display-name (ZIF-97): the name clients see when booking.


def stored_name(tenant_id: uuid.UUID, member: uuid.UUID) -> str | None:
    with tenant_context(tenant_id) as session:
        name: str | None = session.scalar(
            text("SELECT display_name FROM memberships WHERE id = :id"), {"id": member}
        )
        return name


def test_an_owner_sets_and_clears_another_member_s_display_name(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(
        f"/api/members/{only_a_member}/display-name", json={"display_name": "  Ada Lovelace  "}
    )

    assert (response.status_code, response.json()) == (200, {"display_name": "Ada Lovelace"})
    assert stored_name(people.a, only_a_member) == "Ada Lovelace"
    listed = {m["member_id"]: m["display_name"] for m in owner.get("/api/members").json()}
    assert listed[str(only_a_member)] == "Ada Lovelace"

    cleared = owner.put(f"/api/members/{only_a_member}/display-name", json={"display_name": None})

    assert (cleared.status_code, cleared.json()) == (200, {"display_name": None})
    assert stored_name(people.a, only_a_member) is None
    listed = {m["member_id"]: m["display_name"] for m in owner.get("/api/members").json()}
    assert listed[str(only_a_member)] is None


def test_an_owner_sets_their_own_display_name(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    both_member = member_id(people.a, people.both)

    response = owner.put(f"/api/members/{both_member}/display-name", json={"display_name": "Ada"})

    assert (response.status_code, response.json()) == (200, {"display_name": "Ada"})
    assert stored_name(people.a, both_member) == "Ada"


def test_a_worker_sets_and_reads_back_their_own_display_name(people: People, app: FastAPI) -> None:
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)

    response = worker.put(
        f"/api/members/{only_a_member}/display-name", json={"display_name": "Ada"}
    )

    assert (response.status_code, response.json()) == (200, {"display_name": "Ada"})
    assert worker.get("/api/session").json()["display_name"] == "Ada"


def test_the_display_name_answer_never_carries_the_email(people: People, app: FastAPI) -> None:
    worker = signed_in(app, people.a, people.only_a)
    only_a_member = member_id(people.a, people.only_a)

    response = worker.put(
        f"/api/members/{only_a_member}/display-name", json={"display_name": "Ada"}
    )

    assert set(response.json()) == {"display_name"}
    assert response.headers["cache-control"] == "no-store"
    assert email_of(people.only_a) not in response.text
    assert str(people.only_a) not in response.text


def test_a_third_member_may_not_touch_another_s_display_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    both_member = member_id(people.a, people.both)

    response = worker.put(f"/api/members/{both_member}/display-name", json={"display_name": "Ada"})

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert stored_name(people.a, both_member) is None
    assert events(migrate_engine, tenant_id=people.a, action="member_display_name_changed") == []


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": "x" * 61},
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": " "},  # NBSP: strips to empty
        {"display_name": "Ada‍Lovelace"},  # ZWJ
        {"display_name": "Ada Lovelace"},  # U+2028 line separator
        {"display_name": "\x00"},
        {"display_name": 5},
        {"display_name": True},
        {},
        {"display_name": "Ada", "role": "owner"},
    ],
)
def test_a_display_name_must_be_one_to_sixty_printable_characters(
    people: People, app: FastAPI, body: Any
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(f"/api/members/{only_a_member}/display-name", json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored_name(people.a, only_a_member) is None


def test_sixty_characters_is_accepted_and_the_edges_are_trimmed(
    people: People, app: FastAPI
) -> None:
    # The other side of the validator, kept out of the parametrized test above so that a failure
    # there names the case that broke rather than these two writes at its tail.
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    at_max = owner.put(
        f"/api/members/{only_a_member}/display-name", json={"display_name": "x" * 60}
    )
    assert (at_max.status_code, at_max.json()) == (200, {"display_name": "x" * 60})

    trimmed = owner.put(
        f"/api/members/{only_a_member}/display-name", json={"display_name": "  Ada  "}
    )
    assert (trimmed.status_code, trimmed.json()) == (200, {"display_name": "Ada"})
    assert stored_name(people.a, only_a_member) == "Ada"


def test_the_display_name_event_names_no_name(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    assert (
        owner.put(
            f"/api/members/{only_a_member}/display-name", json={"display_name": "Ada Lovelace"}
        ).status_code
        == 200
    )
    assert (
        owner.put(
            f"/api/members/{only_a_member}/display-name", json={"display_name": None}
        ).status_code
        == 200
    )

    recorded = events(migrate_engine, tenant_id=people.a, action="member_display_name_changed")
    assert len(recorded) == 2
    assert recorded[0]["target"] == f"user:{people.only_a}"
    assert recorded[1]["target"] == f"user:{people.only_a}"
    assert recorded[0]["details"] == {"display_name": "set"}
    assert recorded[1]["details"] == {"display_name": "cleared"}
    assert "Ada Lovelace" not in json.dumps(recorded, default=str)


def test_setting_the_same_display_name_records_nothing(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    assert (
        owner.put(
            f"/api/members/{only_a_member}/display-name", json={"display_name": "Ada"}
        ).status_code
        == 200
    )
    again = owner.put(f"/api/members/{only_a_member}/display-name", json={"display_name": "Ada"})
    assert (again.status_code, again.json()) == (200, {"display_name": "Ada"})
    assert (
        len(events(migrate_engine, tenant_id=people.a, action="member_display_name_changed")) == 1
    )

    assert (
        owner.put(
            f"/api/members/{only_a_member}/display-name", json={"display_name": None}
        ).status_code
        == 200
    )
    again_null = owner.put(
        f"/api/members/{only_a_member}/display-name", json={"display_name": None}
    )
    assert (again_null.status_code, again_null.json()) == (200, {"display_name": None})
    assert (
        len(events(migrate_engine, tenant_id=people.a, action="member_display_name_changed")) == 2
    )


def test_a_member_of_another_business_is_not_found(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    other_business_member = member_id(people.b, people.only_b)

    response = owner.put(
        f"/api/members/{other_business_member}/display-name", json={"display_name": "Ada"}
    )

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    assert stored_name(people.b, other_business_member) is None

    unknown = owner.put(f"/api/members/{uuid.uuid7()}/display-name", json={"display_name": "Ada"})
    assert (unknown.status_code, unknown.json()) == (404, {"code": "not_found"})


def test_a_worker_probing_another_business_is_not_found_not_forbidden(
    people: People, app: FastAPI
) -> None:
    # The actor must be a worker: for an owner may_manage is always True, so only a worker tells
    # 404-before-403 from 403-before-404. Membership probing is what the order prevents
    # (CONTRIBUTING.md:84-86).
    worker = signed_in(app, people.a, people.only_a)
    other_business_member = member_id(people.b, people.only_b)

    response = worker.put(
        f"/api/members/{other_business_member}/display-name", json={"display_name": "Ada"}
    )

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    assert stored_name(people.b, other_business_member) is None


# 1: the row stays, so booking history still resolves (ZIF-112). History names a removed member by
# their membership's display name; with none (cleared) they are unnamed: the business no longer
# reads their account, so its name is no fallback (ZB-32). While they are a member, it is.
@pytest.mark.parametrize(
    ("display_name", "removed_actor_name"),
    [("Worked As", "Worked As"), (None, None)],
    ids=["display_name", "cleared"],
)
def test_removing_a_member_with_past_bookings_keeps_the_row_and_the_history(
    people: People, app: FastAPI, display_name: str | None, removed_actor_name: str | None
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE memberships SET display_name = :n WHERE id = :i"),
            {"n": display_name, "i": only_a_member},
        )
    booking_id = book(
        people.a,
        client_id=seed_client(people.a),
        worker_id=only_a_member,
        service_id=seed_service(people.a),
        starts_at=datetime.now(UTC) - timedelta(days=2),
        ends_at=datetime.now(UTC) - timedelta(days=2, minutes=-30),
        display_name="Old Name",
    )
    with tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO booking_events (tenant_id, booking_id, event, actor_user_id)
            VALUES (current_setting('app.tenant_id')::uuid, :b, 'completed', :u)
            """),
            {"b": booking_id, "u": people.only_a},
        )
    live = owner.get(f"/api/bookings/{booking_id}").json()
    assert live["history"][0]["actor_name"] == (display_name or "Test Person")

    assert delete(owner, only_a_member).status_code == 204

    row = removed_row(people.a, only_a_member)
    assert row.role == "removed" and row.removed_at is not None
    detail = owner.get(f"/api/bookings/{booking_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["worker_id"] == str(only_a_member)
    assert body["worker_display_name"] == "Old Name"
    assert body["history"][0]["actor_name"] == removed_actor_name


def account(tenant_id: uuid.UUID, user_id: uuid.UUID) -> tuple[Any, ...] | None:
    """The person's account row as the business reads it (the app role, row-level security on)."""
    with tenant_context(tenant_id) as session:
        row = session.execute(
            text("SELECT email, name, locale FROM users WHERE id = :u"), {"u": user_id}
        ).first()
    return None if row is None else tuple(row)


# ZB-32: a business reads a person's account only while they are its member. Removal hides it
# from that business alone, and a re-invite, which revives the same membership, shows it again.
# Kills: the users policy counting a removed membership.
def test_a_removed_member_s_account_is_hidden_until_they_are_invited_back(
    people: People, app: FastAPI
) -> None:
    add_membership(people.b, people.only_a)
    owner = signed_in(app, people.a, people.both)
    their_account = (email_of(people.only_a), "Test Person", None)
    assert account(people.a, people.only_a) == their_account

    assert delete(owner, member_id(people.a, people.only_a)).status_code == 204

    assert account(people.a, people.only_a) is None
    assert account(people.b, people.only_a) == their_account

    _, secret = minted(people.a, email_of(people.only_a))
    assert call(people.a, digest(secret), None).outcome == "accepted"

    assert account(people.a, people.only_a) == their_account


# 2: a live appointment blocks the removal; a lapsed hold, a past or a completed one does not.
LIVE = timedelta(hours=1)


@pytest.mark.parametrize(
    ("status", "starts", "ends", "expires", "blocked"),
    [
        ("confirmed", timedelta(days=1), timedelta(days=1, minutes=30), None, True),
        ("confirmed", timedelta(minutes=-10), timedelta(minutes=20), None, True),
        ("pending", timedelta(days=1), timedelta(days=1, minutes=30), LIVE, True),
        (
            "awaiting_payment",
            timedelta(days=1),
            timedelta(days=1, minutes=30),
            timedelta(minutes=10),
            True,
        ),
        (
            "pending",
            timedelta(days=1),
            timedelta(days=1, minutes=30),
            timedelta(minutes=-1),
            False,
        ),
        (
            "awaiting_payment",
            timedelta(days=1),
            timedelta(days=1, minutes=30),
            timedelta(minutes=-1),
            False,
        ),
        ("confirmed", timedelta(days=-2), timedelta(days=-2, minutes=30), None, False),
        ("completed", timedelta(days=1), timedelta(days=1, minutes=30), None, False),
    ],
)
def test_a_member_with_a_live_appointment_cannot_be_removed(
    people: People,
    app: FastAPI,
    status: str,
    starts: timedelta,
    ends: timedelta,
    expires: timedelta | None,
    blocked: bool,
) -> None:
    only_a_client = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    now = datetime.now(UTC)
    book(
        people.a,
        client_id=seed_client(people.a),
        worker_id=only_a_member,
        service_id=seed_service(people.a),
        starts_at=now + starts,
        ends_at=now + ends,
        status=status,
        expires_at=None if expires is None else now + expires,
    )

    response = delete(owner, only_a_member)

    if blocked:
        assert response.status_code == 409
        assert response.json() == {"code": "has_upcoming_bookings"}
        assert removed_row(people.a, only_a_member).role == "worker"
        assert only_a_client.get("/api/session").status_code == 200
    else:
        assert response.status_code == 204


def test_another_members_appointment_does_not_block_a_removal(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    now = datetime.now(UTC)
    book(
        people.a,
        client_id=seed_client(people.a),
        worker_id=member_id(people.a, people.both),
        service_id=seed_service(people.a),
        starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=1, minutes=30),
    )

    assert delete(owner, member_id(people.a, people.only_a)).status_code == 204


def test_the_accepted_ceiling_of_the_display_name_validator(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    name = "a" + "\u0301" * 40  # 40 combining acute accents after a letter

    response = owner.put(f"/api/members/{only_a_member}/display-name", json={"display_name": name})

    # Not a happy path: this pins the ceiling the `ponytail:` comment on DisplayNameText accepts.
    # Tightening the validator to reject this is a defensible change - delete this test then.
    assert response.status_code == 200


def test_an_emoji_only_display_name_is_refused(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(f"/api/members/{only_a_member}/display-name", json={"display_name": "☕"})

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
