"""/api/clients and /api/clients/{id}/consents (ZIF-49): owners and workers alike list, search,
create and note-edit clients, and record marketing consent on their behalf."""

import inspect
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth, clients
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, events, fresh_address, fresh_email, new_client, signed_in
from tests.test_clients_db import row_of, seed_client


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def add_client(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/clients", json=body)
    assert response.status_code == 201, response.json()
    result: dict[str, Any] = response.json()
    return result


# 14: FENCE. Wrong impl: CurrentOwner in place of CurrentSession on any of the four routes.
# Second wrong impl: drop `response.headers["Cache-Control"] = "no-store"` from any of the four.
# All four answer with names, emails, phones and private notes, so none may sit in a cache.
def test_a_worker_lists_searches_and_edits_notes_like_an_owner(
    people: People, app: FastAPI
) -> None:
    worker = signed_in(app, people.a, people.only_a)

    listed = worker.get("/api/clients")
    created = worker.post("/api/clients", json={"name": "Worker Client"})
    assert (listed.status_code, created.status_code) == (200, 201)
    client_id = created.json()["id"]

    patched = worker.patch(f"/api/clients/{client_id}", json={"client_note": "note"})
    consented = worker.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
    )
    assert (patched.status_code, consented.status_code) == (200, 201)

    for response in (listed, created, patched, consented):
        assert response.headers["cache-control"] == "no-store", response.request.url


# 15: FENCE. Wrong impl: drop before/limit and return every row (the `services` shape).
def test_the_list_pages_by_id_newest_first(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    ids = [add_client(owner, {"name": f"Client {i}"})["id"] for i in range(3)]

    page = owner.get("/api/clients", params={"limit": 2}).json()
    assert [c["id"] for c in page] == list(reversed(ids))[:2]

    rest = owner.get("/api/clients", params={"before": page[-1]["id"]}).json()
    assert [c["id"] for c in rest] == [ids[0]]


# 16: GUARD.
def test_search_matches_name_email_and_phone_case_insensitively(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    by_name = add_client(owner, {"name": "Zoe Quixote"})
    by_email = add_client(owner, {"name": "Someone", "email": fresh_email()})
    by_phone = add_client(owner, {"name": "Other", "phone": "+31 6 12 34 56 78"})

    def ids_for(q: str) -> set[str]:
        return {c["id"] for c in owner.get("/api/clients", params={"q": q}).json()}

    assert ids_for("quixote") == {by_name["id"]}
    assert ids_for(by_email["email"].upper()) == {by_email["id"]}
    assert ids_for("12 34 56") == {by_phone["id"]}
    assert ids_for("no-such-substring-zzz") == set()


# 17: FENCE. Wrong impl: bind f"%{q}%" without like()'s escaping.
def test_a_search_for_a_wildcard_matches_it_literally(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    names = ["a_b", "axb", "100%", "1000 series", "50", "back\\slash"]
    ids = {name: add_client(owner, {"name": name})["id"] for name in names}

    def ids_for(q: str) -> set[str]:
        return {c["id"] for c in owner.get("/api/clients", params={"q": q}).json()}

    assert ids_for("a_b") == {ids["a_b"]}
    assert ids_for("100%") == {ids["100%"]}
    assert ids_for("\\") == {ids["back\\slash"]}


# 18: FENCE + positive control. Wrong impl: a lookup that trusts a tenant_id from the body, or
# bypasses row-level security.
def test_a_client_of_another_business_is_not_found(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client_id = seed_client(people.b, name="B Client", email=fresh_email())
    owner_a = signed_in(app, people.a, people.both)

    patched = owner_a.patch(f"/api/clients/{client_id}", json={"client_note": "note"})
    consented = owner_a.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
    )

    assert (patched.status_code, patched.json()) == (404, {"code": "not_found"})
    assert (consented.status_code, consented.json()) == (404, {"code": "not_found"})
    assert row_of(people.b, client_id)["client_note"] is None
    assert events(migrate_engine, tenant_id=people.a, action="client_changed") == []
    assert events(migrate_engine, tenant_id=people.a, action="consent_recorded") == []

    # Positive control: B's own session PATCHes that same id and gets 200 with the change applied.
    owner_b = signed_in(app, people.b, people.only_b)
    ok = owner_b.patch(f"/api/clients/{client_id}", json={"client_note": "note"})
    assert ok.status_code == 200
    assert row_of(people.b, client_id)["client_note"] == "note"


# 19: FENCE. Wrong impl: bind change.client_note/change.internal_note straight into the UPDATE.
# Second wrong impl: drop the `if not changed:` short-circuit, so a PATCH that changes nothing
# still writes a row and records a client_changed event.
def test_patching_one_note_leaves_the_other_note_alone(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Note Client"})["id"]
    assert (
        owner.patch(
            f"/api/clients/{client_id}",
            json={"client_note": "Client Kfx-1", "internal_note": "Private Mzt-2"},
        ).status_code
        == 200
    )

    only_internal = owner.patch(
        f"/api/clients/{client_id}", json={"internal_note": "Private Mzt-3"}
    )
    assert only_internal.status_code == 200
    assert only_internal.json()["client_note"] == "Client Kfx-1"
    assert row_of(people.a, uuid.UUID(client_id))["client_note"] == "Client Kfx-1"

    only_client = owner.patch(f"/api/clients/{client_id}", json={"client_note": "Client Kfx-4"})
    assert only_client.status_code == 200
    assert only_client.json()["internal_note"] == "Private Mzt-3"
    assert row_of(people.a, uuid.UUID(client_id))["internal_note"] == "Private Mzt-3"

    cleared = owner.patch(f"/api/clients/{client_id}", json={"client_note": None})
    assert cleared.status_code == 200
    assert cleared.json()["client_note"] is None
    assert cleared.json()["internal_note"] == "Private Mzt-3"

    row = row_of(people.a, uuid.UUID(client_id))
    assert row["name"] == "Note Client"
    assert row["email"] is None
    assert row["phone"] is None
    assert row["locale"] is None
    assert row["user_id"] is None

    recorded = len(events(migrate_engine, tenant_id=people.a, action="client_changed"))
    empty = owner.patch(f"/api/clients/{client_id}", json={})
    same = owner.patch(f"/api/clients/{client_id}", json={"internal_note": "Private Mzt-3"})
    assert (empty.status_code, same.status_code) == (200, 200)
    # An empty body and a body that repeats the stored value both write nothing and record
    # nothing; without the short-circuit each of them is a row and an event.
    assert len(events(migrate_engine, tenant_id=people.a, action="client_changed")) == recorded


# 20: FENCE. Wrong impl: details={"client_note": change.client_note}.
def test_the_note_event_names_the_field_and_never_the_text(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Note Event Client"})["id"]

    assert (
        owner.patch(f"/api/clients/{client_id}", json={"client_note": "Note Wvx-9"}).status_code
        == 200
    )

    recorded = events(migrate_engine, tenant_id=people.a, action="client_changed")
    assert len(recorded) == 1
    assert recorded[0]["details"] == {"client_note": "changed"}
    assert "Note Wvx-9" not in json.dumps(recorded, default=str)


# 21: FENCE, unit echo of 32. Wrong impl: add user_id: UUID | None = None to ClientIn.
def test_no_request_model_declares_a_user_id(people: People, app: FastAPI) -> None:
    assert "user_id" not in clients.ClientIn.model_fields
    assert "user_id" not in clients.NoteChange.model_fields
    assert "user_id" not in clients.ConsentIn.model_fields

    owner = signed_in(app, people.a, people.both)
    valid = owner.post("/api/clients", json={"name": "No User Id"})
    assert valid.status_code == 201


# 22: FENCE. Wrong impl: add text_shown to ConsentIn and pass it through to the insert.
def test_the_consent_text_comes_from_the_server(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Consent Text Client"})["id"]

    rejected = owner.post(
        f"/api/clients/{client_id}/consents",
        json={
            "policy_version": "2026-09-01",
            "purposes": {"sms": True},
            "text_shown": "I agree to anything",
        },
    )
    assert (rejected.status_code, rejected.json()) == (422, {"code": "invalid_request"})
    assert events(migrate_engine, tenant_id=people.a, action="consent_recorded") == []

    accepted = owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
    )
    assert accepted.status_code == 201
    with tenant_context(people.a) as session:
        stored = session.scalar(
            text("SELECT text_shown FROM consents WHERE client_id = :c"),
            {"c": uuid.UUID(client_id)},
        )
    assert stored == clients.CONSENT_TEXTS["2026-09-01"]["sms"]


# 23: FENCE. Wrong impl: drop texts_for and store body.policy_version with an empty text.
def test_an_unknown_policy_version_is_refused_before_anything_is_written(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Unknown Version Client"})["id"]

    response = owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "1999-01-01", "purposes": {"sms": True}},
    )

    assert (response.status_code, response.json()) == (422, {"code": "unknown_policy_version"})
    with tenant_context(people.a) as session:
        count = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :c"),
            {"c": uuid.UUID(client_id)},
        )
    assert count == 0


# 24: FENCE. Wrong impl: read request.headers.get("x-forwarded-for") in the route.
def test_a_merchant_consent_stores_the_connection_address_not_a_forwarded_header(
    people: People, app: FastAPI
) -> None:
    address = fresh_address()
    with tenant_context(people.a) as session:
        token = auth.create(session, people.both, ip=None, user_agent=None)
    owner = new_client(app, address)
    owner.cookies.set(auth.COOKIE, token)
    client_id = add_client(owner, {"name": "Forwarded Client"})["id"]

    response = owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
        headers={"X-Forwarded-For": "198.51.100.1", "User-Agent": "zif49-agent/1"},
    )

    assert response.status_code == 201
    with tenant_context(people.a) as session:
        stored = session.execute(
            text("SELECT host(ip) AS ip, user_agent FROM consents WHERE client_id = :c"),
            {"c": uuid.UUID(client_id)},
        ).one()
    assert stored.ip == address
    assert stored.user_agent == "zif49-agent/1"


# 25: GUARD.
def test_the_console_consent_route_records_one_event_naming_the_purposes(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Purpose Client"})["id"]

    response = owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True, "marketing_email": False}},
    )
    assert response.status_code == 201

    recorded = events(migrate_engine, tenant_id=people.a, action="consent_recorded")
    assert len(recorded) == 1
    assert recorded[0]["target"] == f"client:{client_id}"
    assert recorded[0]["details"] == {"purposes": ["marketing_email", "sms"]}
    assert recorded[0]["actor_user_id"] == people.both


# 25b: FENCE. record_consents unnests three parallel arrays. Wrong impl 1: `reversed(ordered)` for
# the granted array, which stores a withdrawal as a grant. Wrong impl 2: the same for the texts
# array, which files the wrong Art. 7(1) wording against a purpose. Neither is visible unless one
# call disagrees with itself across purposes.
def test_each_purpose_stores_its_own_answer_and_its_own_wording(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Mixed Answer Client"})["id"]

    response = owner.post(
        f"/api/clients/{client_id}/consents",
        json={
            "policy_version": "2026-09-01",
            "purposes": {"sms": True, "marketing_email": False, "whatsapp": True},
        },
    )

    assert response.status_code == 201
    assert response.json()["consents"] == {
        "marketing_email": {"granted": False, "source": "merchant", "mailable": False},
        "sms": {"granted": True, "source": "merchant", "mailable": True},
        "whatsapp": {"granted": True, "source": "merchant", "mailable": True},
    }
    with tenant_context(people.a) as session:
        stored = session.execute(
            text("""
            SELECT purpose, granted, text_shown FROM consents
            WHERE client_id = :c ORDER BY purpose
            """),
            {"c": uuid.UUID(client_id)},
        ).all()
    texts = clients.CONSENT_TEXTS["2026-09-01"]
    assert [tuple(row) for row in stored] == [
        ("marketing_email", False, texts["marketing_email"]),
        ("sms", True, texts["sms"]),
        ("whatsapp", True, texts["whatsapp"]),
    ]


# 26: FENCE (structural). Wrong impl: move auth.record(..., "consent_recorded", ...) out of the
# route and into record_consents, forcing a Request parameter onto it.
def test_the_booking_path_cannot_record_an_audit_event(
    people: People, migrate_engine: Engine
) -> None:
    parameters = inspect.signature(clients.record_consents).parameters
    assert "request" not in parameters
    assert not any(p.annotation is Request for p in parameters.values())

    client_id = seed_client(people.a, email=fresh_email())
    with tenant_context(people.a) as session:
        clients.record_consents(
            session,
            client_id=client_id,
            policy_version="2026-09-01",
            purposes={"sms": True},
            source="booking_page",
            ip=None,
            user_agent=None,
        )

    assert events(migrate_engine, tenant_id=people.a, action="consent_recorded") == []
    with tenant_context(people.a) as session:
        count = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :c"), {"c": client_id}
        )
    assert count == 1


# 27: GUARD.
def test_every_clients_endpoint_needs_a_session_and_json(people: People, app: FastAPI) -> None:
    anon = new_client(app)
    owner = signed_in(app, people.a, people.both)
    existing_id = add_client(owner, {"name": "Session Client"})["id"]

    for method, path, body in [
        ("GET", "/api/clients", None),
        ("POST", "/api/clients", {"name": "X"}),
        ("PATCH", f"/api/clients/{existing_id}", {"client_note": "x"}),
        (
            "POST",
            f"/api/clients/{existing_id}/consents",
            {"policy_version": "2026-09-01", "purposes": {"sms": True}},
        ),
    ]:
        response = anon.request(method, path, json=body)
        assert (response.status_code, response.json()) == (401, {"code": "unauthenticated"})

    for method, path in [
        ("POST", "/api/clients"),
        ("PATCH", f"/api/clients/{existing_id}"),
        ("POST", f"/api/clients/{existing_id}/consents"),
    ]:
        response = new_client(app).request(
            method, path, content="x", headers={"content-type": "text/plain"}
        )
        assert (response.status_code, response.json()) == (415, {"code": "unsupported_media_type"})


# 28: GUARD.
def test_a_non_uuid_client_id_is_refused(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)

    patched = owner.patch("/api/clients/not-a-uuid", json={"client_note": "x"})
    consented = owner.post(
        "/api/clients/not-a-uuid/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
    )

    assert (patched.status_code, patched.json()) == (422, {"code": "invalid_request"})
    assert (consented.status_code, consented.json()) == (422, {"code": "invalid_request"})


# 29: GUARD.
def test_creating_a_client_whose_email_already_exists_is_a_conflict(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()
    first = add_client(owner, {"name": "Original Name", "email": email})

    conflict = owner.post("/api/clients", json={"name": "New Name", "email": email})

    assert (conflict.status_code, conflict.json()) == (409, {"code": "email_taken"})
    assert row_of(people.a, uuid.UUID(first["id"]))["name"] == "Original Name"
    with tenant_context(people.a) as session:
        count = session.scalar(text("SELECT count(*) FROM clients WHERE email = :e"), {"e": email})
    assert count == 1


# 30: GUARD.
def test_a_client_answer_carries_the_current_consent_per_purpose(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Consent Answer Client"})["id"]
    owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"sms": True, "whatsapp": True}},
    )
    owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {"whatsapp": False}},
    )

    listed = next(c for c in owner.get("/api/clients").json() if c["id"] == client_id)
    assert listed["consents"] == {
        "sms": {"granted": True, "source": "merchant", "mailable": True},
        "whatsapp": {"granted": False, "source": "merchant", "mailable": False},
    }
    # The withdrawal did not replace the grant: both rows are kept, which is what append-only
    # means for a reader. The privilege tests only say an UPDATE is refused.
    with tenant_context(people.a) as session:
        kept = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :c AND purpose = 'whatsapp'"),
            {"c": uuid.UUID(client_id)},
        )
    assert kept == 2

    unchanged = owner.patch(f"/api/clients/{client_id}", json={})
    assert unchanged.status_code == 200
    assert unchanged.json()["consents"] == listed["consents"]

    changed = owner.patch(f"/api/clients/{client_id}", json={"client_note": "note"})
    assert changed.status_code == 200
    assert changed.json()["consents"] == listed["consents"]


# 30b: GUARD.
def test_creating_a_client_records_one_event_naming_the_client(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    email = fresh_email()
    created = owner.post(
        "/api/clients",
        json={
            "name": "Event Client Qzx",
            "email": email,
            "phone": "+31 6 99 88 77 66",
            "locale": "nl",
        },
    )
    assert created.status_code == 201
    # The answer echoes what was sent: ClientOut could return null for any of these with every
    # other test in this file still green.
    assert created.json()["name"] == "Event Client Qzx"
    assert created.json()["email"] == email
    assert created.json()["phone"] == "+31 6 99 88 77 66"
    assert created.json()["locale"] == "nl"

    recorded = events(migrate_engine, tenant_id=people.a, action="client_created")
    assert len(recorded) == 1
    assert recorded[0]["actor_user_id"] == people.both
    assert recorded[0]["target"] == f"client:{created.json()['id']}"
    assert recorded[0]["details"] is None
    dumped = json.dumps(recorded, default=str)
    assert "Event Client Qzx" not in dumped
    assert created.json()["email"] not in dumped
    assert "+31 6 99 88 77 66" not in dumped


# 30c: GUARD.
def test_a_consent_call_must_name_at_least_one_purpose(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Empty Purposes Client"})["id"]

    response = owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "2026-09-01", "purposes": {}},
    )

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    with tenant_context(people.a) as session:
        count = session.scalar(
            text("SELECT count(*) FROM consents WHERE client_id = :c"),
            {"c": uuid.UUID(client_id)},
        )
    assert count == 0


# 30d: FENCE. Wrong impl: type ClientName, Phone, NoteText and PolicyVersion as bare `str` and run
# `make openapi`. Nothing else in the suite sends a value any of the four constraints would refuse,
# so the only witness today is the committed contract - which `make openapi` rewrites.
def test_the_request_constraints_are_enforced(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    client_id = add_client(owner, {"name": "Constraints Client"})["id"]
    refused = (422, {"code": "invalid_request"})

    def post(body: dict[str, Any]) -> tuple[int, Any]:
        response = owner.post("/api/clients", json=body)
        return response.status_code, response.json()

    def patch(body: dict[str, Any]) -> tuple[int, Any]:
        response = owner.patch(f"/api/clients/{client_id}", json=body)
        return response.status_code, response.json()

    assert post({"name": "x" * 101}) == refused  # ClientName's max_length
    assert post({"name": "Zero​width"}) == refused  # ClientName's printable validator
    assert post({"name": "Ok", "phone": "+31 6 "}) == refused  # Phone's printable validator
    assert patch({"internal_note": "x" * 2001}) == refused  # NoteText's max_length
    assert patch({"client_note": "a\tb"}) == refused  # NoteText's multiline validator: \n only
    # PolicyVersion's max_length. A 40-character version we don't publish is a different 422
    # (unknown_policy_version), so this one has to be over-length to say anything.
    over_long_version = owner.post(
        f"/api/clients/{client_id}/consents",
        json={"policy_version": "x" * 41, "purposes": {"sms": True}},
    )
    assert (over_long_version.status_code, over_long_version.json()) == refused

    # A note may still hold a newline, and a mixed-case address is normalised rather than refused:
    # the five refusals above are not a blanket "anything unusual is a 422".
    assert patch({"client_note": "line one\nline two"})[0] == 200
    address = fresh_email()
    assert add_client(owner, {"name": "Mixed Case", "email": address.upper()})["email"] == address
