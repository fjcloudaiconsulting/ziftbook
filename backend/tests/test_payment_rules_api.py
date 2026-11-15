"""ZIF-62: how clients pay, per service, fixed on the booking when it is made.

Fake Stripe throughout; the shop, the booking POST and the settle runs are ZIF-158's
(test_booking_payments_api, test_booking_refunds).
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import People, new_client, put_settings, save_setting, signed_in
from tests.stripe_fake import FakeGateway
from tests.test_booking_links_api import cancel_body, get, linked, post
from tests.test_booking_payments_api import (
    _slug,
    make_shop,
    pay_rows,
    place,
    resume,
    status_of,
    sync,
)
from tests.test_booking_refunds import settle_all
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import patch

PRICE = 2500


@pytest.fixture
def app(people: People, fake_stripe: FakeGateway) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


def shop(
    people: People, owner: TestClient, fake: FakeGateway, rule: str, pct: int | None = None
) -> str:
    """make_shop's service and business (the switch on, Stripe ready), sold under this rule."""
    service_id = make_shop(people, owner, fake, price=PRICE)
    ruled = owner.patch(
        f"/api/services/{service_id}", json={"payment_rule": rule, "deposit_pct": pct}
    )
    assert ruled.status_code == 200, ruled.text
    return service_id


def page_service(app: FastAPI, people: People) -> dict[str, Any]:
    page = new_client(app).get(f"/api/public/booking-pages/{_slug(people)}").json()
    (service,) = page["services"]
    out: dict[str, Any] = service
    return out


def detail(owner: TestClient, booking_id: str) -> dict[str, Any]:
    response = owner.get(f"/api/bookings/{booking_id}")
    assert response.status_code == 200, response.text
    out: dict[str, Any] = response.json()
    return out


def charged(fake: FakeGateway) -> list[int]:
    return [params["amount"] for op, params in fake.calls if op == "create_payment"]


def euros(amount_minor: int) -> dict[str, Any]:
    return {"amount_minor": amount_minor, "currency": "EUR"}


# ZIF-62 FENCE. Kills: a deposit or prepaid service that still takes a card check (a check holds
# no money); a refused kind that still places a booking or reaches Stripe; a deposit charged the
# full price; a booking page that shows a step the create would refuse, or another amount.
@pytest.mark.parametrize(("rule", "pct", "due"), [("deposit", 30, 750), ("prepaid", None, PRICE)])
def test_a_deposit_or_prepaid_service_takes_only_a_payment_of_what_is_due(
    rule: str,
    pct: int | None,
    due: int,
    people: People,
    app: FastAPI,
    owner: TestClient,
    fake_stripe: FakeGateway,
) -> None:
    service_id = shop(people, owner, fake_stripe, rule, pct)
    offered = page_service(app, people)
    assert (offered["payment"], offered["payment_rule"], offered["payment_due"]) == (
        True,
        rule,
        euros(due),
    )

    refused = place(app, people, service_id, payment="check")
    assert (refused.status_code, refused.json()) == (422, {"code": "payment_not_offered"})
    assert fake_stripe.calls == []

    made = place(app, people, service_id, payment="pay")
    assert made.status_code == 201, made.text
    booking_id = made.json()["id"]
    assert made.json()["price"] == euros(PRICE)  # the booking still costs the price
    assert charged(fake_stripe) == [due]
    assert [(r["kind"], r["amount_minor"]) for r in pay_rows(people, booking_id)] == [("pay", due)]
    shown = detail(owner, booking_id)
    assert (shown["payment_rule"], shown["payment_due"]) == (rule, euros(due))


# ZIF-62 FENCE. Kills: an in_person rule that is ignored while the business takes payments (the
# client is asked to pay for a service sold in person), or a client who can still choose to pay.
def test_an_in_person_service_books_with_no_payment_step(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = shop(people, owner, fake_stripe, "in_person")
    offered = page_service(app, people)
    assert (offered["payment"], offered["payment_rule"], offered["payment_due"]) == (
        False,
        None,
        None,
    )

    refused = place(app, people, service_id, payment="pay")
    assert (refused.status_code, refused.json()) == (422, {"code": "payment_not_offered"})

    made = place(app, people, service_id, payment=None)
    assert made.status_code == 201, made.text
    assert (made.json()["status"], made.json()["payment"]) == ("pending", None)
    shown = detail(owner, made.json()["id"])
    assert (shown["payment_rule"], shown["payment_due"], shown["payment"]) == (None, None, None)
    assert fake_stripe.calls == []


# ZIF-62 FENCE. Kills: a resume that reads the service's rule now instead of the booking's (a
# deposit booking switched to a card check, or charged a new percentage); a service edit that
# rewrites a booking's snapshot.
def test_the_booking_keeps_the_rule_it_was_placed_under(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = shop(people, owner, fake_stripe, "deposit", 30)
    client = new_client(app)
    made = place(app, people, service_id, payment="pay", client=client)
    assert made.status_code == 201, made.text
    booking_id = made.json()["id"]
    fake_stripe.intents[fake_stripe.latest(booking_id)]["status"] = "canceled"  # a new one next

    changed = owner.patch(
        f"/api/services/{service_id}", json={"payment_rule": "choice", "deposit_pct": None}
    )
    assert changed.status_code == 200, changed.text

    refused = resume(client, people, booking_id, "check")
    assert (refused.status_code, refused.json()) == (422, {"code": "payment_not_offered"})
    again = resume(client, people, booking_id, "pay")
    assert again.status_code == 200, again.text
    assert charged(fake_stripe) == [750, 750]  # a new intent, the same deposit
    assert [(r["status"], r["amount_minor"]) for r in pay_rows(people, booking_id)] == [
        ("canceled", 750),
        ("open", 750),
    ]
    shown = detail(owner, booking_id)
    assert (shown["payment_rule"], shown["payment_due"]) == ("deposit", euros(750))

    # And the other way: a choice booking may still be switched once the service is prepaid.
    placed = place(app, people, service_id, payment="check", client=client, starts_at=at("11:00"))
    assert placed.status_code == 201, placed.text
    other = placed.json()["id"]
    assert (
        owner.patch(f"/api/services/{service_id}", json={"payment_rule": "prepaid"}).json()[
            "payment_rule"
        ]
        == "prepaid"
    )
    switched = resume(client, people, other, "pay")
    assert switched.status_code == 200, switched.text
    assert [r["kind"] for r in pay_rows(people, other)] == ["check", "pay"]
    assert charged(fake_stripe)[-1] == PRICE
    assert detail(owner, other)["payment_rule"] == "choice"


# ZIF-62 FENCE. Kills: a deposit refunded as a share of the price (more than was paid), or a
# business ending that keeps part of it.
@pytest.mark.parametrize(
    ("ending", "hours", "refunds"),
    [("client cancel", 0, [750]), ("client cancel", 720, []), ("declined", 0, [750])],
)
def test_a_deposit_is_refunded_by_the_cancellation_rules(
    ending: str,
    hours: int,
    refunds: list[int],
    people: People,
    app: FastAPI,
    owner: TestClient,
    fake_stripe: FakeGateway,
) -> None:
    service_id = shop(people, owner, fake_stripe, "deposit", 30)
    assert put_settings(owner, {"free_cancellation_hours": hours}).status_code == 200
    client = new_client(app)
    booking_id = place(app, people, service_id, payment="pay", client=client).json()["id"]
    fake_stripe.confirm(fake_stripe.latest(booking_id))
    assert sync(client, people, booking_id).json()["status"] == "pending"

    if ending == "declined":
        assert patch(owner, booking_id, "declined").status_code == 200
    else:
        # Accepted first: a client who withdraws a request still waiting is always refunded.
        assert patch(owner, booking_id, "confirmed").status_code == 200
        linked_client = linked(app, people.a, booking_id)
        pct = get(linked_client).json()["engine"]["refund_pct"]
        assert post(linked_client, "/cancel", cancel_body(booking_id, pct)).status_code == 200
    settle_all()

    assert fake_stripe.refunded() == refunds
    assert status_of(people, booking_id) in ("declined", "cancelled_by_client")


# ZIF-62 FENCE. Kills: a sync answer without the booking's snapshot (a page back on an
# awaiting_payment booking would fall back to the service's current rule and show another
# amount), or one read from the service instead of the booking.
def test_sync_answers_the_booking_s_rule_and_amount_after_the_service_changed(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    service_id = shop(people, owner, fake_stripe, "deposit", 30)
    client = new_client(app)
    booking_id = place(app, people, service_id, payment="pay", client=client).json()["id"]
    changed = owner.patch(
        f"/api/services/{service_id}",
        json={"payment_rule": "prepaid", "deposit_pct": None, "price": {"amount_minor": 4000}},
    )
    assert changed.status_code == 200, changed.text

    answer = sync(client, people, booking_id)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["status"], body["payment_rule"], body["payment_due"], body["price"]) == (
        "awaiting_payment",
        "deposit",
        euros(750),
        euros(PRICE),
    )
