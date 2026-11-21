"""ZIF-80: ziftbook's fee on a payment comes from the business's plan and the booking's source, and
the console shows every fee on it (the business pays Stripe's own fee to Stripe)."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import payments, stripe_gateway
from app.main import create_app
from tests.conftest import (
    ALL_FEATURES,
    People,
    give_plan,
    member_id,
    new_client,
    save_setting,
    signed_in,
)
from tests.stripe_fake import FakeGateway, connect
from tests.test_availability_api import assign, weekdays
from tests.test_booking_payments_api import make_shop, pay_rows, place, sync
from tests.test_working_hours import seed

PRICE = 4000


@pytest.fixture
def app(people: People, fake_stripe: FakeGateway) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


def fees(direct: int, marketplace: int) -> dict[str, object]:
    return {**ALL_FEATURES, "platform_fee_direct": direct, "platform_fee_marketplace": marketplace}


def sent_fees(fake: FakeGateway) -> list[Any]:
    return [p["application_fee"] for op, p in fake.calls if op == "create_payment"]


def paid(
    app: FastAPI, people: People, service_id: str, fake: FakeGateway, stripe_fee: int | None
) -> str:
    """A booking paid in full; Stripe's fee on its charge is `stripe_fee` (None: not known yet)."""
    client = new_client(app)
    booking_id: str = place(app, people, service_id, payment="pay", client=client).json()["id"]
    intent = fake.latest(booking_id)
    fake.intents[intent]["fee"] = stripe_fee
    fake.confirm(intent)
    assert sync(client, people, booking_id).json()["payment"]["state"] == "paid"
    return booking_id


# ZIF-80 fence. Kills: the fee read from payment_accounts or the marketplace fee charged on a
# booking-page booking; the fee kept off the row while it reaches Stripe.
def test_direct_booking_pays_the_direct_fee(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    give_plan(people.a, fees(150, 900))
    service_id = make_shop(people, owner, fake_stripe, price=PRICE)

    made = place(app, people, service_id, payment="pay")

    assert made.status_code == 201, made.text
    assert sent_fees(fake_stripe) == [60]
    (row,) = pay_rows(people, made.json()["id"])
    assert (row["fee_mode"], row["application_fee_minor"]) == ("business_pays", 60)


# ZIF-80 fence. Kills: one fee for every source; the source ignored by _create.
def test_marketplace_booking_pays_the_marketplace_fee(
    people: People, fake_stripe: FakeGateway
) -> None:
    give_plan(people.a, fees(150, 900))
    account = connect(fake_stripe, people.a, enabled=True)

    def fee(source: str) -> int:
        booking = uuid.uuid4()
        _, charged, mode = payments._create(
            "pay", account, people.a, booking, PRICE, "EUR", 1, source
        )
        assert mode == "business_pays"
        return charged

    assert {s: fee(s) for s in ("marketplace", "booking_page", "merchant")} == {
        "marketplace": 360,
        "booking_page": 60,
        "merchant": 60,
    }
    assert sent_fees(fake_stripe) == [360, 60, 60]


# ZIF-80 fence. Kills: an application_fee_amount of 0 sent to Stripe (a fee object per charge for
# nothing); a fee on a card check.
@pytest.mark.parametrize("payment", ["pay", "check"])
def test_zero_fee_sends_no_application_fee(
    payment: str,
    people: People,
    app: FastAPI,
    owner: TestClient,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_stripe_gateway import PAYMENT_INTENT, Recorder

    give_plan(people.a, fees(0 if payment == "pay" else 150, 900))
    service_id = make_shop(people, owner, fake_stripe, price=PRICE)
    http = Recorder(body=PAYMENT_INTENT)
    monkeypatch.setattr(stripe_gateway, "gateway", stripe_gateway.Gateway(http_client=http))

    made = place(app, people, service_id, payment=payment)

    assert made.status_code == 201, made.text
    assert "application_fee" not in "".join(http.wire(i) for i in range(len(http.sent)))
    (row,) = pay_rows(people, made.json()["id"])
    assert row["application_fee_minor"] == 0


# ZIF-80 fence. Kills: a fee above the payment; the old fixed part still added; a half rounded to
# even (float round) instead of up.
@pytest.mark.parametrize(
    ("bp", "amount", "fee"),
    [
        (0, PRICE, 0),
        (150, PRICE, 60),
        (150, 333, 5),
        (100, 50, 1),
        (10000, PRICE, PRICE),
        (1, 1, 0),
    ],
)
def test_fee_never_exceeds_the_amount(bp: int, amount: int, fee: int) -> None:
    assert payments.application_fee(bp, amount) == fee


# ZIF-80 fence. Kills: the console hiding a fee; net computed without ziftbook's fee.
def test_booking_detail_reports_fees_and_net(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    give_plan(people.a, fees(150, 900))
    service_id = make_shop(people, owner, fake_stripe, price=PRICE)
    booking_id = paid(app, people, service_id, fake_stripe, 146)

    payment = owner.get(f"/api/bookings/{booking_id}").json()["payment"]

    assert payment["amount_minor"] == PRICE
    assert payment["fees"] == {
        "stripe_fee_minor": 146,
        "application_fee_minor": 60,
        "net_minor": PRICE - 146 - 60,
    }


# ZIF-80 fence. Kills: a net estimated (or Stripe's fee taken as 0) before Stripe reports it.
def test_net_is_null_until_stripe_fee_known(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    give_plan(people.a, fees(150, 900))
    service_id = make_shop(people, owner, fake_stripe, price=PRICE)
    booking_id = paid(app, people, service_id, fake_stripe, None)

    payment = owner.get(f"/api/bookings/{booking_id}").json()["payment"]

    assert payment["fees"] == {
        "stripe_fee_minor": None,
        "application_fee_minor": 60,
        "net_minor": None,
    }


# ZIF-80 fence (R35). Kills: the fees, or what the business receives, sent to a worker, in the
# booking detail or the pending queue; an owner's fees left out.
def test_only_an_owner_reads_the_fees(
    people: People, app: FastAPI, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    give_plan(people.a, fees(150, 900))
    service_id = make_shop(people, owner, fake_stripe, price=PRICE)
    seed(people.a, people.only_a, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.only_a))
    worker = signed_in(app, people.a, people.only_a)
    client = new_client(app)
    booking_id = place(
        app,
        people,
        service_id,
        payment="pay",
        client=client,
        member_id=str(member_id(people.a, people.only_a)),
    ).json()["id"]
    intent = fake_stripe.latest(booking_id)
    fake_stripe.intents[intent]["fee"] = 146
    fake_stripe.confirm(intent)
    assert sync(client, people, booking_id).json()["status"] == "pending"

    def payments_seen(member: TestClient) -> list[dict[str, Any]]:
        detail = member.get(f"/api/bookings/{booking_id}")
        assert detail.status_code == 200, detail.text
        (queued,) = [b for b in member.get("/api/bookings/pending").json() if b["id"] == booking_id]
        return [detail.json()["payment"], queued["payment"]]

    assert [p["fees"] for p in payments_seen(worker)] == [None, None]
    assert [p["fees"]["net_minor"] for p in payments_seen(owner)] == [PRICE - 146 - 60] * 2


# ZIF-80 fence. Kills: the payments settings stating a fee the plan does not charge, or no
# country to find Stripe's own pricing for.
def test_the_payments_settings_state_the_plan_fees(
    people: People, owner: TestClient, fake_stripe: FakeGateway
) -> None:
    give_plan(people.a, fees(0, 900))

    account = owner.get("/api/payments/account").json()

    assert (account["platform_fee_direct"], account["platform_fee_marketplace"]) == (0, 900)
    assert account["country"] == "NL"
