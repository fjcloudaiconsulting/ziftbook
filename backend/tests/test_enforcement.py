"""ZIF-78: a business over its plan's places has 30 days; then only the places it keeps sign in, and
the trial's and the grace's emails go to its owners once each."""

import asyncio
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import enforcement, mail
from app.db import tenant_context
from app.jobs import run_once
from app.worker import KINDS
from tests.conftest import (
    People,
    add_membership,
    add_password,
    add_user,
    delete_billing,
    email_of,
    events,
    give_plan,
    jobs_as_the_worker,
    member_id,
    migrate_tenant_context,
    new_client,
    save_setting,
    sent_to,
    set_role,
    signed_in,
)
from tests.test_booking_page_api import page, slug_of
from tests.test_entitlements import (  # noqa: F401
    Plan,
    enqueue,
    over_limit_since,
    plan,
    run_enforce,
    subscribe,
    trial_plan,
    write,
)
from tests.test_merchant_booking_api import (  # noqa: F401
    ana_id,
    availability,
    book,
    owner,
    ready,
    row_of,
    team,
)
from tests.test_names import routes_behind_signed_in
from tests.test_services import service

PASSWORD = "lavender-harbour-19"


@pytest.fixture
def app(people: People, migrate_engine: Engine) -> FastAPI:
    from app.main import create_app

    add_password(migrate_engine, people.only_a, PASSWORD)
    add_password(migrate_engine, people.both, PASSWORD)
    return create_app()


@pytest.fixture(autouse=True)
def clean_outbox(people: People, migrate_engine: Engine) -> Iterator[None]:
    yield
    # As the migrate role: the app role has no DELETE on email_outbox (ZB-10).
    for tenant_id in (people.a, people.b):
        with migrate_tenant_context(migrate_engine, tenant_id) as conn:
            conn.execute(text("DELETE FROM email_outbox"))


@pytest.fixture
def junior(people: People, migrate_engine: Engine) -> Iterator[uuid.UUID]:
    """A third member of a, a worker who joined after the other two."""
    user_id = add_user(migrate_engine)
    add_membership(people.a, user_id)
    yield user_id
    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(text("DELETE FROM memberships WHERE user_id = :u"), {"u": user_id})
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


def run_jobs() -> None:
    async def until_idle() -> None:
        while await run_once(KINDS):
            pass

    with jobs_as_the_worker():
        asyncio.run(until_idle())


def sweep() -> None:
    with jobs_as_the_worker():
        enforcement.sweep()


def enforce(tenant_id: uuid.UUID) -> None:
    enqueue(tenant_id)
    run_enforce()


def backdate(migrate_engine: Engine, tenant_id: uuid.UUID, days: int) -> None:
    """Move the business's over-the-limit stamp that many days into the past."""
    write(
        migrate_engine,
        tenant_id,
        "UPDATE tenant_billing SET over_limit_since = now() - make_interval(days => :d)",
        d=days,
    )


def paused(tenant_id: uuid.UUID) -> set[uuid.UUID]:
    with tenant_context(tenant_id) as db:
        return set(
            db.scalars(text("SELECT user_id FROM memberships WHERE access_paused_at IS NOT NULL"))
        )


def past_grace(migrate_engine: Engine, tenant_id: uuid.UUID, places: int = 1) -> None:
    """Over the limit, and the 30 days over."""
    give_plan(tenant_id, {"places": places})
    enforce(tenant_id)
    backdate(migrate_engine, tenant_id, 31)
    enforce(tenant_id)


def sign_in(app: FastAPI, user_id: uuid.UUID) -> Response:
    return new_client(app).post(
        "/api/session", json={"email": email_of(user_id), "password": PASSWORD}
    )


def subjects(user_id: uuid.UUID) -> list[str]:
    return [m["Subject"] for m in sent_to(email_of(user_id))]


def subject(template: str, locale: str = "nl") -> str:
    return mail.render(template, locale)[0]


def keeps(client: TestClient, member: uuid.UUID, body: Any) -> Response:
    return client.put(f"/api/members/{member}/keeps-access", json=body)


# ZIF-78 fence. Kills: an email on every run instead of on the stamp; a dedupe key without the
# stamp (a second downgrade would never mail); the email to a worker; the 7-days-left email missing
# or repeated.
def test_downgrade_stamps_and_mails_once(people: People, migrate_engine: Engine) -> None:
    give_plan(people.a, {"places": 1})  # a: an owner (both, locale nl) and a worker
    enforce(people.a)
    enforce(people.a)
    run_jobs()
    assert subjects(people.both) == [subject("over_limit_started")]

    give_plan(people.a, {"places": 2})
    enforce(people.a)
    give_plan(people.a, {"places": 1})
    enforce(people.a)
    run_jobs()
    assert subjects(people.both) == [subject("over_limit_started")] * 2

    backdate(migrate_engine, people.a, 24)
    sweep()
    sweep()
    run_jobs()
    assert subjects(people.both)[0] == subject("over_limit_ending")
    assert len(subjects(people.both)) == 3
    assert subjects(people.only_a) == []


# ZIF-78 fence. Kills: pausing on the stamp instead of 30 days after it.
def test_within_grace_everyone_signs_in(people: People, app: FastAPI) -> None:
    give_plan(people.a, {"places": 1})
    enforce(people.a)

    assert over_limit_since(people.a) is not None
    assert paused(people.a) == set()
    assert sign_in(app, people.only_a).status_code == 200
    assert sign_in(app, people.both).status_code == 200


# ZIF-78 fence. Kills: nobody paused after the grace; the owner paused instead of the worker; a
# paused member let in, or refused with a code the console cannot explain.
def test_after_grace_only_kept_members_sign_in(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    past_grace(migrate_engine, people.a)

    assert paused(people.a) == {people.only_a}
    refused = sign_in(app, people.only_a)
    assert (refused.status_code, refused.json()) == (403, {"code": "place_paused"})
    assert sign_in(app, people.both).status_code == 200


# ZIF-78 fence. Kills: seniority ranked above the owner's pick; a pick that the next run forgets.
def test_keeps_access_choice_wins_over_seniority(
    people: People, app: FastAPI, migrate_engine: Engine, junior: uuid.UUID
) -> None:
    owner_client = signed_in(app, people.a, people.both)
    assert keeps(owner_client, member_id(people.a, junior), {"keeps": True}).status_code == 204

    past_grace(migrate_engine, people.a, places=2)
    enforce(people.a)

    assert paused(people.a) == {people.only_a}


# ZIF-78 fence. Kills: Free pausing every member, or keeping a worker the owner picked in place of
# the only owner (nobody left to upgrade); a junior owner kept over a senior one.
def test_free_keeps_one_owner(
    people: People, app: FastAPI, migrate_engine: Engine, free_default: None, junior: uuid.UUID
) -> None:
    owner_client = signed_in(app, people.a, people.both)
    assert keeps(owner_client, member_id(people.a, junior), {"keeps": True}).status_code == 204
    enforce(people.a)
    backdate(migrate_engine, people.a, 31)
    enforce(people.a)
    assert paused(people.a) == {people.only_a, junior}
    assert sign_in(app, people.both).status_code == 200

    # Two owners: the senior one (only_a joined first) is kept.
    with tenant_context(people.a) as db:
        db.execute(text("UPDATE memberships SET keeps_access = false"))
    give_plan(people.a, {"places": 3})
    enforce(people.a)
    set_role(people.a, people.only_a, "owner")
    delete_billing_of(migrate_engine, people.a)
    enforce(people.a)
    backdate(migrate_engine, people.a, 31)
    enforce(people.a)
    assert paused(people.a) == {people.both, junior}


def delete_billing_of(migrate_engine: Engine, tenant_id: uuid.UUID) -> None:
    with migrate_engine.begin() as conn:
        delete_billing(conn, [tenant_id])


# ZIF-78 fence (Review Focus 4). Kills: a paused member's live session still resolving; the
# owner's session ended with it.
def test_paused_session_stops_resolving_owner_does_not(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    owner_client = signed_in(app, people.a, people.both)
    assert worker.get("/api/session").status_code == 200

    past_grace(migrate_engine, people.a)

    assert worker.get("/api/session").status_code == 401
    assert worker.get("/api/entitlements").status_code == 401
    assert owner_client.get("/api/session").status_code == 200
    assert owner_client.get("/api/members").status_code == 200


# ZIF-78 fence. Kills: an upgrade that waits for the grace or the sweep to let members back.
def test_upgrade_unpauses_at_once(people: People, app: FastAPI, migrate_engine: Engine) -> None:
    past_grace(migrate_engine, people.a)
    assert paused(people.a) == {people.only_a}

    give_plan(people.a, {"places": 2})
    enforce(people.a)

    assert paused(people.a) == set()
    assert over_limit_since(people.a) is None
    assert sign_in(app, people.only_a).status_code == 200


# ZIF-78 fence. Kills: availability offering a member who can no longer sign in.
def test_paused_member_is_not_offered_in_availability(
    people: People,
    owner: TestClient,  # noqa: F811
    team: str,  # noqa: F811
    migrate_engine: Engine,
) -> None:
    before = availability(owner, team).json()["workers"]
    past_grace(migrate_engine, people.a)
    after = availability(owner, team).json()["workers"]

    assert str(ana_id(people)) in {w["id"] for w in before}
    assert str(ana_id(people)) not in {w["id"] for w in after}
    assert after


# ZIF-78 fence. Kills: the merchant booking a paused member, or refusing with slot_unavailable
# (which says the time is taken).
def test_merchant_cannot_book_a_paused_member(
    people: People,
    owner: TestClient,  # noqa: F811
    team: str,  # noqa: F811
    migrate_engine: Engine,
) -> None:
    past_grace(migrate_engine, people.a)

    refused = book(owner, team, member_id=ana_id(people))
    assert (refused.status_code, refused.json()) == (409, {"code": "member_paused"})
    assert book(owner, team, member_id=member_id(people.a, people.both)).status_code == 201


# ZIF-78 fence. Kills: pausing a member cancelling or moving their bookings.
def test_existing_bookings_of_paused_member_stay(
    people: People,
    owner: TestClient,  # noqa: F811
    team: str,  # noqa: F811
    migrate_engine: Engine,
) -> None:
    booked = book(owner, team, member_id=ana_id(people))
    assert booked.status_code == 201

    past_grace(migrate_engine, people.a)

    row = row_of(people.a, booked.json()["id"])
    assert (row["status"], row["worker_id"]) == ("confirmed", ana_id(people))


# ZIF-78 fence. Kills: trial emails to a business with a grant or a subscription (at the sweep, or
# at send time); a repeat each hour; a new trial end that never mails.
def test_trial_emails_are_sent_once_and_not_to_subscribers(
    people: People,
    migrate_engine: Engine,
    trial_plan: uuid.UUID,  # noqa: F811
) -> None:
    def trial_ends_in(hours: int) -> None:
        write(
            migrate_engine,
            people.a,
            "UPDATE tenant_billing SET trial_ends_at = now() + make_interval(hours => :h)",
            h=hours,
        )

    trial_ends_in(48)
    give_plan(people.a, {"places": 5})
    sweep()
    run_jobs()
    assert subjects(people.both) == []

    # A subscription Stripe does not count yet (incomplete) still means the business is buying.
    delete_billing_of(migrate_engine, people.a)
    subscribe(migrate_engine, people.a, trial_plan, status="incomplete")
    sweep()
    run_jobs()
    assert subjects(people.both) == []

    delete_billing_of(migrate_engine, people.a)
    sweep()  # queued while on the trial, then subscribed before it is sent
    subscribe(migrate_engine, people.a, trial_plan, status="incomplete")
    run_jobs()
    assert subjects(people.both) == []

    delete_billing_of(migrate_engine, people.a)
    trial_ends_in(-1)
    sweep()
    sweep()
    run_jobs()
    assert subjects(people.both) == [subject("trial_ended")]

    trial_ends_in(24)
    sweep()
    sweep()
    run_jobs()
    assert subjects(people.both) == [subject("trial_ending"), subject("trial_ended")]
    assert subjects(people.only_a) == []


# ZIF-78 fence. Kills: a worker choosing who keeps access; an unaudited change; a change that waits
# for the sweep; a member of another business reachable by id.
def test_keeps_access_is_owner_only(
    people: People, app: FastAPI, migrate_engine: Engine, app_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    owner_client = signed_in(app, people.a, people.both)
    ana = member_id(people.a, people.only_a)

    assert keeps(worker, ana, {"keeps": True}).json() == {"code": "owner_only"}
    assert keeps(owner_client, ana, {"keeps": "yes"}).status_code == 422
    assert keeps(owner_client, member_id(people.b, people.only_b), {"keeps": True}).status_code == (
        404
    )
    with app_engine.connect() as conn:
        before = conn.scalar(text("SELECT count(*) FROM jobs WHERE kind = 'billing.enforce'"))

    assert keeps(owner_client, ana, {"keeps": True}).status_code == 204
    assert keeps(owner_client, ana, {"keeps": True}).status_code == 204  # unchanged: no event

    with tenant_context(people.a) as db:
        assert db.scalar(text("SELECT keeps_access FROM memberships WHERE id = :m"), {"m": ana})
    recorded = events(migrate_engine, tenant_id=people.a, action="member_keeps_access_changed")
    assert [(e["actor_user_id"], e["target"], e["details"]) for e in recorded] == [
        (people.both, f"user:{people.only_a}", {"keeps": True})
    ]
    with app_engine.connect() as conn:
        after = conn.scalar(text("SELECT count(*) FROM jobs WHERE kind = 'billing.enforce'"))
    assert after == before + 1
    listed = {m["member_id"]: m for m in owner_client.get("/api/members").json()}
    assert (listed[str(ana)]["keeps_access"], listed[str(ana)]["paused"]) == (True, False)


# ZIF-78 fence. Kills: a person paused in their oldest business locked out of every other one.
def test_paused_in_one_business_signs_in_to_another(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    set_role(people.a, people.only_a, "owner")
    set_role(people.a, people.both, "worker")  # both's oldest membership, now a worker's
    past_grace(migrate_engine, people.a)
    assert paused(people.a) == {people.both}

    signed = sign_in(app, people.both)
    assert (signed.status_code, signed.json()["tenant_id"]) == (200, str(people.b))


def pause_directly(migrate_engine: Engine, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """A paused membership whose session survived (a pause that has not ended it yet)."""
    write(
        migrate_engine,
        tenant_id,
        "UPDATE memberships SET access_paused_at = now() WHERE user_id = :u",
        u=user_id,
    )


UNAUTHENTICATED = (401, {"code": "unauthenticated"})


def answer(response: Response) -> tuple[int, Any]:
    is_json = response.headers.get("content-type", "").startswith("application/json")
    return response.status_code, response.json() if is_json else None


# ZIF-78 fence. Deny by default: every route that needs a session refuses a paused member's
# surviving session, those that resolve it themselves (photos, payments, services' Save) included.
def test_every_session_route_refuses_a_paused_member(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client = signed_in(app, people.a, people.both)  # an owner: owner-only routes get past role
    pause_directly(migrate_engine, people.a, people.both)
    routes = routes_behind_signed_in(app)
    assert len(routes) >= 30  # the walk still sees the routes (tests/test_names.py's floor)
    # The routes that resolve their session themselves are in the walk too.
    assert {
        ("POST", "/api/photo-uploads/{upload_id}/check"),
        ("POST", "/api/services"),
        ("POST", "/api/payments/dashboard"),
    } <= set(routes)

    reached = set()
    for method, path in routes:
        url = re.sub(r"\{[^}]+\}", "x", path)
        response = client.request(method, url, json=None if method == "GET" else {})
        if answer(response) != UNAUTHENTICATED:
            reached.add((method, path, response.status_code))
    assert reached == set()


# ZIF-78 fence. Kills: a route that resolves its own session (auth.resolve) skipping the pause.
def test_owner_routes_that_resolve_their_own_session_refuse_a_paused_owner(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    client = signed_in(app, people.a, people.both)
    pause_directly(migrate_engine, people.a, people.both)

    assert answer(service(client)) == UNAUTHENTICATED
    assert answer(client.get("/api/payments/account")) == UNAUTHENTICATED
    assert answer(client.post("/api/payments/account", json={})) == UNAUTHENTICATED
    assert answer(client.post("/api/payments/dashboard", json={})) == UNAUTHENTICATED
    check = client.post(f"/api/photo-uploads/{uuid.uuid4()}/check", json={})
    assert answer(check) == UNAUTHENTICATED
    with tenant_context(people.a) as db:
        assert db.scalar(text("SELECT count(*) FROM services")) == 0


# ZIF-78 fence. Kills: a pause that leaves the member's sessions, or their bound account session,
# alive; one that ends the kept owner's.
def test_a_pause_ends_the_members_sessions(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    assert sign_in(app, people.only_a).status_code == 200
    owner_client = signed_in(app, people.a, people.both)

    past_grace(migrate_engine, people.a)

    with migrate_engine.connect() as conn:
        counts = conn.execute(
            text("""
            SELECT (SELECT count(*) FROM sessions WHERE user_id = :u),
                   (SELECT count(*) FROM account_sessions WHERE user_id = :u)
            """),
            {"u": people.only_a},
        ).one()
    assert tuple(counts) == (0, 0)
    assert owner_client.get("/api/session").status_code == 200


# ZIF-78 fence. Kills: the booking page listing a member who can no longer sign in.
def test_paused_member_is_not_on_the_booking_page(
    people: People,
    app: FastAPI,
    team: str,  # noqa: F811
    migrate_engine: Engine,
    app_engine: Engine,
) -> None:
    save_setting(people.a, "published", True)
    slug = slug_of(app_engine, people.a)

    def workers() -> set[str]:
        (listed,) = page(new_client(app), slug).json()["services"]
        return {w["id"] for w in listed["workers"]}

    assert str(ana_id(people)) in workers()
    past_grace(migrate_engine, people.a)
    assert workers() == {str(member_id(people.a, people.both))}


def enforce_jobs(app_engine: Engine, tenant_id: uuid.UUID) -> int:
    with app_engine.connect() as conn:
        count: int = conn.scalar(
            text("SELECT count(*) FROM jobs WHERE kind = 'billing.enforce' AND tenant_id = :t"),
            {"t": tenant_id},
        )
    return count


# ZIF-78 fence. Kills: a role change or a removal that waits for the sweep to re-rank; a removed
# member's pick kept for when they are invited back.
def test_removal_and_role_change_check_the_places_again(
    people: People, app: FastAPI, app_engine: Engine, junior: uuid.UUID
) -> None:
    owner_client = signed_in(app, people.a, people.both)
    junior_member = member_id(people.a, junior)
    assert keeps(owner_client, junior_member, {"keeps": True}).status_code == 204
    before = enforce_jobs(app_engine, people.a)

    ana = member_id(people.a, people.only_a)
    assert owner_client.patch(f"/api/members/{ana}", json={"role": "owner"}).status_code == 200
    assert enforce_jobs(app_engine, people.a) == before + 1
    removed = owner_client.request("DELETE", f"/api/members/{junior_member}", json={})
    assert removed.status_code == 204
    assert enforce_jobs(app_engine, people.a) == before + 2
    with tenant_context(people.a) as db:
        kept = db.scalar(
            text("SELECT keeps_access FROM memberships WHERE id = :m"), {"m": junior_member}
        )
    assert kept is False


# ZIF-78 fence. Kills: an hourly enforce job for every business, within its places or not.
def test_the_sweep_enqueues_nothing_for_a_business_within_its_places(
    people: People, app_engine: Engine
) -> None:
    give_plan(people.a, {"places": 5})
    give_plan(people.b, {"places": 1})  # b: two workers
    a_before, b_before = enforce_jobs(app_engine, people.a), enforce_jobs(app_engine, people.b)

    sweep()

    assert enforce_jobs(app_engine, people.a) == a_before
    assert enforce_jobs(app_engine, people.b) == b_before + 1
