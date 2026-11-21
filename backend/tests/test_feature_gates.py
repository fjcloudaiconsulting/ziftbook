"""The plan's flags on features that predate plans (ZIF-78): online payments, deposits, email
reminders and worker permissions. A flag the plan drops turns the feature off where it is used;
the stored setting is kept, so an upgrade restores it. Money already in motion never asks."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app import business_settings
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    email_of,
    give_plan,
    member_id,
    new_client,
    put_settings,
    save_setting,
    saved_settings,
    signed_in,
)
from tests.stripe_fake import FakeGateway
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_email import (
    clean_outbox,  # noqa: F401 -- autouse: the request email test sends mail
    outbox_for,
    run_jobs,
    run_send_booking,
    template_of,
    texts_to,
)
from tests.test_booking_email import jobs_for_booking as jobs_of
from tests.test_booking_payments_api import make_shop, pay_rows, place, resume, sync
from tests.test_booking_refunds import booked, settle_all
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import make_pending, patch
from tests.test_payment_rules_api import PRICE, detail, euros, page_service, shop
from tests.test_services import service
from tests.test_working_hours import hours_path, seed

FREE: dict[str, object] = {"places": 1, "email_reminders": True}
PAYMENTS = FREE | {"online_payments": True}


def plan_limit(feature: str) -> dict[str, object]:
    return {"code": "plan_limit", "feature": feature, "limit": None}


@pytest.fixture
def app(people: People, fake_stripe: FakeGateway) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


# ZIF-78 fence. Kills: a business that lost online_payments still showing the card step, or a
# booking create that still asks Stripe for an intent.
def test_no_online_payments_means_no_checkout_step(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = make_shop(people, owner, fake_stripe)  # payment_check on, Stripe ready
    give_plan(people.a, FREE)

    assert page_service(app, people)["payment"] is False
    refused = place(app, people, service_id, payment="check")
    assert (refused.status_code, refused.json()) == (422, {"code": "payment_not_offered"})
    made = place(app, people, service_id, payment=None)
    assert made.status_code == 201, made.text
    assert made.json()["payment"] is None
    assert fake_stripe.calls == []


# ZIF-78 fence. Kills: a deposit taken without the deposits flag; a stored deposit that drops the
# payment step altogether instead of falling back to the choice.
@pytest.mark.parametrize(("rule", "pct"), [("deposit", 30), ("prepaid", None)])
def test_deposit_service_checks_out_as_choice_without_deposits(
    rule: str,
    pct: int | None,
    people: People,
    app: FastAPI,
    owner: TestClient,
    fake_stripe: FakeGateway,
) -> None:
    service_id = shop(people, owner, fake_stripe, rule, pct)
    give_plan(people.a, PAYMENTS)

    offered = page_service(app, people)
    assert (offered["payment"], offered["payment_rule"], offered["payment_due"]) == (
        True,
        "choice",
        euros(PRICE),
    )
    assert place(app, people, service_id, payment="check").status_code == 201


# ZIF-78 fence. Kills: a stored deposit that still asks for money once online payments are gone.
@pytest.mark.parametrize(("rule", "pct"), [("deposit", 30), ("prepaid", None)])
def test_deposit_service_is_in_person_without_payments(
    rule: str,
    pct: int | None,
    people: People,
    app: FastAPI,
    owner: TestClient,
    fake_stripe: FakeGateway,
) -> None:
    service_id = shop(people, owner, fake_stripe, rule, pct)
    give_plan(people.a, FREE)

    offered = page_service(app, people)
    assert (offered["payment"], offered["payment_rule"], offered["payment_due"]) == (
        False,
        None,
        None,
    )
    made = place(app, people, service_id, payment=None)
    assert made.status_code == 201, made.text
    assert detail(owner, made.json()["id"])["payment_rule"] is None
    assert fake_stripe.calls == []


# ZIF-78 fence. Kills: snapshotting the service's stored rule instead of the one sold (a resume
# would then offer a deposit the client never saw); an upgrade rewriting a booking already made.
def test_booking_snapshots_the_effective_rule(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = shop(people, owner, fake_stripe, "deposit", 30)
    give_plan(people.a, PAYMENTS)

    made = place(app, people, service_id, payment="pay")
    assert made.status_code == 201, made.text
    booking_id = made.json()["id"]
    sold = detail(owner, booking_id)
    assert (sold["payment_rule"], sold["payment_due"]) == ("choice", euros(PRICE))

    give_plan(people.a, PAYMENTS | {"deposits": True})
    assert page_service(app, people)["payment_rule"] == "deposit"  # the upgrade restores it
    kept = detail(owner, booking_id)
    assert (kept["payment_rule"], kept["payment_due"]) == ("choice", euros(PRICE))


# ZIF-78 fence. Kills: a new deposit or prepaid rule saved without the deposits flag; a business
# that lost the flag unable to save anything else on a service that already has one.
def test_saving_a_deposit_rule_without_deposits_is_402(
    people: People, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    kept = shop(people, owner, fake_stripe, "deposit", 30)
    plain = new_service(owner)
    give_plan(people.a, PAYMENTS)

    created = service(owner, payment_rule="prepaid")
    assert (created.status_code, created.json()) == (402, plan_limit("deposits"))
    changed = owner.patch(
        f"/api/services/{plain}", json={"payment_rule": "deposit", "deposit_pct": 20}
    )
    assert (changed.status_code, changed.json()) == (402, plan_limit("deposits"))
    switched = owner.patch(
        f"/api/services/{kept}", json={"payment_rule": "prepaid", "deposit_pct": None}
    )
    assert (switched.status_code, switched.json()) == (402, plan_limit("deposits"))

    resaved = owner.patch(
        f"/api/services/{kept}", json={"payment_rule": "deposit", "duration_minutes": 45}
    )
    assert resaved.status_code == 200, resaved.text
    assert resaved.json()["payment_rule"] == "deposit"
    assert (
        owner.patch(
            f"/api/services/{kept}", json={"payment_rule": "choice", "deposit_pct": None}
        ).status_code
        == 200
    )


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def reminders(app_engine: Engine, people: People, booking_id: str) -> list[dict[str, Any]]:
    return [
        j for j in jobs_of(app_engine, people.a, booking_id) if template_of(j) == "booking_reminder"
    ]


# ZIF-78 fence. Kills: the 24h reminder queued without email_reminders; a reminder queued before
# a downgrade still mailed after it.
def test_no_reminder_without_email_reminders(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    queued = make_pending(app, people.a, ready)
    assert patch(owner, queued, "confirmed").status_code == 200
    (reminder,) = reminders(app_engine, people, queued)  # on the all-features plan

    give_plan(people.a, {"places": 1, "email_reminders": False})
    skipped = make_pending(app, people.a, ready, starts_at=at("11:00"))
    assert patch(owner, skipped, "confirmed").status_code == 200
    assert reminders(app_engine, people, skipped) == []

    job = run_send_booking(
        people.a, queued, "booking_reminder", starts_at=reminder["payload"]["starts_at"]
    )
    assert outbox_for(people.a, job.id) is None


@pytest.fixture
def worker(app: FastAPI, people: People, owner: TestClient) -> tuple[TestClient, str]:
    """only_a, a non-owner, performing a service: (their session, the service)."""
    service_id = new_service(owner)
    seed(people.a, people.only_a, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.only_a))
    return signed_in(app, people.a, people.only_a), service_id


# ZIF-78 fence. Kills: the stored worker settings read as-is, so a business that lost
# worker_permissions still lets workers answer requests and edit their own hours.
def test_worker_cannot_answer_when_worker_permissions_off_even_if_setting_on(
    people: People, app: FastAPI, worker: tuple[TestClient, str]
) -> None:
    session, service_id = worker
    save_setting(people.a, "workers_answer_requests", True)
    save_setting(people.a, "workers_edit_own_hours", True)
    give_plan(people.a, FREE)
    worker_member = member_id(people.a, people.only_a)

    booking_id = make_pending(app, people.a, service_id, member_id=str(worker_member))
    refused = patch(session, booking_id, "confirmed")
    assert (refused.status_code, refused.json()) == (403, {"code": "owner_only"})
    week = [{"weekday": 1, "starts_at": "09:00", "ends_at": "12:00"}]
    hours = session.put(hours_path(worker_member), json=week)
    assert (hours.status_code, hours.json()) == (403, {"code": "owner_only"})

    give_plan(people.a, FREE | {"worker_permissions": True})  # the upgrade restores both
    assert patch(session, booking_id, "confirmed").status_code == 200
    assert session.put(hours_path(worker_member), json=week).status_code == 200


# ZIF-78 fence. Kills: the request email read from the stored setting, telling a worker who may
# not answer (the plan lost worker_permissions) to accept or decline.
def test_a_worker_without_worker_permissions_gets_the_owner_answers_email(
    people: People, app: FastAPI, worker: tuple[TestClient, str]
) -> None:
    _, service_id = worker
    save_setting(people.a, "workers_answer_requests", True)
    give_plan(people.a, FREE)

    as_worker = str(member_id(people.a, people.only_a))
    make_pending(app, people.a, service_id, member_id=as_worker)
    run_jobs()

    (text,) = texts_to(email_of(people.only_a))
    assert "The business owner accepts or declines it." in text
    assert "Accept or decline" not in text


# ZIF-78 fence. Kills: a PUT that turns a gated setting on without the plan's flag; one that
# refuses a business re-sending, or turning off, a setting it already had.
@pytest.mark.parametrize(
    ("key", "feature"),
    [
        ("payment_check", "online_payments"),
        ("workers_edit_own_hours", "worker_permissions"),
        ("workers_answer_requests", "worker_permissions"),
    ],
)
def test_settings_put_turning_on_a_gated_flag_is_402(
    key: str, feature: str, people: People, owner: TestClient
) -> None:
    give_plan(people.a, FREE)

    refused = put_settings(owner, {key: True, "buffer_pct": 20})
    assert (refused.status_code, refused.json()) == (402, plan_limit(feature))
    assert saved_settings(people.a) == {"published": True}  # nothing in the request saved
    assert put_settings(owner, {key: False}).status_code == 200

    with tenant_context(people.a) as session:
        business_settings.save(session, key, True)  # stored on before the downgrade
    kept = put_settings(owner, {key: True})
    assert kept.status_code == 200, kept.text
    assert kept.json()[key] is True  # the stored truth, kept for an upgrade


# ZIF-78 fence. Kills: a refund gated on the plan, so a business that lost online_payments keeps
# a client's money after declining their request.
def test_refunds_ignore_entitlements(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = make_shop(people, owner, fake_stripe, price=PRICE)
    _, booking_id, _ = booked(app, people, fake_stripe, service_id, "pay")
    give_plan(people.a, FREE)

    assert patch(owner, booking_id, "declined").status_code == 200
    settle_all()
    assert len(fake_stripe.refunds) == 1


# ZIF-78 R31 fence. Kills: a resume's switch between checking the card and paying that asks the
# plan now, so a client mid-checkout is stuck after the business lost online_payments.
def test_a_resume_switch_ignores_entitlements(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = make_shop(people, owner, fake_stripe)
    client = new_client(app)
    made = place(app, people, service_id, payment="check", client=client)
    assert made.status_code == 201, made.text
    booking_id = made.json()["id"]
    first = fake_stripe.latest(booking_id)
    give_plan(people.a, FREE)

    switched = resume(client, people, booking_id, "pay")

    assert switched.status_code == 200, switched.text
    assert fake_stripe.latest(booking_id) != first
    assert [r["kind"] for r in pay_rows(people, booking_id)] == ["check", "pay"]
    assert sync(client, people, booking_id).status_code == 200
