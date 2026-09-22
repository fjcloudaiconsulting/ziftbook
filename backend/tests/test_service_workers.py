"""PUT /api/services/{id}/workers: which members perform each service (ZIF-45)."""

import threading
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege, UniqueViolation
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app import auth, services
from app.auth import SignedIn
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
from tests.test_services import DEFAULT, stored

CHANGED = "service_workers_changed"


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def worker(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.only_a)


def svc(client: TestClient) -> str:
    response = client.post("/api/services", json=DEFAULT)
    assert response.status_code == 201
    service_id: str = response.json()["id"]
    return service_id


def put(client: TestClient, service_id: object, body: Any) -> Response:
    return client.put(f"/api/services/{service_id}/workers", json=body)


def pairs(tenant_id: uuid.UUID) -> list[tuple[Any, ...]]:
    with tenant_context(tenant_id) as session:
        rows = session.execute(
            text("SELECT service_id, member_id FROM service_workers ORDER BY 1, 2")
        )
        return [tuple(row) for row in rows]


def ids(*members: uuid.UUID) -> list[str]:
    return sorted(str(m) for m in members)


# 1. fence: an owner assigns workers, themselves included; every member reads them.
def test_an_owner_assigns_workers_and_any_member_reads_them(
    people: People, owner: TestClient, worker: TestClient
) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    service_id = svc(owner)

    response = put(owner, service_id, [str(only_a), str(both)])

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["worker_ids"] == ids(only_a, both)
    assert worker.get(f"/api/services/{service_id}").json()["worker_ids"] == ids(only_a, both)
    assert [s["worker_ids"] for s in worker.get("/api/services").json()] == [ids(only_a, both)]
    assert pairs(people.a) == sorted(
        [(uuid.UUID(service_id), only_a), (uuid.UUID(service_id), both)]
    )


# 2. fence: members left out are unassigned; [] unassigns everyone.
def test_members_left_out_are_unassigned(people: People, owner: TestClient) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    service_id = svc(owner)
    put(owner, service_id, [str(only_a), str(both)])

    assert put(owner, service_id, [str(both)]).json()["worker_ids"] == ids(both)
    assert pairs(people.a) == [(uuid.UUID(service_id), both)]

    assert put(owner, service_id, []).json()["worker_ids"] == []
    assert pairs(people.a) == []


# 3. fence: the same set again, in any order or spelling, writes and records nothing.
def test_the_same_workers_again_change_nothing(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    service_id = svc(owner)
    first = put(owner, service_id, [str(only_a), str(both)])
    before = pairs(people.a)

    again = put(owner, service_id, [both.hex, str(only_a), str(both)])

    assert again.status_code == 200
    assert again.json() == first.json()
    assert pairs(people.a) == before
    assert len(events(migrate_engine, action=CHANGED, target=f"service:{service_id}")) == 1


# 4. fence: only owners assign, and a worker's malformed body is still a 403.
@pytest.mark.parametrize("body", ["valid", {}], ids=["valid", "malformed"])
def test_a_worker_may_not_assign(
    people: People, owner: TestClient, worker: TestClient, migrate_engine: Engine, body: Any
) -> None:
    service_id = svc(owner)
    if body == "valid":
        body = [str(member_id(people.a, people.only_a))]

    response = put(worker, service_id, body)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert pairs(people.a) == []
    assert events(migrate_engine, action=CHANGED, tenant_id=people.a) == []


def b_service(app: FastAPI, people: People) -> str:
    set_role(people.b, people.both, "owner")
    return svc(signed_in(app, people.b, people.both))


# 5a. fence: another business's service, or a made-up one, is not found.
@pytest.mark.parametrize("which", ["other_business", "random"])
def test_another_businesss_service_is_not_found(
    people: People, app: FastAPI, owner: TestClient, which: str
) -> None:
    service_id = b_service(app, people) if which == "other_business" else uuid.uuid7()

    response = put(owner, service_id, [str(member_id(people.a, people.only_a))])

    assert (response.status_code, response.json()) == (404, {"code": "not_found"})
    assert pairs(people.a) == []


# 5b. fence: a member id that isn't a member of this business is refused, and nothing is written.
@pytest.mark.parametrize("bogus", ["only_b", "both_in_b", "random"])
def test_another_businesss_member_is_refused(
    people: People, app: FastAPI, owner: TestClient, migrate_engine: Engine, bogus: str
) -> None:
    in_b = b_service(app, people)
    b_member = member_id(people.b, people.only_b)
    signed_in(app, people.b, people.both).put(f"/api/services/{in_b}/workers", json=[str(b_member)])
    service_id = svc(owner)
    before_a, before_b = pairs(people.a), pairs(people.b)
    other = {
        "only_b": b_member,
        "both_in_b": member_id(people.b, people.both),
        "random": uuid.uuid7(),
    }[bogus]

    # only_a sorts first, so checking only the first id lets the bogus one through.
    response = put(owner, service_id, [str(member_id(people.a, people.only_a)), str(other)])

    assert (response.status_code, response.json()) == (422, {"code": "unknown_member"})
    assert (pairs(people.a), pairs(people.b)) == (before_a, before_b)
    assert events(migrate_engine, action=CHANGED, tenant_id=people.a) == []


# 6. guard: authentication, content type and body shape.
@pytest.mark.parametrize(
    "body",
    [{}, "x", [1], [None], [str(uuid.uuid7()) for _ in range(201)]],
    ids=["object", "string", "number", "null", "201-ids"],
)
def test_a_malformed_body_is_refused(people: People, owner: TestClient, body: Any) -> None:
    service_id = svc(owner)

    response = put(owner, service_id, body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert pairs(people.a) == []


def test_auth_content_type_and_path_guards(people: People, app: FastAPI, owner: TestClient) -> None:
    service_id = svc(owner)
    only_a = str(member_id(people.a, people.only_a))

    assert put(new_client(app), service_id, [only_a]).status_code == 401
    plain = owner.put(
        f"/api/services/{service_id}/workers",
        content=f'["{only_a}"]',
        headers={"content-type": "text/plain"},
    )
    assert plain.status_code == 415
    bad_path = put(owner, "not-a-uuid", [only_a])
    assert (bad_path.status_code, bad_path.json()) == (422, {"code": "invalid_request"})
    assert pairs(people.a) == []


# 7. fence: removing a member removes their assignments, and only theirs.
def test_removing_a_member_removes_their_assignments(people: People, owner: TestClient) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    first, second = svc(owner), svc(owner)
    put(owner, first, [str(only_a), str(both)])
    put(owner, second, [str(only_a)])

    response = owner.request("DELETE", f"/api/members/{only_a}", json={})

    assert response.status_code == 204
    assert pairs(people.a) == [(uuid.UUID(first), both)]
    assert owner.get(f"/api/services/{first}").json()["worker_ids"] == ids(both)
    assert owner.get(f"/api/services/{second}").json()["worker_ids"] == []


# 8. guard: a role change or archiving leaves assignments alone; archived services are assignable.
def test_role_and_archive_leave_assignments_alone(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    service_id = svc(owner)
    put(owner, service_id, [str(both)])
    set_role(people.a, people.only_a, "owner")  # keep_an_owner: another owner first
    new_owner = signed_in(app, people.a, people.only_a)
    demoted = new_owner.patch(f"/api/members/{both}", json={"role": "worker"})
    assert demoted.status_code == 200
    path = f"/api/services/{service_id}"
    assert new_owner.get(path).json()["worker_ids"] == ids(both)

    assert new_owner.patch(path, json={"archived": True}).status_code == 200
    assert new_owner.get(path).json()["worker_ids"] == ids(both)
    assigned = put(new_owner, service_id, [str(both), str(only_a)])
    assert assigned.status_code == 200
    assert assigned.json()["archived"] is True
    assert new_owner.patch(path, json={"archived": False}).status_code == 200
    assert new_owner.get(path).json()["worker_ids"] == ids(both, only_a)


# 9. fence: one event naming users, never member ids or emails.
def test_a_change_is_recorded_with_user_ids(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    service_id = svc(owner)
    put(owner, service_id, [str(only_a)])
    before = events(migrate_engine, action=CHANGED, tenant_id=people.a)

    assert put(owner, service_id, [str(both)]).status_code == 200

    recorded = events(migrate_engine, action=CHANGED, tenant_id=people.a)
    assert len(recorded) == len(before) + 1
    event = recorded[-1]
    assert event["tenant_id"] == people.a
    assert event["actor_user_id"] == people.both
    assert event["target"] == f"service:{service_id}"
    assert event["details"] == {"added": [str(people.both)], "removed": [str(people.only_a)]}
    assert email_of(people.only_a) not in str(recorded)
    assert email_of(people.both) not in str(recorded)


# 10. fence: a failed recording leaves the assignments unchanged.
def test_a_failed_recording_leaves_assignments_unchanged(
    people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_id = svc(owner)
    put(owner, service_id, [str(member_id(people.a, people.only_a))])
    before = pairs(people.a)
    failing(monkeypatch, auth, "record")

    response = put(owner, service_id, [str(member_id(people.a, people.both))])

    assert (response.status_code, response.json()) == (500, {"code": "internal"})
    assert pairs(people.a) == before


# 11. fence: a removal in flight makes the assignment a 422, never a foreign key 500.
def test_an_assignment_waits_on_a_removal_in_flight(
    people: People, owner: TestClient, app_engine: Engine
) -> None:
    only_a = member_id(people.a, people.only_a)
    service_id = svc(owner)
    results: list[Response] = []

    def request() -> None:
        results.append(put(owner, service_id, [str(only_a)]))

    with app_engine.connect() as holder:
        with holder.begin():
            holder.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)}
            )
            holder.execute(text("DELETE FROM memberships WHERE id = :m"), {"m": only_a})
            thread = threading.Thread(target=request)
            thread.start()
            wait_until_blocked(app_engine, 1)
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert len(results) == 1
    assert (results[0].status_code, results[0].json()) == (422, {"code": "unknown_member"})
    assert pairs(people.a) == []


# 12. fence: a removal after the member lock waits, then cascades the fresh assignment.
def test_a_removal_waits_on_an_assignment_in_flight(
    people: People, owner: TestClient, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    only_a = member_id(people.a, people.only_a)
    service_id = svc(owner)
    locked = threading.Event()
    release = threading.Event()
    real_lock_members = services.lock_members

    def wrapper(current: SignedIn, member_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        result = real_lock_members(current, member_ids)
        locked.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(services, "lock_members", wrapper)
    results: dict[str, Response] = {}

    def do_put() -> None:
        results["put"] = put(owner, service_id, [str(only_a)])

    def do_remove() -> None:
        results["remove"] = owner.request("DELETE", f"/api/members/{only_a}", json={})

    put_thread = threading.Thread(target=do_put)
    remove_thread = threading.Thread(target=do_remove)
    put_thread.start()
    try:
        assert locked.wait(10)
        remove_thread.start()
        wait_until_blocked(app_engine, 1)
    finally:
        release.set()
        put_thread.join(timeout=10)
        if remove_thread.ident:
            remove_thread.join(timeout=10)

    assert not put_thread.is_alive()
    assert not remove_thread.is_alive()
    assert results["put"].status_code == 200
    assert results["remove"].status_code == 204
    assert pairs(people.a) == []


# 13. guard: each service lists its own workers, in id order; a new one has none.
def test_each_service_lists_its_own_workers(people: People, owner: TestClient) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)
    first, second = svc(owner), svc(owner)
    put(owner, first, [str(both), str(only_a)])
    put(owner, second, [str(both)])

    listed = owner.get("/api/services").json()

    assert [(s["id"], s["worker_ids"]) for s in listed] == [
        (first, ids(only_a, both)),
        (second, ids(both)),
    ]
    assert owner.post("/api/services", json=DEFAULT).json()["worker_ids"] == []


# 14. fence: the database's own checks, as the app role.
INSERT = "INSERT INTO service_workers VALUES (:t, :s, :m)"


def test_the_database_refuses_crossed_or_duplicate_rows(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    service_a = svc(owner)
    service_b = b_service(app, people)
    member_a = member_id(people.a, people.only_a)
    member_b = member_id(people.b, people.only_b)

    for service_id, member in ((service_b, member_a), (service_a, member_b)):
        with pytest.raises(IntegrityError) as crossed, tenant_context(people.a) as session:
            session.execute(text(INSERT), {"t": people.a, "s": service_id, "m": member})
        assert isinstance(crossed.value.orig, ForeignKeyViolation)

    with pytest.raises(ProgrammingError) as other, tenant_context(people.a) as session:
        session.execute(text(INSERT), {"t": people.b, "s": service_a, "m": member_a})
    assert isinstance(other.value.orig, InsufficientPrivilege)

    with pytest.raises(IntegrityError) as twice, tenant_context(people.a) as session:
        session.execute(text(INSERT), {"t": people.a, "s": service_a, "m": member_a})
        session.execute(text(INSERT), {"t": people.a, "s": service_a, "m": member_a})
    assert isinstance(twice.value.orig, UniqueViolation)
    assert twice.value.orig.diag.constraint_name == "pk_service_workers"

    with tenant_context(people.a) as session:
        session.execute(text(INSERT), {"t": people.a, "s": service_a, "m": member_a})
        deleted = session.scalars(text("DELETE FROM service_workers RETURNING member_id")).all()
    assert deleted == [member_a]


# 15. guard: the index, no (tenant_id, id) target, and the service cascade.
def test_schema_index_and_service_cascade(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    put(owner, svc(owner), [str(member_id(people.a, people.only_a))])
    with migrate_engine.begin() as conn:
        index = conn.scalar(
            text("""
            SELECT indexdef FROM pg_indexes
            WHERE indexname = 'ix_service_workers_tenant_id_member_id'
            """)
        )
        unique = conn.scalar(
            text("SELECT count(*) FROM pg_constraint WHERE conname = :name"),
            {"name": "uq_service_workers_tenant_id_id"},
        )
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("DELETE FROM services"))
        left = conn.scalar(text("SELECT count(*) FROM service_workers"))
    assert index is not None and "(tenant_id, member_id)" in index
    assert unique == 0
    assert left == 0


# ZIF-104: POST /api/services takes worker_ids and assigns them in the same transaction.
def create(client: TestClient, worker_ids: Any) -> Response:
    return client.post("/api/services", json={**DEFAULT, "worker_ids": worker_ids})


# 16. fence: creating with workers assigns them and records what the PUT would.
def test_creating_a_service_assigns_its_workers(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    only_a, both = member_id(people.a, people.only_a), member_id(people.a, people.both)

    response = create(owner, [str(both), str(only_a)])

    assert response.status_code == 201
    service_id = response.json()["id"]
    assert response.json()["worker_ids"] == ids(only_a, both)
    assert pairs(people.a) == sorted((uuid.UUID(service_id), m) for m in (only_a, both))
    recorded = events(migrate_engine, tenant_id=people.a)
    assert [e["action"] for e in recorded] == ["service_created", CHANGED]
    assert recorded[-1]["target"] == f"service:{service_id}"
    assert recorded[-1]["actor_user_id"] == people.both
    assert recorded[-1]["details"] == {
        "added": sorted(str(u) for u in (people.only_a, people.both)),
        "removed": [],
    }


# 17. fence: a rejected worker id leaves no service, no assignment and no audit row behind.
@pytest.mark.parametrize("bogus", ["only_b", "both_in_b", "random"])
def test_a_rejected_worker_leaves_no_service_behind(
    people: People, app: FastAPI, owner: TestClient, migrate_engine: Engine, bogus: str
) -> None:
    in_b = b_service(app, people)
    b_member = member_id(people.b, people.only_b)
    signed_in(app, people.b, people.both).put(f"/api/services/{in_b}/workers", json=[str(b_member)])
    before_b = (stored(people.b), pairs(people.b))
    other = {
        "only_b": b_member,
        "both_in_b": member_id(people.b, people.both),
        "random": uuid.uuid7(),
    }[bogus]

    # only_a sorts first, so checking only the first id lets the bogus one through.
    response = create(owner, [str(member_id(people.a, people.only_a)), str(other)])

    assert (response.status_code, response.json()) == (422, {"code": "unknown_member"})
    assert stored(people.a) == []
    assert pairs(people.a) == []
    assert events(migrate_engine, tenant_id=people.a) == []
    assert (stored(people.b), pairs(people.b)) == before_b


# 18. guard: no list, or an empty one, is today's create: nobody assigned, one event.
@pytest.mark.parametrize("body", [DEFAULT, {**DEFAULT, "worker_ids": []}], ids=["omitted", "[]"])
def test_creating_without_workers_is_unchanged(
    people: People, owner: TestClient, migrate_engine: Engine, body: dict[str, Any]
) -> None:
    response = owner.post("/api/services", json=body)

    assert response.status_code == 201
    assert response.json()["worker_ids"] == []
    assert pairs(people.a) == []
    assert [e["action"] for e in events(migrate_engine, tenant_id=people.a)] == ["service_created"]


# 19. guard: a malformed list is refused before anything is written.
@pytest.mark.parametrize(
    "worker_ids",
    [None, "x", [1], [None], [str(uuid.uuid7()) for _ in range(201)]],
    ids=["null", "string", "number", "null-item", "201-ids"],
)
def test_a_malformed_worker_list_is_refused_on_create(
    people: People, owner: TestClient, worker_ids: Any
) -> None:
    response = create(owner, worker_ids)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a) == []
