"""ZIF-158 B1: Stripe's webhook, signature first, dedupe by event id, its own body cap."""

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import payments
from app.jobs import run_once
from app.main import WEBHOOK_MAX_BODY, create_app
from app.worker import KINDS
from tests.conftest import People, closed, jobs_as_the_worker, new_client, points
from tests.stripe_fake import WEBHOOK_SECRET, FakeGateway, connect, event_body, signed

WRONG_TYPES = (
    b'{"id": "evt_1", "type": ["account.updated"], "account": {"id": "acct_1"},'
    b' "data": {"object": {"id": "acct_1"}}}'
)
JSON = {"content-type": "application/json"}
Lines = Callable[[], list[dict[str, Any]]]


@pytest.fixture
def client(people: People) -> TestClient:
    return new_client(create_app())


def post(client: TestClient, body: bytes, headers: dict[str, str]) -> Any:
    return client.post(payments.WEBHOOK_PATH, content=body, headers=headers)


def stripe_jobs(migrate_engine: Engine) -> list[str]:
    with migrate_engine.connect() as conn:
        return sorted(
            conn.scalars(text("SELECT dedupe_key FROM jobs WHERE kind = 'stripe.event'")).all()
        )


def run_jobs() -> None:
    """Runs the queued jobs as the worker does, as ziftbook_worker (ZB-08)."""

    async def drain() -> None:
        while await run_once(KINDS):
            pass

    with jobs_as_the_worker():
        asyncio.run(drain())


# ZIF-158 F1. fence. Kills: parsing the JSON before verifying; verifying a re-serialised body
# (json.dumps(await request.json()) breaks the HMAC only for real, indented payloads); no tolerance.
@pytest.mark.parametrize(
    "case",
    [
        "no header",
        "wrong secret",
        "changed body",
        "old",
        "not json",
        "signed not json",
        "signed not an event",
        "signed wrong types",
    ],
)
def test_an_unsigned_or_badly_signed_event_is_refused(
    case: str,
    client: TestClient,
    people: People,
    fake_stripe: FakeGateway,
    migrate_engine: Engine,
    log_lines: Lines,
) -> None:
    account = connect(fake_stripe, people.a)
    body = event_body(account=account)
    headers = {
        "no header": JSON,
        "wrong secret": signed(body, secret="whsec_somebody_else"),
        "changed body": signed(body),
        "old": signed(body, t=int(time.time()) - 301),
        "not json": signed(b"not json", secret="whsec_somebody_else"),
        "signed not json": signed(b"not json"),
        "signed not an event": signed(b'{"hello": 1}'),
        "signed wrong types": signed(WRONG_TYPES),
    }[case]
    sent = {
        "changed body": body + b" ",
        "not json": b"not json",
        "signed not json": b"not json",
        "signed not an event": b'{"hello": 1}',
        "signed wrong types": WRONG_TYPES,
    }.get(case, body)
    before = stripe_jobs(migrate_engine)

    response = post(client, sent, headers)

    assert (response.status_code, response.json()) == (400, {"code": "bad_signature"})
    assert stripe_jobs(migrate_engine) == before
    evt = json.loads(body)["id"]
    assert evt not in json.dumps(log_lines())

    # The control: the same indented body, signed properly, is taken.
    assert post(client, body, signed(body)).status_code == 200


# ZIF-158 F2. fence. Kills: dedupe on the object id (two events for one account collapse), or none.
def test_one_event_is_handled_once(
    client: TestClient, people: People, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    account = connect(fake_stripe, people.a)
    body = event_body(account=account)
    before = stripe_jobs(migrate_engine)

    first = post(client, body, signed(body))
    again = post(client, body, signed(body))

    assert (first.status_code, first.json(), again.status_code, again.json()) == (200, {}, 200, {})
    evt = json.loads(body)["id"]
    assert [k for k in stripe_jobs(migrate_engine) if k not in before] == [f"stripe.event:{evt}"]
    run_jobs()
    assert fake_stripe.count("retrieve_account") == 1

    other = event_body(account=account)
    assert post(client, other, signed(other)).status_code == 200
    assert len([k for k in stripe_jobs(migrate_engine) if k not in before]) == 2


# ZIF-158 F3. fence. Kills: a 4xx/5xx for an account we do not know (Stripe retries for days and
# then disables the endpoint).
def test_an_event_for_an_unknown_account_is_accepted_and_dropped(
    client: TestClient, people: People, fake_stripe: FakeGateway, migrate_engine: Engine
) -> None:
    body = event_body(account="acct_nobody_we_know")
    before = stripe_jobs(migrate_engine)

    response = post(client, body, signed(body))

    assert (response.status_code, response.json()) == (200, {})
    assert stripe_jobs(migrate_engine) == before


# ZIF-158 F4. fence. Kills: raising MAX_BODY globally; a cap only on the declared length.
def test_the_webhook_takes_256_kib_and_nothing_else_does(
    client: TestClient, people: People, fake_stripe: FakeGateway
) -> None:
    account = connect(fake_stripe, people.a)
    ok = event_body(account=account, pad=200 * 1024)
    assert 200 * 1024 < len(ok) < WEBHOOK_MAX_BODY
    assert post(client, ok, signed(ok)).status_code == 200

    big = event_body(account=account, pad=257 * 1024)
    declared = post(client, big, signed(big))
    assert (declared.status_code, declared.json()) == (413, {"code": "content_too_large"})

    def chunks(body: bytes) -> Iterator[bytes]:
        yield from (body[i : i + 4096] for i in range(0, len(body), 4096))

    chunked = client.post(payments.WEBHOOK_PATH, content=chunks(big), headers=signed(big))
    assert "content-length" not in chunked.request.headers
    assert (chunked.status_code, chunked.json()) == (413, {"code": "content_too_large"})

    padded = b"{}" + b" " * (65 * 1024 + 1 - 2)  # literal: a raised MAX_BODY must not move it
    for content in (padded, chunks(padded)):
        response = client.post("/api/sign-up", content=content, headers=JSON)
        assert (response.status_code, response.json()) == (413, {"code": "content_too_large"})


# ZIF-158 F12 (webhook half, with the three keys). fence. Kills: treating a missing key as on.
@pytest.mark.parametrize(
    "name", ["ZIF_STRIPE_SECRET_KEY", "ZIF_STRIPE_WEBHOOK_SECRET", "ZIF_STRIPE_PUBLISHABLE_KEY"]
)
def test_the_webhook_answers_503_while_any_key_is_unset(
    name: str,
    client: TestClient,
    fake_stripe: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(name)
    body = event_body()

    response = post(client, body, signed(body, secret=WEBHOOK_SECRET))

    assert (response.status_code, response.json()) == (503, {"code": "payments_unavailable"})


# ZIF-158 G20 (metrics half). guard. The closed attribute set holds.
def test_the_webhook_counts_what_it_did(
    client: TestClient, people: People, fake_stripe: FakeGateway, metric_reader: Any
) -> None:
    account = connect(fake_stripe, people.a)
    body = event_body(account=account)
    other = event_body(account="acct_nobody_we_know", type_="charge.succeeded")

    post(client, body, signed(body))
    post(client, body, signed(body))
    post(client, other, signed(other))
    post(client, body, {**signed(body), "Stripe-Signature": "t=1,v1=00"})

    counted = {
        (p.attributes["stripe.event"], p.attributes["stripe.result"]): p.value
        for p in points(metric_reader, "ziftbook.stripe.webhooks")
    }
    assert counted == {
        ("account.updated", "enqueued"): 1,
        ("account.updated", "duplicate"): 1,
        ("other", "ignored"): 1,
        ("other", "refused"): 1,
    }
    closed(metric_reader)


def test_the_webhook_is_not_in_the_api_contract(people: People) -> None:
    app: FastAPI = create_app()
    assert payments.WEBHOOK_PATH not in app.openapi()["paths"]
