"""The never-log fence: real flows, at DEBUG, checked for personal data reaching a log line.

CONTRIBUTING "Logging": ids only, never a password, token or its hash, cookie, email address,
name, business or service name, IP address, user agent, request body, query string or header.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from sqlalchemy import Engine, text

from app import auth, bookings, clients, payments
from app.db import tenant_context
from app.jobs import run_once
from app.mail import render
from app.main import create_app
from app.worker import KINDS
from tests.conftest import (
    People,
    add_password,
    email_of,
    fresh_email,
    jobs_as_the_worker,
    mailed,
    member_id,
    migrate_tenant_context,
    new_client,
    put_settings,
    save_setting,
    signed_in,
    token_in,
)
from tests.stripe_fake import FakeGateway
from tests.test_booking_payments_api import make_shop, resume, run, sync
from tests.test_bookings_api import post_booking, sign_in_as
from tests.test_working_hours import seed

UA = {"User-Agent": "zif-never-log-agent/7"}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture(autouse=True)
def clean_outbox(people: People, migrate_engine: Engine) -> Iterator[None]:
    yield
    # As the migrate role: the app role has no DELETE on email_outbox (ZB-10).
    for tenant_id in (people.a, people.b):
        with migrate_tenant_context(migrate_engine, tenant_id) as conn:
            conn.execute(text("DELETE FROM email_outbox"))


def run_jobs_once() -> None:
    """One batch of due jobs, run as the worker does, as ziftbook_worker: only it mints a link or
    sets an invite token (ZB-08)."""
    with jobs_as_the_worker():
        asyncio.run(run_once(KINDS))


def client_for(app: FastAPI) -> Any:
    client = new_client(app)
    client.headers.update(UA)
    return client


def test_no_personal_data_ever_reaches_a_log(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
    mailgun_replies: dict[str, int | Exception],
    spans: Callable[[], list[ReadableSpan]],
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never: list[str] = ["2001:db8:", "@example.com", UA["User-Agent"]]
    # The audit table persists across runs: only this run's rows are scanned for names.
    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        started = conn.scalar(text("SELECT clock_timestamp()"))
    templates = ("sign_up", "sign_up_registered", "password_reset", "invite")
    never += [render(template, "en")[0] for template in templates]

    def secret(value: str) -> None:
        never.append(value)

    def token_secret(token: str) -> None:
        secret(token)
        secret(hashlib.sha256(token.encode()).hexdigest())

    def cookie_secret(response: Any) -> None:
        value = response.cookies.get(auth.COOKIE)
        if value:
            secret(value)
            secret(hashlib.sha256(value.encode()).hexdigest())

    # 1: sign up, complete with a distinctive password and business name.
    sign_up_email = fresh_email()
    sign_up_password = "Studio-Zzyzx-Pw-1"
    business_name = "Studio Zzyzx-9"
    signer_name = "Zzyzx Signer-9"
    secret(sign_up_email)
    secret(sign_up_password)
    secret(business_name)
    secret(signer_name)

    anon = client_for(app)
    signed_up = anon.post("/api/sign-up", json={"email": sign_up_email, "locale": "en"})
    assert signed_up.status_code == 202
    signup_token = token_in(mailed(sign_up_email))
    token_secret(signup_token)
    response = anon.post(
        "/api/sign-up/complete",
        content=json.dumps(
            {
                "token": signup_token,
                "password": sign_up_password,
                "business_name": business_name,
                "country": "NL",
                "name": signer_name,
            }
        ),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 201
    cookie_secret(response)

    # 2: sign in, wrong password then the right one.
    sign_in_password = "Right-Pw2-88"
    wrong_password = "wrong-Qx81"
    secret(sign_in_password)
    secret(wrong_password)
    secret(email_of(people.only_a))
    add_password(migrate_engine, people.only_a, sign_in_password)

    wrong = client_for(app).post(
        "/api/session", json={"email": email_of(people.only_a), "password": wrong_password}
    )
    assert wrong.status_code == 401
    right = client_for(app).post(
        "/api/session", json={"email": email_of(people.only_a), "password": sign_in_password}
    )
    assert right.status_code == 200
    cookie_secret(right)

    # 2b: a sign-up for an address that already has an account gets the "registered" mail.
    assert (
        client_for(app)
        .post("/api/sign-up", json={"email": email_of(people.only_a), "locale": "en"})
        .status_code
        == 202
    )
    mailed(email_of(people.only_a))

    # 3: password reset for a second account.
    reset_old_password = "Old-Pw3-77"
    reset_new_password = "New-Pw3-9977"
    secret(reset_old_password)
    secret(reset_new_password)
    secret(email_of(people.only_b))
    add_password(migrate_engine, people.only_b, reset_old_password)

    reset_client = client_for(app)
    assert (
        reset_client.post(
            "/api/password-reset", json={"email": email_of(people.only_b), "locale": "en"}
        ).status_code
        == 202
    )
    reset_token = token_in(mailed(email_of(people.only_b)))
    token_secret(reset_token)
    reset_response = reset_client.post(
        "/api/password-reset/complete",
        content=json.dumps({"token": reset_token, "password": reset_new_password}),
        headers={"content-type": "application/json"},
    )
    assert reset_response.status_code == 204
    cookie_secret(reset_response)

    # 4: PUT /api/settings with a distinctive value.
    timezone = "America/Argentina/ComodRivadavia"
    secret(timezone)
    owner = signed_in(app, people.a, people.both)
    owner.headers.update(UA)
    # published too: step 6c below hits the public availability route (ZIF-145 gate).
    assert put_settings(owner, {"timezone": timezone, "published": True}).status_code == 200

    # 5: PATCH /api/members/{id} role change, as the owner.
    target = member_id(people.a, people.only_a)
    assert owner.patch(f"/api/members/{target}", json={"role": "owner"}).status_code == 200

    # 5b: PUT /api/members/{id}/display-name with a distinctive name, and working hours so the
    # public availability call below (6c) actually renders it.
    member_name = "Vivi Qzx-3"
    secret(member_name)
    assert (
        owner.put(
            f"/api/members/{target}/display-name", json={"display_name": member_name}
        ).status_code
        == 200
    )
    seed(people.a, people.only_a, [(d, "09:00", "17:00") for d in range(1, 8)])

    # 6: POST /api/services with a distinctive name.
    service_name = "Service Qwyx-4"
    secret(service_name)
    created = owner.post(
        "/api/services",
        json={
            "name": {"en": service_name},
            "price": {"amount_minor": 1000},
            "duration_minutes": 15,
        },
    )
    assert created.status_code == 201

    # 6b: PUT /api/services/{id}/workers, assigning a member.
    workers = owner.put(f"/api/services/{created.json()['id']}/workers", json=[str(target)])
    assert workers.status_code == 200

    # 6c: public availability for that service, no session.
    availability = client_for(app).get(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/availability",
        params={"from": "2026-09-17", "to": "2026-09-18"},
    )
    assert availability.status_code == 200

    # 6d: a client, its notes, a recorded consent, and a search for it (ZIF-49).
    client_name = "Client Zqx-8"
    client_email = fresh_email()
    client_phone = "+31 6 55 44 33 22"
    client_note = "Note Wvx-5"
    internal_note = "Private Vbn-6"
    # Registered on its own: the search below sends this, not the whole name, and a log line that
    # echoed the raw query string would not contain "Client Zqx-8" for secret(client_name) to find
    # (a space is %20 on the wire anyway). CONTRIBUTING "Logging" forbids the query string.
    client_search = "Zqx-8"
    secret(client_search)
    secret(client_name)
    secret(client_email)
    secret(client_phone)
    secret("+31655443322")  # as stored (ZIF-152)
    secret(client_note)
    secret(internal_note)
    added_client = owner.post(
        "/api/clients", json={"name": client_name, "email": client_email, "phone": client_phone}
    )
    assert added_client.status_code == 201
    client_id = added_client.json()["id"]
    assert (
        owner.patch(
            f"/api/clients/{client_id}",
            json={"client_note": client_note, "internal_note": internal_note},
        ).status_code
        == 200
    )
    assert (
        owner.post(
            f"/api/clients/{client_id}/consents",
            json={"policy_version": "2026-09-01", "purposes": {"sms": True}},
        ).status_code
        == 201
    )
    assert owner.get("/api/clients", params={"q": client_search}).status_code == 200

    # 6e (ZIF-51): a guest booking POST at DEBUG, and the availability GET that finds its slot.
    # F-i: the 6c call above is fixed-dated and now in the past, so its slots list is empty - this
    # flow needs its OWN future-dated GET.
    # The business's date (the default zone), never the host's (ZIF-54 F1).
    today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    booking_day = (today + timedelta(days=2)).isoformat()
    own_availability = client_for(app).get(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/availability",
        params={"from": booking_day, "to": booking_day},
    )
    assert own_availability.status_code == 200
    slots = own_availability.json()["slots"]
    assert slots, "no slots offered for the logging flow to book"
    booker_name = "Booker Zvq-9"
    booker_email = fresh_email()
    booker_phone = "+31 6 11 22 33 44"
    secret(booker_name)
    secret(booker_email)
    secret(booker_phone)
    secret("+31611223344")  # as stored (ZIF-152)
    # ZIF-157: the client's message for the merchant, through the merchant email and a 422.
    booker_message = "Message Qx-7 for the salon"
    bad_message = "Message Qx-8\twith a tab"
    secret(booker_message)
    secret("Message Qx-8")
    # ZIF-117: the public POST books only for the signed-in address itself.
    booker = client_for(app)
    sign_in_as(booker, people.a, booker_email)

    # 6f (ZIF-53): a pending booking sends booking_received (client) and booking_request (merchant);
    # the owner's confirm then sends booking_confirmed with its .ics. The reminder's due_at is a day
    # away, so run_once leaves it queued.
    refused = booker.post(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/bookings",
        json={
            "starts_at": slots[0],
            "name": booker_name,
            "email": booker_email,
            "phone": booker_phone,
            "message": bad_message,
            "policy_version": clients.policy_version_for("en"),
            "consents": {},
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "message_invalid"
    booked = booker.post(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/bookings",
        json={
            "starts_at": slots[0],
            "name": booker_name,
            "email": booker_email,
            "phone": booker_phone,
            "message": booker_message,
            "policy_version": clients.policy_version_for("en"),
            "consents": {},
        },
    )
    assert booked.status_code == 201
    run_jobs_once()
    confirmed = owner.patch(f"/api/bookings/{booked.json()['id']}", json={"status": "confirmed"})
    assert confirmed.status_code == 200
    run_jobs_once()

    # 6g (ZIF-122): a second booking's hold runs out and the sweep settles it, logging its count.
    assert len(slots) >= 2  # slots[-1] must not be 6f's slot, which is taken
    lapsed = booker.post(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/bookings",
        json={
            "starts_at": slots[-1],
            "name": booker_name,
            "email": booker_email,
            "phone": booker_phone,
            "policy_version": clients.policy_version_for("en"),
            "consents": {},
        },
    )
    assert lapsed.status_code == 201
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE bookings SET expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": lapsed.json()["id"]},
        )
    assert bookings.sweep() >= 1

    # 6h (ZIF-121): a decline with a message; the text goes to the client's email and nowhere else.
    assert len(slots) >= 4  # clear of 6f's slot and its buffer; 6g's expired hold is free again
    declined = booker.post(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/bookings",
        json={
            "starts_at": slots[-2],
            "name": booker_name,
            "email": booker_email,
            "phone": booker_phone,
            "policy_version": clients.policy_version_for("en"),
            "consents": {},
        },
    )
    assert declined.status_code == 201
    decline_message = "Zzyzx-9 fully booked that day"
    secret(decline_message)
    assert (
        owner.patch(
            f"/api/bookings/{declined.json()['id']}",
            json={"status": "declined", "message": decline_message},
        ).status_code
        == 200
    )
    run_jobs_once()

    # 6i (ZIF-130): a block over 6f's confirmed booking is refused with its client's name in the
    # body, and only there.
    in_the_way = datetime.fromisoformat(slots[0])
    refused = owner.post(
        f"/api/members/{target}/time-off",
        json={
            "starts_at": in_the_way.isoformat(),
            "ends_at": (in_the_way + timedelta(minutes=30)).isoformat(),
        },
    )
    assert refused.status_code == 409
    assert refused.json()["bookings"][0]["client_name"] == booker_name

    # 6j (ZB-05): a guest over the business's unproven cap is refused and the owners' alert goes.
    monkeypatch.setattr(bookings, "UNPROVEN_PER_BUSINESS_HOUR", 0)
    paused = client_for(app).post(
        f"/api/public/businesses/{people.a}/services/{created.json()['id']}/bookings",
        json={
            "starts_at": slots[1],
            "name": booker_name,
            "email": booker_email,
            "phone": booker_phone,
            "policy_version": clients.policy_version_for("en"),
            "consents": {},
        },
    )
    assert paused.status_code == 429
    run_jobs_once()

    # 7: invite flow — send, list, a wrong token, then accept with a brand-new account.
    invite_email = fresh_email()
    invite_password = "Invite-Pw7-55"
    invitee_name = "Zzyzx Invitee-7"
    secret(invite_email)
    secret(invite_password)
    secret(invitee_name)

    assert owner.post("/api/invites", json={"email": invite_email}).status_code == 201
    assert owner.get("/api/invites").status_code == 200

    invite_token = token_in(mailed(invite_email))
    token_secret(invite_token)

    # Same shape, wrong digest: exercises the "no matching invite" path, not just the regex one.
    tenant_part, _, secret_part = invite_token.partition(".")
    flipped = "A" if secret_part[-1] != "A" else "B"
    wrong_token = f"{tenant_part}.{secret_part[:-1]}{flipped}"
    wrong_accept = client_for(app).post(
        "/api/invites/accept",
        json={"token": wrong_token, "password": invite_password, "name": invitee_name},
    )
    assert wrong_accept.status_code == 400

    accepted = client_for(app).post(
        "/api/invites/accept",
        json={"token": invite_token, "password": invite_password, "name": invitee_name},
    )
    assert accepted.status_code == 201
    cookie_secret(accepted)

    # 7b: a person from before names were asked for gives theirs.
    legacy_name = "Zzyzx Legacy-3"
    secret(legacy_name)
    with migrate_engine.begin() as conn:
        conn.execute(text("UPDATE users SET name = NULL WHERE id = :u"), {"u": people.only_b})
    assert (
        signed_in(app, people.b, people.only_b)
        .put("/api/session/name", json={"name": legacy_name})
        .status_code
        == 200
    )
    # Names are never event details or targets either (the audit trail is not a log, but the same
    # rule holds: it would be read by whoever reads events).
    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        recorded = json.dumps(
            [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT action, target, details FROM audit_events "
                        "WHERE created_at >= :started"
                    ),
                    {"started": started},
                ).mappings()
            ],
            default=str,
        )
    assert '"user_name_set"' in recorded  # the window holds this test's events
    for value in (signer_name, invitee_name, legacy_name):
        assert value not in recorded, value

    # 8: forced 500, a real duplicate-email error, the email known only at runtime.
    def duplicate_email() -> None:
        with migrate_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (email) VALUES (:email), (:email)"),
                {"email": sign_up_email},
            )

    app.add_api_route("/api/test/duplicate-email", duplicate_email, methods=["POST"], tags=["test"])
    forced = client_for(app).post("/api/test/duplicate-email", json={})
    assert forced.status_code == 500

    # 9: Mailgun refuses a fresh sign-up link, its reply quoting the address; run directly.
    fail_email = fresh_email()
    secret(fail_email)
    assert (
        client_for(app).post("/api/sign-up", json={"email": fail_email, "locale": "en"}).status_code
        == 202
    )

    mailgun_replies[fail_email] = 400
    try:
        run_jobs_once()
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(
                text("""
                DELETE FROM jobs WHERE kind = 'email.token'
                  AND payload->>'token_id' IN (SELECT id::text FROM email_tokens WHERE email = :e)
                """),
                {"e": fail_email},
            )

    dumped = json.dumps(log_lines())
    for value in never:
        assert value not in dumped, value

    # INFRA-106: what leaves over OTLP too, every span and metric point of the same flows.
    exported = json.dumps([s.to_json() for s in spans()])
    data = metric_reader.get_metrics_data()
    assert data is not None
    exported += data.to_json()
    for value in never:
        assert value not in exported, value

    lines = log_lines()
    assert any(line["msg"] == "access" for line in lines)
    unhandled = [line for line in lines if line["msg"] == "unhandled error"]
    assert len(unhandled) == 1
    failed = [line for line in lines if line["msg"] == "job failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "HTTPError"
    sent = {line["template"] for line in lines if line["msg"] == "email sent"}
    assert {"sign_up", "sign_up_registered", "password_reset", "invite"} <= sent
    assert {"booking_received", "booking_request", "booking_confirmed", "booking_declined"} <= sent
    assert "booking_flood" in sent
    flood = [line for line in lines if line["msg"] == "booking flood"]
    assert [(line["level"], line["tenant_id"]) for line in flood] == [("WARNING", str(people.a))]
    email_failed = [line for line in lines if line["msg"] == "email failed"]
    assert [(line["template"], line["error"]) for line in email_failed] == [
        ("sign_up", "HTTPError")
    ]


# ZIF-158 B2 14. fence. Kills: logging the PaymentStart (a client_secret finishes a payment for
# whoever reads it).
def test_a_client_secret_never_reaches_a_log_a_span_or_a_job(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    fake_stripe: FakeGateway,
    log_lines: Callable[[], list[dict[str, Any]]],
    spans: Callable[[], list[ReadableSpan]],
) -> None:
    save_setting(people.a, "published", True)
    owner = signed_in(app, people.a, people.both)
    service_id = make_shop(people, owner, fake_stripe)
    client = new_client(app)
    made = post_booking(client, people.a, service_id, payment="check")
    booking_id = made.json()["id"]
    resume(client, people, booking_id)
    fake_stripe.confirm(fake_stripe.latest(booking_id))
    sync(client, people, booking_id)
    run("payment.settle", "stripe.event")
    payments.reconcile(people.a, fake_stripe.latest(booking_id))
    secrets = [got["client_secret"] for got in fake_stripe.intents.values()]
    assert secrets and made.json()["payment"]["client_secret"] in secrets

    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        stored = " ".join(
            str(value)
            for value in [
                *conn.scalars(text("SELECT payload::text FROM jobs")),
                *conn.scalars(text("SELECT details::text FROM booking_events")),
            ]
        )
    everything = json.dumps(log_lines()) + json.dumps(
        [dict(span.attributes or {}) | {"name": span.name} for span in spans()]
    )
    for secret in secrets:
        assert secret not in everything + stored


# ZIF-146 (security N12): the account routes log ids and outcome codes only. Kills: logging the
# typed email, the name or phone, the account cookie or its hash.
def test_the_account_routes_log_no_personal_data(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    password = "Account-Pw-Zq-71"
    add_password(migrate_engine, people.both, password)
    email = email_of(people.both)
    name, phone = "Zzyzx Account-7", "+31687654321"
    never = [email, name, phone, "2001:db8:", UA["User-Agent"], password]
    client = client_for(app)

    wrong = client.post("/api/account/session", json={"email": email, "password": "Wrong-1"})
    assert wrong.status_code == 401
    right = client.post("/api/account/session", json={"email": email, "password": password})
    assert right.status_code == 200
    token = right.cookies.get(auth.ACCOUNT_COOKIE)
    assert token
    never += [token, hashlib.sha256(token.encode()).hexdigest()]
    changed = client.patch("/api/account", json={"name": name, "phone": phone})
    assert changed.status_code == 200
    assert client.get("/api/account").json()["phone"] == phone
    assert client.request("DELETE", "/api/account/session", json={}).status_code == 204

    dumped = json.dumps(log_lines())
    assert any(line["msg"] == "access" for line in log_lines())
    for value in never:
        assert value not in dumped, value
