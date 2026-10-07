"""POST /api/bookings, POST /api/bookings/{id}/reschedule and GET /api/services/{id}/availability
(ZIF-57 PR3): a member books, moves and looks up times from the calendar. Each fence names the
wrong implementation it kills (docs/specs/2026-09-30-zif-57-pr3-spec.md, tests F1-F20)."""

import json
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, event, text

from app.db import tenant_context
from tests.conftest import (
    People,
    add_membership,
    add_user,
    member_id,
    new_client,
    save_setting,
    signed_in,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_email import jobs_for_booking, template_of
from tests.test_bookings_api import DAY, at, post_booking
from tests.test_bookings_approval_api import expire
from tests.test_working_hours import seed


@pytest.fixture
def app(people: People) -> FastAPI:
    from app.main import create_app

    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day. Not published:
    the merchant routes must not depend on it (F1)."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


@pytest.fixture
def team(people: People, ready: str) -> str:
    """The same service, also performed by Ana (people.only_a, a worker)."""
    seed(people.a, people.only_a, weekdays("09:00", "17:00"))
    assign(people.a, ready, member_id(people.a, people.only_a))
    return ready


@pytest.fixture
def ana(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.only_a)


def boss(people: People) -> uuid.UUID:
    return member_id(people.a, people.both)


def ana_id(people: People) -> uuid.UUID:
    return member_id(people.a, people.only_a)


def book(client: TestClient, service_id: str, **body: Any) -> Response:
    payload: dict[str, Any] = {
        "service_id": service_id,
        "new_client": {"name": "Walk-in"},
        "starts_at": at("09:00"),
        **body,
    }
    payload = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in payload.items()}
    return client.post("/api/bookings", json=payload)


def move(client: TestClient, booking_id: object, **body: Any) -> Response:
    payload = {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in body.items()}
    return client.post(f"/api/bookings/{booking_id}/reschedule", json=payload)


def row_of(tenant_id: uuid.UUID, booking_id: str) -> dict[str, Any]:
    with tenant_context(tenant_id) as session:
        row = session.execute(
            text("SELECT * FROM bookings WHERE id = :id"), {"id": booking_id}
        ).mappings()
        return dict(row.one())


def events_of(tenant_id: uuid.UUID, booking_id: str) -> list[dict[str, Any]]:
    with tenant_context(tenant_id) as session:
        rows = session.execute(
            text("SELECT * FROM booking_events WHERE booking_id = :id ORDER BY id"),
            {"id": booking_id},
        ).mappings()
        return [dict(r) for r in rows]


def clients_named(tenant_id: uuid.UUID, name: str) -> int:
    with tenant_context(tenant_id) as session:
        count: int = session.scalar(
            text("SELECT count(*) FROM clients WHERE name = :n"), {"n": name}
        )
    return count


def jobs(app_engine: Engine, tenant_id: uuid.UUID, booking_id: str) -> list[dict[str, Any]]:
    return jobs_for_booking(app_engine, tenant_id, booking_id)


def payload_of(job: dict[str, Any]) -> dict[str, Any]:
    payload = job["payload"]
    out: dict[str, Any] = json.loads(payload) if isinstance(payload, str) else payload
    return out


def mails(
    app_engine: Engine, tenant_id: uuid.UUID, booking_id: str, template: str
) -> list[dict[str, Any]]:
    return [j for j in jobs(app_engine, tenant_id, booking_id) if template_of(j) == template]


def clear_jobs(app_engine: Engine, tenant_id: uuid.UUID) -> None:
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE tenant_id = :t"), {"t": tenant_id})


def in_a_week(clock: str, days: int = 6) -> str:
    return at(clock, DAY + timedelta(days=days))


def yesterday(clock: str) -> str:
    return at(clock, date.today() - timedelta(days=1))


def availability(client: TestClient, service_id: str, **params: Any) -> Response:
    return client.get(
        f"/api/services/{service_id}/availability",
        params={"from": DAY.isoformat(), "to": DAY.isoformat(), **params},
    )


# F1. Kills: reusing the public publish gate, or ungating the wrong route.
def test_f1_an_unpublished_business_can_still_book_and_look_up_times_by_hand(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    assert book(owner, ready, member_id=boss(people)).status_code == 201
    response = availability(owner, ready)
    assert response.status_code == 200
    assert response.json()["slots"]
    assert len(response.json()["slot_workers"]) == len(response.json()["slots"])
    public = new_client(app).get(
        f"/api/public/businesses/{people.a}/services/{ready}/availability",
        params={"from": DAY.isoformat(), "to": DAY.isoformat()},
    )
    assert public.status_code == 404


# F2. Kills: calling create()'s gating (limits, Turnstile) or PENDING_COUNT.
def test_f2_no_public_gating_no_turnstile_no_pending_cap(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_setting(people.a, "published", True)
    save_setting(people.a, "max_pending_per_email", 1)
    email = f"{uuid.uuid4()}@example.com"
    pending = post_booking(new_client(app), people.a, ready, email=email, starts_at=at("10:00"))
    assert pending.status_code == 201 and pending.json()["status"] == "pending"
    client_id = owner.get("/api/clients", params={"q": email}).json()[0]["id"]
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    calls: list[object] = []

    def spy(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("the merchant route must never call Turnstile")

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    base = datetime.fromisoformat(at("00:00", DAY + timedelta(days=1)).replace("Z", "+00:00"))
    for i in range(31):
        starts = (base + timedelta(minutes=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = book(
            owner,
            ready,
            client_id=client_id,
            new_client=None,
            member_id=boss(people),
            starts_at=starts,
            override=True,
        )
        assert response.status_code == 201, (i, response.json())
    assert calls == []


# F3. Kills: status from auto_confirm, source booking_page, NULL actor, consent 'null' jsonb.
def test_f3_the_row_the_event_and_the_history(
    people: People, owner: TestClient, ready: str
) -> None:
    response = book(owner, ready, member_id=boss(people), new_client={"name": "Dana"})
    assert response.status_code == 201, response.json()
    booking_id = response.json()["id"]
    row = row_of(people.a, booking_id)
    assert (row["status"], row["source"], row["expires_at"]) == ("confirmed", "merchant", None)
    (created,) = events_of(people.a, booking_id)
    assert created["event"] == "created"
    assert created["actor_user_id"] == people.both
    assert created["consent_purposes"] is None and created["policy_version"] is None
    history = owner.get(f"/api/bookings/{booking_id}").json()["history"]
    assert [(h["event"], h["actor"]) for h in history] == [("created", "team")]
    assert history[0]["actor_name"]


# F4. Kills: override ignored, or always on.
def test_f4_an_off_grid_start_needs_override(people: People, owner: TestClient, ready: str) -> None:
    off = at("09:07")
    refused = book(owner, ready, member_id=boss(people), starts_at=off)
    assert (refused.status_code, refused.json()["code"]) == (409, "slot_unavailable")
    assert (
        book(owner, ready, member_id=boss(people), starts_at=off, override=True).status_code == 201
    )


# F5. Kills: swallowing 23P01, a client insert outside the transaction.
def test_f5_an_override_onto_a_taken_slot_is_409_and_leaves_no_client(
    people: People, owner: TestClient, ready: str
) -> None:
    assert book(owner, ready, member_id=boss(people)).status_code == 201
    response = book(
        owner,
        ready,
        member_id=boss(people),
        override=True,
        new_client={"name": "Ghost Client"},
    )
    assert (response.status_code, response.json()["code"]) == (409, "slot_taken")
    assert clients_named(people.a, "Ghost Client") == 0


# F6. Kills: missing EXPIRE (a lapsed, unswept pending still holds the slot in the constraint).
def test_f6_an_override_onto_a_lapsed_pending_works(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    save_setting(people.a, "published", True)
    pending = post_booking(new_client(app), people.a, ready, starts_at=at("09:00"))
    assert pending.json()["status"] == "pending"
    expire(people.a, pending.json()["id"])
    response = book(owner, ready, member_id=boss(people), override=True)
    assert response.status_code == 201, response.json()


# F7. Kills: an off-grid "anyone" picking by load.
def test_f7_override_needs_a_person(people: People, owner: TestClient, ready: str) -> None:
    response = book(owner, ready, override=True)
    assert (response.status_code, response.json()["code"]) == (422, "member_required")


# F8. Kills: no may_manage on create.
def test_f8_a_worker_books_only_for_themselves(people: People, ana: TestClient, team: str) -> None:
    assert book(ana, team, member_id=ana_id(people)).status_code == 201
    colleague = book(ana, team, member_id=boss(people), starts_at=at("10:00"))
    assert (colleague.status_code, colleague.json()["code"]) == (403, "owner_only")
    anyone = book(ana, team, starts_at=at("10:00"))
    assert (anyone.status_code, anyone.json()["code"]) == (403, "owner_only")


# F9. Kills: find_or_create's upsert.
def test_f9_a_new_client_is_a_plain_insert(people: People, owner: TestClient, ready: str) -> None:
    first = book(owner, ready, member_id=boss(people), new_client={"name": "No Mail"})
    assert first.status_code == 201
    with tenant_context(people.a) as session:
        email = session.scalar(text("SELECT email FROM clients WHERE name = 'No Mail'"))
    assert email is None
    mail = f"{uuid.uuid4()}@example.com"
    assert owner.post("/api/clients", json={"name": "Taken", "email": mail}).status_code == 201
    clash = book(
        owner,
        ready,
        member_id=boss(people),
        starts_at=at("10:00"),
        new_client={"name": "Other Name", "email": mail},
    )
    assert (clash.status_code, clash.json()["code"]) == (409, "email_taken")
    assert clients_named(people.a, "Taken") == 1 and clients_named(people.a, "Other Name") == 0


# F10. Kills: relying on the foreign key (a 500), or seeing another business's client.
def test_f10_an_unknown_or_foreign_client_is_422(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    other = signed_in(app, people.b, people.both)
    foreign = other.post("/api/clients", json={"name": "Elsewhere"}).json()["id"]
    for client_id in (foreign, str(uuid.uuid4())):
        response = book(owner, ready, member_id=boss(people), client_id=client_id, new_client=None)
        assert (response.status_code, response.json()["code"]) == (422, "unknown_client")


# G. Exactly one of client_id / new_client.
def test_a_client_is_named_exactly_one_way(people: People, owner: TestClient, ready: str) -> None:
    neither = book(owner, ready, member_id=boss(people), new_client=None)
    both = book(owner, ready, member_id=boss(people), client_id=str(uuid.uuid4()))
    assert neither.status_code == 422 and both.status_code == 422


# F11 (a GUARD, not a fence: the real guard is the refactor-first commit with the existing suite
# green). The snapshot columns equal what the settings said.
def test_f11_the_snapshot_columns_match_the_settings(
    people: People, owner: TestClient, ready: str
) -> None:
    save_setting(people.a, "free_cancellation_hours", 12)
    save_setting(people.a, "reschedule_cutoff_hours", 6)
    save_setting(people.a, "max_reschedules", 3)
    save_setting(people.a, "cancellation_policy_text", "Be nice.")
    booking_id = book(owner, ready, member_id=boss(people)).json()["id"]
    row = row_of(people.a, booking_id)
    assert row["free_cancellation_hours"] == 12
    assert row["reschedule_cutoff_hours"] == 6
    assert row["max_reschedules"] == 3
    assert row["cancellation_policy_text"] == "Be nice."
    assert row["original_starts_at"] == row["earliest_starts_at"] == row["starts_at"]
    assert row["auto_confirm_at_booking"] is not None and row["reschedule_count"] == 0


# F12a. Kills: copying the public emails (booking_request, email_merchants fan-out).
def test_f12_an_owner_books_onto_ana_ana_gets_one_booking_new_the_owner_none(
    people: People, owner: TestClient, team: str, app_engine: Engine
) -> None:
    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    sent = jobs(app_engine, people.a, booking_id)
    templates = sorted(template_of(j) for j in sent)
    assert templates == ["booking_confirmed", "booking_new", "booking_reminder"]
    (new,) = mails(app_engine, people.a, booking_id, "booking_new")
    assert payload_of(new)["user_id"] == str(people.only_a)
    assert payload_of(new)["starts_at"]
    assert str(people.a) in new["dedupe_key"] and str(people.only_a) in new["dedupe_key"]
    assert str(people.both) not in " ".join(j["dedupe_key"] for j in sent)


# F12b. Kills: emailing the actor about their own booking, or fanning out to every owner.
def test_f12_ana_books_herself_no_team_email(
    people: People, ana: TestClient, team: str, app_engine: Engine
) -> None:
    booking_id = book(ana, team, member_id=ana_id(people)).json()["id"]
    assert mails(app_engine, people.a, booking_id, "booking_new") == []
    assert mails(app_engine, people.a, booking_id, "booking_request") == []
    assert len(mails(app_engine, people.a, booking_id, "booking_confirmed")) == 1


# F12c. Kills: the worker's user taken from the request instead of the placed row.
def test_f12_anyone_resolves_to_a_worker_and_that_worker_is_emailed(
    people: People, owner: TestClient, team: str, app_engine: Engine
) -> None:
    # Make the owner busy so least-loaded lands on Ana.
    assert book(owner, team, member_id=boss(people), starts_at=at("11:00")).status_code == 201
    response = book(owner, team, starts_at=at("09:00"))
    assert response.status_code == 201, response.json()
    assert response.json()["worker_id"] == str(ana_id(people))
    (new,) = mails(app_engine, people.a, response.json()["id"], "booking_new")
    assert payload_of(new)["user_id"] == str(people.only_a)


# Past starts: queue no client confirmation and no team email.
def test_a_booking_recorded_after_the_fact_sends_nothing(
    people: People, owner: TestClient, team: str, app_engine: Engine
) -> None:
    response = book(
        owner, team, member_id=ana_id(people), starts_at=yesterday("10:00"), override=True
    )
    assert response.status_code == 201, response.json()
    assert jobs(app_engine, people.a, response.json()["id"]) == []


# F13. Kills: exclude dropped.
def test_f13_exclude_offers_the_bookings_own_slot(
    people: People, owner: TestClient, ready: str
) -> None:
    booking_id = book(owner, ready, member_id=boss(people)).json()["id"]
    own = at("09:00").replace("Z", "+00:00")

    def starts(**p: Any) -> list[datetime]:
        body = availability(owner, ready, **p).json()
        return [datetime.fromisoformat(s) for s in body["slots"]]

    assert datetime.fromisoformat(own) not in starts()
    assert datetime.fromisoformat(own) in starts(exclude=booking_id)


# exclude is honoured only when the caller may manage that booking's worker; otherwise 404.
def test_exclude_of_a_colleagues_booking_is_404_for_a_worker(
    people: People, owner: TestClient, ana: TestClient, team: str
) -> None:
    booking_id = book(owner, team, member_id=boss(people)).json()["id"]
    assert availability(ana, team, exclude=booking_id).status_code == 404
    assert availability(owner, team, exclude=booking_id).status_code == 200
    assert availability(ana, team, exclude=str(uuid.uuid4())).status_code == 404


def test_the_merchant_availability_checks_its_inputs(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    wide = owner.get(
        f"/api/services/{ready}/availability",
        params={"from": DAY.isoformat(), "to": (DAY + timedelta(days=14)).isoformat()},
    )
    assert (wide.status_code, wide.json()["code"]) == (422, "invalid_range")
    assert availability(owner, str(uuid.uuid4())).status_code == 404
    assert availability(new_client(app), ready).status_code == 401


# F14. Kills: reusing RESCHEDULE (count + 1), a NULL actor.
def test_f14_a_move_keeps_the_clients_allowance_and_records_the_team(
    people: People,
    app_engine: Engine,
    owner: TestClient,
    ready: str,
) -> None:
    booking_id = book(owner, ready, member_id=boss(people)).json()["id"]
    # A session that hands timestamps back in Amsterdam time, so only an explicit .astimezone(UTC)
    # yields +00:00; a UTC session would pass either way. Set on each new connection and committed,
    # so the pool's reset-on-return rollback can't undo it.
    from app.db import SessionLocal

    engine = SessionLocal.kw["bind"]

    def amsterdam(dbapi_connection: Any, _record: object) -> None:
        dbapi_connection.execute("SET TIME ZONE 'Europe/Amsterdam'")
        dbapi_connection.commit()

    event.listen(engine, "connect", amsterdam)
    engine.dispose()
    try:
        response = move(owner, booking_id, starts_at=at("11:00"))
    finally:
        event.remove(engine, "connect", amsterdam)
        engine.dispose()  # no Amsterdam connections left in the shared pool
    assert response.status_code == 200, response.json()
    assert datetime.fromisoformat(response.json()["starts_at"]) == datetime.fromisoformat(
        at("11:00").replace("Z", "+00:00")
    )
    assert row_of(people.a, booking_id)["reschedule_count"] == 0
    last = events_of(people.a, booking_id)[-1]
    assert (last["event"], last["actor_user_id"]) == ("rescheduled", people.both)
    assert set(last["details"]) == {"from", "to"}
    assert last["details"] == {
        "from": at("09:00").replace("Z", "+00:00"),
        "to": at("11:00").replace("Z", "+00:00"),
    }
    history = owner.get(f"/api/bookings/{booking_id}").json()["history"]
    assert history[-1]["actor"] == "team"


# F15. Kills: allowing a pending (D7).
def test_f15_only_a_confirmed_booking_moves(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    save_setting(people.a, "published", True)
    pending = post_booking(new_client(app), people.a, ready, starts_at=at("09:00")).json()["id"]
    refused = move(owner, pending, starts_at=at("11:00"))
    assert (refused.status_code, refused.json()["code"]) == (409, "invalid_transition")
    booking_id = book(owner, ready, member_id=boss(people), starts_at=at("12:00")).json()["id"]
    assert (
        owner.patch(
            f"/api/bookings/{booking_id}", json={"status": "cancelled_by_merchant"}
        ).status_code
        == 200
    )
    assert move(owner, booking_id, starts_at=at("13:00")).status_code == 409


# F16. Kills: a missing 23P01 map, an ignored override.
def test_f16_overlap_and_off_grid(people: People, owner: TestClient, ready: str) -> None:
    mine = book(owner, ready, member_id=boss(people), starts_at=at("10:00")).json()["id"]
    book(owner, ready, member_id=boss(people), starts_at=at("09:00"))
    taken = move(owner, mine, starts_at=at("09:00"), override=True)
    assert (taken.status_code, taken.json()["code"]) == (409, "slot_taken")
    off = move(owner, mine, starts_at=at("11:07"))
    assert (off.status_code, off.json()["code"]) == (409, "slot_unavailable")
    assert move(owner, mine, starts_at=at("11:07"), override=True).status_code == 200


# F17. Kills: SET earliest_starts_at = :new (anchor raised, or the CHECK).
def test_f17_earliest_follows_only_an_earlier_move(
    people: People, owner: TestClient, ready: str
) -> None:
    booking_id = book(owner, ready, member_id=boss(people), starts_at=at("11:00")).json()["id"]
    original = row_of(people.a, booking_id)["earliest_starts_at"]
    assert move(owner, booking_id, starts_at=at("14:00")).status_code == 200
    assert row_of(people.a, booking_id)["earliest_starts_at"] == original
    assert move(owner, booking_id, starts_at=at("09:30")).status_code == 200
    row = row_of(people.a, booking_id)
    assert row["earliest_starts_at"] == row["starts_at"] != original


# F18. Kills: checking only the booking's worker.
def test_f18_a_worker_moves_only_their_own_and_cannot_hand_over(
    people: People, owner: TestClient, ana: TestClient, team: str
) -> None:
    hers = book(owner, team, member_id=ana_id(people), starts_at=at("09:00")).json()["id"]
    his = book(owner, team, member_id=boss(people), starts_at=at("09:00")).json()["id"]
    assert move(ana, hers, starts_at=at("10:00")).status_code == 200
    colleagues = move(ana, his, starts_at=at("10:00"))
    assert (colleagues.status_code, colleagues.json()["code"]) == (403, "owner_only")
    handed = move(ana, hers, starts_at=at("11:00"), member_id=boss(people))
    assert (handed.status_code, handed.json()["code"]) == (403, "owner_only")


# F19. Kills: a `:r{count}` suffix (A -> B -> A would dedupe).
def test_f19_a_move_back_is_a_new_confirmation(
    people: People, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    booking_id = book(owner, ready, member_id=boss(people), starts_at=at("11:00")).json()["id"]
    clear_jobs(app_engine, people.a)
    assert move(owner, booking_id, starts_at=at("14:00")).status_code == 200
    assert move(owner, booking_id, starts_at=at("11:00")).status_code == 200
    confirmations = mails(app_engine, people.a, booking_id, "booking_confirmed")
    assert len(confirmations) == 2
    assert len({j["dedupe_key"] for j in confirmations}) == 2
    starts = sorted(payload_of(j)["starts_at"] for j in confirmations)
    assert starts[0] != starts[1]
    latest = datetime.fromisoformat(at("11:00").replace("Z", "+00:00"))
    assert latest in {datetime.fromisoformat(s) for s in starts}


# F20. Kills: a stale worker_display_name.
def test_f20_moving_to_another_member_updates_the_snapshot(
    people: People, app: FastAPI, owner: TestClient, team: str
) -> None:
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE memberships SET display_name = :n WHERE id = :m"),
            [
                {"n": "Boss", "m": boss(people)},
                {"n": "Ana", "m": ana_id(people)},
            ],
        )
    booking_id = book(owner, team, member_id=boss(people)).json()["id"]
    assert row_of(people.a, booking_id)["worker_display_name"] == "Boss"
    response = move(owner, booking_id, starts_at=at("09:00"), member_id=ana_id(people))
    assert response.status_code == 200, response.json()
    row = row_of(people.a, booking_id)
    assert row["worker_id"] == ana_id(people)
    assert row["worker_display_name"] == "Ana"


# Sign-off r1. Kills: a member the service does not use (override must not bypass candidates).
def test_an_override_move_onto_a_member_without_the_service_is_409(
    people: People, app_engine: Engine, owner: TestClient, ready: str
) -> None:
    stranger = add_user(app_engine)
    add_membership(people.a, stranger)
    booking_id = book(owner, ready, member_id=boss(people)).json()["id"]
    before = row_of(people.a, booking_id)
    response = move(
        owner,
        booking_id,
        starts_at=at("11:00"),
        member_id=member_id(people.a, stranger),
        override=True,
    )
    assert (response.status_code, response.json()["code"]) == (409, "slot_unavailable")
    assert row_of(people.a, booking_id) == before


def test_unchanged_is_422(people: People, owner: TestClient, ready: str) -> None:
    booking_id = book(owner, ready, member_id=boss(people)).json()["id"]
    response = move(owner, booking_id, starts_at=at("09:00"))
    assert (response.status_code, response.json()["code"]) == (422, "unchanged")


# F12d-g. Kills: the wrong recipients, a missing or stale payload, a deduping key, the new worker
# told "now with" themselves.
def test_f12_owner_moves_anas_booking_to_themselves_only_ana_is_told(
    people: People, app_engine: Engine, owner: TestClient, team: str
) -> None:
    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    clear_jobs(app_engine, people.a)
    response = move(owner, booking_id, starts_at=at("10:00"), member_id=boss(people))
    assert response.status_code == 200, response.json()
    sent = mails(app_engine, people.a, booking_id, "booking_moved_team")
    # Ben here is the owner himself (the actor): only Ana, the old worker, is told.
    assert [payload_of(j)["user_id"] for j in sent] == [str(people.only_a)]
    assert payload_of(sent[0])["previous_starts_at"] and payload_of(sent[0])["starts_at"]
    assert mails(app_engine, people.a, booking_id, "booking_new") == []


def test_f12_owner_moves_between_two_workers_old_hears_moved_new_hears_new_booking(
    people: People, app_engine: Engine, owner: TestClient, team: str
) -> None:
    ben = add_user(app_engine)
    add_membership(people.a, ben)
    seed(people.a, ben, weekdays("09:00", "17:00"))
    assign(people.a, team, member_id(people.a, ben))
    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    clear_jobs(app_engine, people.a)
    assert (
        move(
            owner, booking_id, starts_at=at("10:00"), member_id=member_id(people.a, ben)
        ).status_code
        == 200
    )
    moved = mails(app_engine, people.a, booking_id, "booking_moved_team")
    assert [payload_of(j)["user_id"] for j in moved] == [str(people.only_a)]
    assert payload_of(moved[0])["member_changed"] == "1"
    new = mails(app_engine, people.a, booking_id, "booking_new")
    assert [payload_of(j)["user_id"] for j in new] == [str(ben)]
    assert "member_changed" not in payload_of(new[0])
    keys = [j["dedupe_key"] for j in moved + new]
    assert len(keys) == len(set(keys)) == 2


def test_f12_a_move_in_time_only_tells_the_one_worker(
    people: People, app_engine: Engine, owner: TestClient, team: str
) -> None:
    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    clear_jobs(app_engine, people.a)
    assert move(owner, booking_id, starts_at=at("10:00")).status_code == 200
    sent = mails(app_engine, people.a, booking_id, "booking_moved_team")
    assert [payload_of(j)["user_id"] for j in sent] == [str(people.only_a)]
    assert "member_changed" not in payload_of(sent[0])


def test_f12_anas_own_move_tells_nobody(
    people: People, app_engine: Engine, owner: TestClient, ana: TestClient, team: str
) -> None:
    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    clear_jobs(app_engine, people.a)
    assert move(ana, booking_id, starts_at=at("10:00")).status_code == 200
    assert mails(app_engine, people.a, booking_id, "booking_moved_team") == []


def test_f12_a_move_there_and_back_is_two_emails(
    people: People, app_engine: Engine, owner: TestClient, team: str
) -> None:
    booking_id = book(owner, team, member_id=ana_id(people), starts_at=at("11:00")).json()["id"]
    clear_jobs(app_engine, people.a)
    assert move(owner, booking_id, starts_at=at("14:00")).status_code == 200
    assert move(owner, booking_id, starts_at=at("11:00")).status_code == 200
    sent = mails(app_engine, people.a, booking_id, "booking_moved_team")
    assert len(sent) == 2 and len({j["dedupe_key"] for j in sent}) == 2


def test_a_move_into_the_past_sends_nothing(
    people: People, app_engine: Engine, owner: TestClient, team: str
) -> None:
    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    clear_jobs(app_engine, people.a)
    response = move(owner, booking_id, starts_at=yesterday("10:00"), override=True)
    assert response.status_code == 200, response.json()
    assert jobs(app_engine, people.a, booking_id) == []


# G1. Kills: route shadowing, duplicate operation ids, a missing audit action.
def test_g1_old_routes_still_route_and_the_audit_trail_is_written(
    people: People, owner: TestClient, ready: str, app: FastAPI, migrate_engine: Engine
) -> None:
    assert owner.get("/api/bookings/pending").status_code == 200
    booking_id = book(owner, ready, member_id=boss(people)).json()["id"]
    assert owner.get(f"/api/bookings/{booking_id}").status_code == 200
    assert move(owner, booking_id, starts_at=at("11:00")).status_code == 200
    ids = [op["operationId"] for p in app.openapi()["paths"].values() for op in p.values()]
    assert len(ids) == len(set(ids))
    from tests.conftest import events as audit

    for action in ("booking_created", "booking_rescheduled"):
        rows = audit(migrate_engine, action=action, target=f"booking:{booking_id}")
        assert len(rows) == 1 and rows[0]["actor_user_id"] == people.both


# The mail: booking_moved_team is a status-gated template in every locale, with and without the
# new member.
def test_the_team_template_set_is_complete() -> None:
    from app.mail import LOCALES, STATUS_FOR, render

    assert STATUS_FOR["booking_moved_team"] == "confirmed"
    values = {
        "business": "Biz",
        "service": "SVC-MARKER",
        "date": "DATE-MARKER",
        "time": "TIME-MARKER",
        "zone": "Europe/Amsterdam",
        "client": "Client Name",
        "link": "https://example.com/en",
        "old_date": "OLD-DATE-MARKER",
        "old_time": "OLD-TIME-MARKER",
        "member": "MEMBER-MARKER",
    }
    for template in ("booking_moved_team", "booking_moved_team_member"):
        for locale in LOCALES:
            subject, body = render(template, locale, values)
            assert subject and "Client Name" in body and "OLD-DATE-MARKER" in body, (
                template,
                locale,
            )
            assert "—" not in body and "—" not in subject
    _, body = render("booking_moved_team_member", "en", values)
    assert "MEMBER-MARKER" in body


# The moved-to-another-member email: "now with" names the booking's snapshot worker; a NULL
# snapshot falls back to the plain moved template. Kills: the file switch, a missing fallback.
@pytest.fixture
def clean_outbox(people: People) -> Iterator[None]:
    yield
    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM email_outbox"))


@pytest.mark.usefixtures("clean_outbox")
@pytest.mark.parametrize("name", ["Ana Snapshot", None])
def test_send_booking_member_changed_body_names_the_member_or_falls_back(
    people: People,
    owner: TestClient,
    team: str,
    monkeypatch: pytest.MonkeyPatch,
    name: str | None,
) -> None:
    from app import mail
    from tests.test_booking_email import run_send_booking

    booking_id = book(owner, team, member_id=ana_id(people)).json()["id"]
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE bookings SET worker_display_name = :n WHERE id = :id"),
            {"n": name, "id": booking_id},
        )
    starts_at = row_of(people.a, booking_id)["starts_at"].isoformat()
    bodies: list[str] = []
    monkeypatch.setattr(mail, "deliver", lambda *args, **kwargs: bodies.append(args[3]))
    run_send_booking(
        people.a,
        booking_id,
        "booking_moved_team",
        user_id=str(people.both),
        starts_at=starts_at,
        previous_starts_at=starts_at,
        member_changed="1",
    )
    (body,) = bodies
    # Locale-free: the member variant ends "(zone), now with <name>."; the plain one at "(zone)."
    if name is None:
        assert "(Europe/Amsterdam).\n" in body
    else:
        assert f"{name}.\n" in body and "(Europe/Amsterdam).\n" not in body
