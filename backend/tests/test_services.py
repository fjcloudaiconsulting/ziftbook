"""Services: what a business sells. Owners add and change them; every member reads them,
archived ones included."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.errors import CheckViolation, ForeignKeyViolation, InsufficientPrivilege
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app import auth
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, events, failing, new_client, signed_in

DEFAULT = {"name": {"en": "Cut"}, "price": {"amount_minor": 2500}, "duration_minutes": 30}


def service(client: TestClient, **overrides: Any) -> Response:
    return client.post("/api/services", json={**DEFAULT, **overrides})


def stored(tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    with tenant_context(tenant_id) as session:
        rows = session.execute(text("SELECT * FROM services ORDER BY id")).mappings()
        return [dict(row) for row in rows]


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def worker(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.only_a)


# an owner creates a service; any member reads it


def test_an_owner_creates_a_service_and_any_member_reads_it(
    people: People, owner: TestClient, worker: TestClient
) -> None:
    response = service(owner)

    assert response.status_code == 201
    body = response.json()
    service_id = body.pop("id")
    assert body == {
        "name": {"en": "Cut"},
        "description": {},
        "price": {"amount_minor": 2500, "currency": "EUR"},
        "duration_minutes": 30,
        "buffer_minutes": None,
        "archived": False,
        "worker_ids": [],
    }
    assert response.headers["cache-control"] == "no-store"

    listed = worker.get("/api/services")
    assert (listed.status_code, listed.json()) == (200, [{"id": service_id, **body}])
    assert listed.headers["cache-control"] == "no-store"

    read = worker.get(f"/api/services/{service_id}")
    assert (read.status_code, read.json()) == (200, {"id": service_id, **body})
    assert read.headers["cache-control"] == "no-store"


def test_an_explicit_null_buffer_is_the_same_as_leaving_it_out(
    people: People, owner: TestClient
) -> None:
    response = service(owner, buffer_minutes=None)

    assert (response.status_code, response.json()["buffer_minutes"]) == (201, None)


# a worker never writes a service


@pytest.mark.parametrize(
    "request_",
    [
        lambda client, sid: client.post("/api/services", json=DEFAULT),
        lambda client, sid: client.post("/api/services", json={"name": 5}),
        lambda client, sid: client.post("/api/services", json=[]),
        lambda client, sid: client.patch(f"/api/services/{sid}", json={"duration_minutes": 45}),
        lambda client, sid: client.patch(f"/api/services/{sid}", json={"duration_minutes": "x"}),
        lambda client, sid: client.patch(f"/api/services/{sid}", json=[]),
        lambda client, sid: client.patch("/api/services/not-a-uuid", json={"duration_minutes": 45}),
    ],
    ids=["valid", "bad-name", "list", "valid-patch", "bad-patch", "list-patch", "bad-path"],
)
def test_a_worker_never_writes_a_service(
    people: People,
    owner: TestClient,
    worker: TestClient,
    migrate_engine: Engine,
    request_: Any,
) -> None:
    created = service(owner)
    service_id = created.json()["id"]

    response = request_(worker, service_id)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert len(stored(people.a)) == 1
    assert [e["action"] for e in events(migrate_engine, tenant_id=people.a)] == ["service_created"]


# every route needs a session and JSON; there is no DELETE


def test_the_routes_require_a_session_and_json(
    people: People, app: FastAPI, owner: TestClient
) -> None:
    created = service(owner)
    service_id = created.json()["id"]
    before = stored(people.a)
    anon = new_client(app)

    assert anon.get("/api/services").status_code == 401
    assert anon.get(f"/api/services/{service_id}").status_code == 401
    assert anon.post("/api/services", json=DEFAULT).status_code == 401
    assert (
        anon.patch(f"/api/services/{service_id}", json={"duration_minutes": 45}).status_code == 401
    )

    text_type = {"content-type": "text/plain"}
    assert owner.post("/api/services", content="not json", headers=text_type).status_code == 415
    assert (
        owner.patch(
            f"/api/services/{service_id}", content="not json", headers=text_type
        ).status_code
        == 415
    )
    assert (
        owner.request(
            "DELETE", f"/api/services/{service_id}", headers={"content-type": "application/json"}
        ).status_code
        == 405
    )

    assert stored(people.a) == before


# tenant isolation


@pytest.mark.parametrize("target", ["b_service", "random"])
def test_a_business_cannot_reach_another_businesss_service(
    people: People, owner: TestClient, target: str
) -> None:
    with tenant_context(people.b) as session:
        b_service_id = session.execute(
            text("""
            INSERT INTO services (tenant_id, name, price_amount_minor, price_currency,
                                  duration_minutes)
            SELECT id, '{"en": "B cut"}'::jsonb, 1000, currency, 30 FROM tenants WHERE id = :b
            RETURNING id
            """),
            {"b": people.b},
        ).scalar()

    target_id = b_service_id if target == "b_service" else uuid.uuid7()

    read = owner.get(f"/api/services/{target_id}")
    patched = owner.patch(f"/api/services/{target_id}", json={"duration_minutes": 45})

    assert (read.status_code, read.json()) == (404, {"code": "not_found"})
    assert (patched.status_code, patched.json()) == (404, {"code": "not_found"})
    assert [row["duration_minutes"] for row in stored(people.b)] == [30]
    assert owner.get("/api/services").json() == []


# the price's currency comes from the business, not the client


def test_the_price_currency_comes_from_the_business(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    # people is function-scoped, so changing b's currency here as the migrate role never leaks
    # into another test.
    with migrate_engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET country = 'BR', currency = 'BRL' WHERE id = :b"),
            {"b": people.b},
        )
    with tenant_context(people.b) as session:
        session.execute(
            text("UPDATE memberships SET role = 'owner' WHERE user_id = :u"), {"u": people.only_b}
        )
    owner_b = signed_in(app, people.b, people.only_b)
    owner_a = signed_in(app, people.a, people.both)

    created_b = service(owner_b)
    created_a = service(owner_a)

    assert created_b.json()["price"]["currency"] == "BRL"
    assert stored(people.b)[0]["price_currency"] == "BRL"
    assert created_a.json()["price"]["currency"] == "EUR"


# a client cannot set currency, tenant_id or id


def extra_field(people: People, key: str) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {
        "currency_in_price": {"price": {"amount_minor": 100, "currency": "USD"}},
        "price_currency": {"price_currency": "USD"},
        "tenant_id": {"tenant_id": str(people.b)},
        "id": {"id": str(uuid.uuid7())},
    }
    return fields[key]


@pytest.mark.parametrize("key", ["currency_in_price", "price_currency", "tenant_id", "id"])
def test_a_client_cannot_set_currency_tenant_or_id_on_create(
    people: People, owner: TestClient, key: str
) -> None:
    response = owner.post("/api/services", json={**DEFAULT, **extra_field(people, key)})

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a) == []


@pytest.mark.parametrize("key", ["currency_in_price", "price_currency", "tenant_id", "id"])
def test_a_client_cannot_set_currency_tenant_or_id_on_update(
    people: People, owner: TestClient, key: str
) -> None:
    created = service(owner).json()

    response = owner.patch(f"/api/services/{created['id']}", json=extra_field(people, key))

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    rows = stored(people.a)
    assert (len(rows), rows[0]["duration_minutes"]) == (1, 30)


# a business's currency is fixed once it has services


def test_a_businesss_currency_cannot_change_while_it_has_services(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    service(owner)

    with pytest.raises(IntegrityError) as error, migrate_engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET currency = 'USD' WHERE id = :a"), {"a": people.a})
    assert isinstance(error.value.orig, ForeignKeyViolation)

    with migrate_engine.connect() as conn:
        conn.execute(text("UPDATE tenants SET currency = 'USD' WHERE id = :b"), {"b": people.b})
        conn.rollback()


# archiving


def test_archiving_keeps_a_service_listed_and_remembers_when(
    people: People, owner: TestClient
) -> None:
    # "Wash" created first, "Cut" second: id order and name order disagree, so a list sorted by
    # name instead of id would still pass a check that only compares sets.
    first = service(owner, name={"en": "Wash"}).json()
    second = service(owner).json()

    archived = owner.patch(f"/api/services/{first['id']}", json={"archived": True})
    assert (archived.status_code, archived.json()["archived"]) == (200, True)
    rows_by_id = {str(row["id"]): row for row in stored(people.a)}
    first_archived_at = rows_by_id[first["id"]]["archived_at"]
    assert first_archived_at is not None

    listed = owner.get("/api/services").json()
    assert [s["id"] for s in listed] == [first["id"], second["id"]]
    assert [s["archived"] for s in listed] == [True, False]

    reput = owner.patch(
        f"/api/services/{first['id']}", json={"archived": True, "duration_minutes": 45}
    )
    assert (reput.status_code, reput.json()["duration_minutes"]) == (200, 45)
    rows_by_id = {str(row["id"]): row for row in stored(people.a)}
    assert rows_by_id[first["id"]]["archived_at"] == first_archived_at

    # An unrelated field alone, with no "archived" key at all, must not unarchive the service.
    unrelated = owner.patch(f"/api/services/{first['id']}", json={"duration_minutes": 50})
    assert (unrelated.status_code, unrelated.json()["duration_minutes"]) == (200, 50)
    assert unrelated.json()["archived"] is True
    rows_by_id = {str(row["id"]): row for row in stored(people.a)}
    assert rows_by_id[first["id"]]["archived_at"] == first_archived_at

    unarchived = owner.patch(f"/api/services/{first['id']}", json={"archived": False})
    assert (unarchived.status_code, unarchived.json()["archived"]) == (200, False)
    rows_by_id = {str(row["id"]): row for row in stored(people.a)}
    assert rows_by_id[first["id"]]["archived_at"] is None


# omitted vs null


def test_an_omitted_field_keeps_its_value_and_null_is_refused_except_for_buffer(
    people: People, owner: TestClient
) -> None:
    created = service(owner, buffer_minutes=10, description={"en": "Wash\nand cut"}).json()
    sid = created["id"]

    kept = owner.patch(f"/api/services/{sid}", json={"duration_minutes": 45})
    assert (
        kept.json()["duration_minutes"],
        kept.json()["buffer_minutes"],
        kept.json()["description"],
    ) == (
        45,
        10,
        {"en": "Wash\nand cut"},
    )
    assert stored(people.a)[0]["duration_minutes"] == 45
    assert kept.headers["cache-control"] == "no-store"

    repriced = owner.patch(f"/api/services/{sid}", json={"price": {"amount_minor": 3000}})
    assert repriced.json()["price"]["amount_minor"] == 3000
    assert stored(people.a)[0]["price_amount_minor"] == 3000

    nulled = owner.patch(f"/api/services/{sid}", json={"buffer_minutes": None})
    assert nulled.json()["buffer_minutes"] is None

    cleared = owner.patch(f"/api/services/{sid}", json={"description": {}})
    assert cleared.json()["description"] == {}

    for field in ("name", "description", "price", "duration_minutes", "archived"):
        response = owner.patch(f"/api/services/{sid}", json={field: None})
        assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})

    unchanged = owner.get(f"/api/services/{sid}").json()
    assert unchanged["description"] == {}


# every field's validation


@pytest.mark.parametrize(
    "extra",
    [
        {"name": {"de": "Schnitt"}},
        {"name": {"EN": "Cut"}},
        {"name": {}},
        {"name": "Cut"},
        {"name": {"en": 5}},
        {"name": {"en": "   "}},
        {"name": {"en": "x" * 101}},
        {"name": {"en": "Cut\nhair"}},
        {"name": {"en": "Cut\u200b"}},
        {"description": {"fr": "x"}},
        {"description": {"en": "x" * 1001}},
        {"description": {"en": "a\r\nb"}},
        {"description": {"en": "a\u0000b"}},
        {"description": {"en": "a\u2028b"}},
        {"description": {"en": "a\u2029b"}},
        {"description": {"en": ""}},
        {"price": {"amount_minor": 10.0}},
        {"price": {"amount_minor": 10.5}},
        {"price": {"amount_minor": True}},
        {"price": {"amount_minor": "1000"}},
        {"price": {"amount_minor": -1}},
        {"price": {"amount_minor": 1000001}},
        {"price": {"amount_minor": None}},
        {"duration_minutes": 4},
        {"duration_minutes": 721},
        {"duration_minutes": 30.0},
        {"duration_minutes": True},
        {"buffer_minutes": -1},
        {"buffer_minutes": 241},
        {"buffer_minutes": True},
    ],
)
def test_a_service_body_is_refused_for_the_wrong_kind_of_value(
    people: People, owner: TestClient, extra: dict[str, Any]
) -> None:
    response = owner.post("/api/services", json={**DEFAULT, **extra})

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a) == []


@pytest.mark.parametrize(
    "body",
    [
        {"description": {}, "price": DEFAULT["price"], "duration_minutes": 30},
        {"name": DEFAULT["name"], "duration_minutes": 30},
        {"name": DEFAULT["name"], "price": DEFAULT["price"]},
        {**DEFAULT, "archived": True},
        [],
    ],
    ids=["no-name", "no-price", "no-duration", "archived-on-create", "list"],
)
def test_a_service_body_missing_a_field_is_refused(
    people: People, owner: TestClient, body: Any
) -> None:
    response = owner.post("/api/services", json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a) == []


# The Annotated types are shared between ServiceIn and ServiceChange: a representative sample
# proves PATCH reuses the same validation, not the full matrix above again.
@pytest.mark.parametrize(
    "extra",
    [
        {"name": {"de": "Schnitt"}},
        {"description": {"en": "x" * 1001}},
        {"price": {"amount_minor": -1}},
        {"duration_minutes": 4},
        {"buffer_minutes": 241},
    ],
)
def test_an_update_is_also_refused_for_the_wrong_kind_of_value(
    people: People, owner: TestClient, extra: dict[str, Any]
) -> None:
    created = service(owner).json()

    response = owner.patch(f"/api/services/{created['id']}", json=extra)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert stored(people.a)[0]["duration_minutes"] == 30


# boundaries


def test_boundary_values_are_accepted_and_text_is_stripped(
    people: People, owner: TestClient
) -> None:
    full_name = {"en": "x" * 100, "nl": "y" * 100, "pt": "z" * 100}
    stripped = service(owner, name={"en": " Cut "}).json()
    assert stripped["name"] == {"en": "Cut"}

    every_language = service(owner, name=full_name).json()
    assert every_language["name"] == full_name

    description = "x" * 499 + "\n" + "x" * 500  # 1000 chars, \n in the middle so it isn't stripped
    long_description = service(owner, description={"en": description}).json()
    assert long_description["description"]["en"] == description

    full_description = {"en": "Wash", "nl": "Wassen", "pt": "Lavar"}
    every_description = service(owner, description=full_description).json()
    assert every_description["description"] == full_description

    limits = service(
        owner, price={"amount_minor": 1000000}, duration_minutes=720, buffer_minutes=240
    ).json()
    assert (
        limits["price"]["amount_minor"],
        limits["duration_minutes"],
        limits["buffer_minutes"],
    ) == (
        1000000,
        720,
        240,
    )
    zero = service(owner, price={"amount_minor": 0}, duration_minutes=5, buffer_minutes=0).json()
    assert (zero["price"]["amount_minor"], zero["duration_minutes"], zero["buffer_minutes"]) == (
        0,
        5,
        0,
    )


# audit: changed fields only, never on a no-op


def test_service_changes_are_audited_only_when_something_changed(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    created = service(owner)
    sid = created.json()["id"]

    changed = owner.patch(
        f"/api/services/{sid}", json={"price": {"amount_minor": 3000}, "duration_minutes": 30}
    )
    resent = owner.patch(
        f"/api/services/{sid}", json={"price": {"amount_minor": 3000}, "duration_minutes": 30}
    )
    empty = owner.patch(f"/api/services/{sid}", json={})
    assert (changed.status_code, resent.status_code, empty.status_code) == (200, 200, 200)

    rows = events(migrate_engine, tenant_id=people.a, target=f"service:{sid}")
    assert [r["action"] for r in rows] == ["service_created", "service_changed"]
    assert (rows[0]["details"], rows[0]["actor_user_id"]) == (None, people.both)
    assert rows[1]["actor_user_id"] == people.both
    assert rows[1]["details"] == {
        "price": {"old": {"amount_minor": 2500}, "new": {"amount_minor": 3000}}
    }


# no text ever reaches the log


def test_the_audit_log_never_holds_a_services_name_or_description_text(
    people: People, owner: TestClient, migrate_engine: Engine
) -> None:
    created = service(
        owner,
        name={"en": "Knip met Maria", "nl": "Knip met Maria"},
        description={"en": "Private note Joana"},
    )
    sid = created.json()["id"]

    renamed = owner.patch(f"/api/services/{sid}", json={"name": {"en": "Cut with Rita"}})
    # Replace semantics: sending only "en" drops "nl", it doesn't merge into the existing object.
    assert renamed.json()["name"] == {"en": "Cut with Rita"}
    owner.patch(f"/api/services/{sid}", json={"description": {"pt": "Nota Ana"}})
    owner.patch(f"/api/services/{sid}", json={"duration_minutes": 45})

    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        rows = list(
            conn.scalars(
                text("SELECT audit_events::text FROM audit_events WHERE target = :t"),
                {"t": f"service:{sid}"},
            )
        )
    assert len(rows) == 4
    assert [
        name for name in ("Maria", "Rita", "Joana", "Ana") if any(name in row for row in rows)
    ] == []

    detail_rows = events(migrate_engine, target=f"service:{sid}")
    assert detail_rows[1]["details"] == {"name": "changed"}
    assert detail_rows[2]["details"] == {"description": "changed"}
    assert detail_rows[3]["details"] == {"duration_minutes": {"old": 30, "new": 45}}


# a failed audit write rolls back the change


def test_a_failed_audit_write_rolls_back_the_create(
    people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing(monkeypatch, auth, "record")

    response = service(owner)

    assert response.status_code == 500
    assert stored(people.a) == []


def test_a_failed_audit_write_rolls_back_the_update(
    people: People, owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = service(owner).json()["id"]
    failing(monkeypatch, auth, "record")

    response = owner.patch(f"/api/services/{sid}", json={"duration_minutes": 45})

    assert response.status_code == 500
    assert stored(people.a)[0]["duration_minutes"] == 30


# the app role can never delete a service


def test_the_app_role_cannot_delete_services(people: People, owner: TestClient) -> None:
    service(owner)

    with pytest.raises(ProgrammingError) as error, tenant_context(people.a) as session:
        session.execute(text("DELETE FROM services"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


# the database's own checks


BASE_INSERT = """
INSERT INTO services (tenant_id, name, description, price_amount_minor, price_currency,
                      duration_minutes, buffer_minutes)
SELECT id, CAST(:name AS jsonb), CAST(:description AS jsonb), :amount, currency,
       :duration, :buffer
FROM tenants WHERE id = :tenant_id
"""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", '{"de":"x"}'),
        ("name", "{}"),
        ("name", "[]"),
        ("name", '"x"'),
        ("name", "null"),
        ("description", '{"fr":"x"}'),
        ("description", "[]"),
        ("amount", -1),
        ("amount", 1000001),
        ("duration", 4),
        ("duration", 721),
        ("buffer", -1),
        ("buffer", 241),
    ],
)
def test_the_database_checks_reject_bad_rows(people: People, field: str, value: Any) -> None:
    params: dict[str, Any] = {
        "tenant_id": people.a,
        "name": '{"en": "x"}',
        "description": "{}",
        "amount": 1000,
        "duration": 30,
        "buffer": None,
    }
    params[field] = value

    with pytest.raises(IntegrityError) as caught, tenant_context(people.a) as session:
        session.execute(text(BASE_INSERT), params)
    assert isinstance(caught.value.orig, CheckViolation)


def test_a_wrong_currency_is_a_foreign_key_violation(people: People) -> None:
    with pytest.raises(IntegrityError) as caught, tenant_context(people.a) as session:
        session.execute(
            text("""
            INSERT INTO services (tenant_id, name, price_amount_minor, price_currency,
                                  duration_minutes)
            VALUES (:t, '{"en":"x"}'::jsonb, 1000, 'USD', 30)
            """),
            {"t": people.a},
        )
    assert isinstance(caught.value.orig, ForeignKeyViolation)


def test_an_empty_description_with_no_buffer_is_accepted(people: People) -> None:
    with tenant_context(people.a) as session:
        session.execute(
            text(BASE_INSERT),
            {
                "tenant_id": people.a,
                "name": '{"en": "x"}',
                "description": "{}",
                "amount": 1000,
                "duration": 30,
                "buffer": None,
            },
        )
    assert len(stored(people.a)) == 1
