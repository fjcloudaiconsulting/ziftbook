"""/api/members: owners list, promote, demote and remove members; anyone sets their own
display name."""

import json
import threading
import uuid
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
    People,
    add_membership,
    email_of,
    events,
    failing,
    member_id,
    new_client,
    set_role,
    signed_in,
    wait_until_blocked,
)
from tests.test_services import service as create_service


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
            session.execute(text("SELECT user_id, role FROM memberships")).tuples().all()
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
            session.execute(text("SELECT user_id, role FROM memberships")).tuples().all()
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
        remaining = session.execute(text("SELECT user_id, role FROM memberships")).all()

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
@pytest.mark.parametrize("holder_action", ["demoted", "removed"])
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
            holder.execute(text("DELETE FROM memberships WHERE id = :id"), {"id": both_member})
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


# Removal is a hard delete of the membership only, and it ends the member's sessions.
def test_removing_a_member_deletes_the_membership_not_the_account(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    only_a_client = signed_in(app, people.a, people.only_a)
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

    assert response.status_code == 204
    assert response.content == b""
    assert unauthenticated(only_a_client.get("/api/session"))
    listed = {m["user_id"] for m in owner.get("/api/members").json()}
    assert str(people.only_a) not in listed
    with migrate_engine.connect() as conn:
        alive = conn.scalar(text("SELECT count(*) FROM users WHERE id = :u"), {"u": people.only_a})
    assert alive == 1


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
            CREATE TRIGGER zz_refuse BEFORE DELETE ON memberships
            FOR EACH ROW EXECUTE FUNCTION zz_refuse()
            """)
        )
    try:
        response = owner.request("DELETE", f"/api/members/{only_a_member}", json={})

        expected_body = {"code": "last_owner" if constraint == "last_owner" else "internal"}
        assert (response.status_code, response.json()) == (expected, expected_body)
        with tenant_context(people.a) as session:
            still_there = session.scalar(
                text("SELECT count(*) FROM memberships WHERE user_id = :u"), {"u": people.only_a}
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


# ZIF-51: bookings reference memberships with no ON DELETE, so removing a member who holds one is
# a 409, not the 500 a bare ForeignKeyViolation re-raise would give (F2).


def seed_booking_for(tenant_id: uuid.UUID, worker_id: uuid.UUID, service_id: str) -> None:
    """A real booking row, inserted directly as the console path would (source='merchant')."""
    with tenant_context(tenant_id) as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(51, hashtext(current_setting('app.tenant_id')))")
        )
        client_id = session.scalar(
            text(
                "INSERT INTO clients (tenant_id, name) "
                "VALUES (current_setting('app.tenant_id')::uuid, 'Client') RETURNING id"
            )
        )
        session.execute(
            text("""
            INSERT INTO bookings (tenant_id, client_id, worker_id, service_id, starts_at, ends_at,
                status, source, service_name, price_amount_minor, price_currency,
                duration_minutes, auto_confirm_at_booking)
            SELECT current_setting('app.tenant_id')::uuid, :client_id, :worker_id, :service_id,
                   now() + interval '1 day', now() + interval '1 day 30 minutes', 'confirmed',
                   'merchant', name, price_amount_minor, price_currency, duration_minutes, true
            FROM services WHERE id = :service_id
            """),
            {"client_id": client_id, "worker_id": worker_id, "service_id": service_id},
        )


def test_removing_a_member_who_holds_a_booking_is_a_409(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)
    created = create_service(owner)
    assert created.status_code == 201
    service_id = created.json()["id"]
    seed_booking_for(people.a, only_a_member, service_id)

    # F-h: json_only refuses a bare `.request("DELETE", ...)` with no body before routing unless
    # the content-type is application/json - this is the repo's first DELETE test.
    response = owner.request(
        "DELETE",
        f"/api/members/{only_a_member}",
        headers={"content-type": "application/json"},
        json={},
    )

    assert (response.status_code, response.json()) == (409, {"code": "has_bookings"})
    with tenant_context(people.a) as session:
        still_there = session.scalar(
            text("SELECT count(*) FROM memberships WHERE id = :id"), {"id": only_a_member}
        )
    assert still_there == 1


@pytest.mark.parametrize(
    "name",
    ["⠀", "a" + "́" * 40],  # Braille blank; 40 combining acute accents
)
def test_the_accepted_ceiling_of_the_display_name_validator(
    people: People, app: FastAPI, name: str
) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.put(f"/api/members/{only_a_member}/display-name", json={"display_name": name})

    # Not a happy path: this pins the ceiling the `ponytail:` comment on DisplayNameText accepts.
    # Tightening the validator to reject these is a defensible change — delete this test then.
    assert response.status_code == 200
