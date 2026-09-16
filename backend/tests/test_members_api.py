"""GET/PATCH/DELETE /api/members: owners list, promote, demote and remove members."""

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
    email_of,
    events,
    failing,
    member_id,
    new_client,
    set_role,
    signed_in,
    wait_until_blocked,
)


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def unauthenticated(response: Response) -> bool:
    return response.status_code == 401 and response.json() == {"code": "unauthenticated"}


# 1. fence: the listing, ordered, scoped to the business, with the joined email.
def test_the_owner_lists_members_of_their_business(people: People, app: FastAPI) -> None:
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
            },
            {
                "member_id": str(member_id(people.a, people.both)),
                "user_id": str(people.both),
                "email": email_of(people.both),
                "role": "owner",
            },
        ],
        key=lambda m: str(m["member_id"]),
    )
    assert response.json() == expected


# 2. fence: a worker is refused every endpoint, whatever it sends.
@pytest.mark.parametrize(
    ("method", "target_both", "body"),
    [
        ("GET", False, None),
        ("PATCH", True, {"role": "worker"}),
        ("PATCH", True, {"role": "nope"}),
        ("PATCH", True, []),
        ("PATCH", False, {"role": "worker"}),  # path "not-a-uuid"
        ("DELETE", True, {}),
        ("DELETE", True, []),
    ],
)
def test_a_worker_is_refused_every_members_endpoint(
    people: People, app: FastAPI, method: str, target_both: bool, body: Any
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    owner_session = signed_in(app, people.a, people.both)
    assert owner_session.get("/api/session").status_code == 200
    if method == "GET":
        path = "/api/members"
    elif target_both:
        path = f"/api/members/{member_id(people.a, people.both)}"
    else:
        path = "/api/members/not-a-uuid"

    response = worker.request(method, path, json=body)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    with tenant_context(people.a) as session:
        roles: dict[uuid.UUID, str] = dict(
            session.execute(text("SELECT user_id, role FROM memberships")).tuples().all()
        )
    assert roles == {people.only_a: "worker", people.both: "owner"}
    assert owner_session.get("/api/session").status_code == 200


# 3. guard: no cookie is 401 on every endpoint; a non-JSON write is 415 before auth.
def test_without_a_cookie_every_endpoint_is_unauthenticated(people: People, app: FastAPI) -> None:
    client = new_client(app)
    both_member = member_id(people.a, people.both)

    for method, path, body in [
        ("GET", "/api/members", None),
        ("PATCH", f"/api/members/{both_member}", {"role": "worker"}),
        ("DELETE", f"/api/members/{both_member}", {}),
    ]:
        response = client.request(method, path, json=body)
        assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_a_non_json_write_is_refused_before_auth(people: People, app: FastAPI, method: str) -> None:
    owner = signed_in(app, people.a, people.both)
    only_a_member = member_id(people.a, people.only_a)

    response = owner.request(
        method,
        f"/api/members/{only_a_member}",
        content="x",
        headers={"content-type": "text/plain"},
    )

    assert (response.status_code, response.json()) == (415, {"code": "unsupported_media_type"})
    with tenant_context(people.a) as session:
        roles: dict[uuid.UUID, str] = dict(
            session.execute(text("SELECT user_id, role FROM memberships")).tuples().all()
        )
    assert roles == {people.only_a: "worker", people.both: "owner"}


# 4. fence: demoting ends the member's sessions in this business only.
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
    assert response.json()["role"] == "worker"
    assert unauthenticated(both_in_a.get("/api/session"))
    assert both_in_b.get("/api/session").status_code == 200
    new_session = signed_in(app, people.a, people.both)
    assert new_session.get("/api/session").json()["role"] == "worker"


# 5. fence (deterministic race): two owners acting on each other serialize through target()'s
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


# 5b. fence (deterministic race): a stale owner is rechecked after the lock, not before.
@pytest.mark.parametrize("holder_action", ["demoted", "removed"])
def test_a_stale_owner_is_rechecked_after_the_lock(
    people: People, app: FastAPI, app_engine: Engine, migrate_engine: Engine, holder_action: str
) -> None:
    # uuidv7 ids follow fixture insertion order: only_a's membership (inserted first, as a worker)
    # sorts before both's (inserted second, as owner). target()'s "ORDER BY m.id FOR UPDATE"
    # therefore locks only_a's row first (uncontended) and then blocks on both's row below, which
    # the holder locks alone -- never the whole table, or this test's wait would be undetectable.
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


# 6. fence: promoting a member rotates their session and grants owner access immediately.
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


# 7. fence: the unchanged-role no-op doesn't rotate sessions or record anything.
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
    }
    assert worker_client.get("/api/session").status_code == 200
    assert events(migrate_engine, tenant_id=people.a, action="member_role_changed") == []


# 8. fence: removal is a hard delete of the membership only, and it ends the member's sessions.
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


# 9. fence: a member id outside the business, or a random one, is 404, never a leak.
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


# 10. fence: an owner can't change or remove their own membership through these endpoints.
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


# 11. fence: a malformed role, extra keys, or a non-UUID path are all 422, nothing changed.
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


# 12. fence: a role change is recorded with the user's id, and the old and new role.
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


# 12b. fence: a removal is recorded with the user's id and no details.
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


# 13. fence: a change whose event fails to record leaves nothing changed.
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


# 14. fence: the 409 mapping matches only the trigger's own last_owner constraint.
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
