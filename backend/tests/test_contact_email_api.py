"""A business's contact email through the API (ZIF-232): a typed address waits for its inbox to
confirm it, and only a confirmed one is in effect."""

import hashlib
import secrets
import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, Row, event, text

import app as app_package
from app import contact_email, limits
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    add_membership,
    add_user,
    confirm_contact_email,
    events,
    fresh_address,
    migrate_tenant_context,
    new_client,
    put_settings,
    saved_settings,
    set_role,
    signed_in,
    wait_until_blocked,
)

pytestmark = pytest.mark.usefixtures("fresh_contact_limits")


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def waiting(migrate_engine: Engine, tenant_id: uuid.UUID) -> Row[Any] | None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        return conn.execute(
            text("SELECT id, email, token_hash, expires_at FROM contact_email_requests")
        ).first()


def contact_events(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return [
        (e["action"], e["actor_user_id"], e["details"])
        for e in events(migrate_engine, tenant_id=tenant_id, target="setting:contact_email")
    ]


# F2.3, F7.1 FENCE. Wrong impl: the PUT still saves the key (the address in effect unconfirmed),
# or it queues no link, or one not tied to the request (a later change could not stop it).
def test_a_typed_address_waits_and_is_sent_a_link(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = put_settings(owner, {"contact_email": " Shop@Example.COM "})

    assert (response.status_code, response.json()["contact_email"]) == (200, None)
    assert owner.get("/api/settings").json()["contact_email"] is None
    request = waiting(migrate_engine, people.a)
    assert request is not None
    assert (request.email, request.token_hash) == ("shop@example.com", None)
    assert request.expires_at is not None
    assert saved_settings(people.a) == {}
    assert queued(migrate_engine, people.a) == [f"email.contact_email:{people.a}:{request.id}"]
    with migrate_tenant_context(migrate_engine, people.a) as conn:  # the owner who asked
        assert conn.scalar(text("SELECT requested_by FROM contact_email_requests")) == people.both


def queued(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[str]:
    with migrate_engine.connect() as conn:
        return list(
            conn.scalars(
                text(
                    "SELECT dedupe_key FROM jobs"
                    " WHERE tenant_id = :t AND kind = 'email.contact_email' ORDER BY id"
                ),
                {"t": tenant_id},
            )
        )


# F7.1 FENCE. Wrong impl: a link on every save of the waiting address (the one already sent dies).
def test_the_same_address_again_sends_no_second_link(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    token = link(worker_engine, people.a, "shop@example.com")

    assert put_settings(owner, {"contact_email": "shop@example.com"}).status_code == 200
    assert put_settings(owner, {"language": "nl"}).status_code == 200

    assert queued(migrate_engine, people.a) == []
    assert new_client(app).post(CONFIRM, json={"token": token}).status_code == 204


def test_the_address_in_effect_stays_until_the_new_one_is_confirmed(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)

    response = put_settings(owner, {"contact_email": "new@example.com"})

    assert response.json()["contact_email"] == "old@example.com"
    assert waiting(migrate_engine, people.a).email == "new@example.com"  # type: ignore[union-attr]


# F2.7 FENCE. Wrong impl: the typed address in the audit details (it may be a third party's, and
# nobody confirmed it), or a setting_changed for a change that is not in effect.
def test_a_typed_address_is_recorded_without_it(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put_settings(owner, {"contact_email": "shop@example.com"}).status_code == 200
    assert put_settings(owner, {"contact_email": "shop@example.com"}).status_code == 200

    assert contact_events(migrate_engine, people.a) == [
        ("contact_email_requested", people.both, None)
    ]


# F2.5 FENCE. Wrong impl: a clear that removes only the waiting address.
def test_null_clears_the_address_in_effect_and_the_waiting_one(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "new@example.com"}).status_code == 200

    response = put_settings(owner, {"contact_email": None})

    assert (response.status_code, response.json()["contact_email"]) == (200, None)
    assert (saved_settings(people.a), waiting(migrate_engine, people.a)) == ({}, None)
    assert contact_events(migrate_engine, people.a)[-1] == (
        "setting_changed",
        people.both,
        {"old": "old@example.com", "new": None},
    )
    assert put_settings(owner, {"contact_email": None}).status_code == 200
    assert len(contact_events(migrate_engine, people.a)) == 2  # nothing in effect: not recorded


def test_typing_the_address_in_effect_drops_the_waiting_one(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "new@example.com"}).status_code == 200

    response = put_settings(owner, {"contact_email": "Old@Example.com"})

    assert response.json()["contact_email"] == "old@example.com"
    assert waiting(migrate_engine, people.a) is None
    assert len(contact_events(migrate_engine, people.a)) == 1


# F2.6 FENCE. Wrong impl: an absent key treated as null.
def test_a_save_of_other_keys_leaves_both_addresses_alone(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "new@example.com"}).status_code == 200
    before = waiting(migrate_engine, people.a)

    assert put_settings(owner, {"language": "nl"}).status_code == 200

    assert waiting(migrate_engine, people.a) == before
    assert saved_settings(people.a)["contact_email"] == "old@example.com"


def test_a_worker_member_cannot_type_one(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)

    assert put_settings(worker, {"contact_email": "shop@example.com"}).status_code == 403
    assert waiting(migrate_engine, people.a) is None


NAMES = (
    "contact_email_requests",
    "change_contact_email",
    "resend_contact_email",
    "confirm_contact_email",
    "mint_contact_email_token",
    "cancel_contact_email",
)


# F2.8 FENCE. Wrong impl: another module reading the typed address, for clients or anyone.
def test_only_the_contact_email_module_and_the_mailer_name_the_requests() -> None:
    here = Path(app_package.__file__).parent
    naming = sorted(
        path.name
        for path in here.glob("*.py")
        if any(name in path.read_text() for name in NAMES)
        and path.name not in {"contact_email.py", "mail.py"}
    )
    assert naming == []


CONFIRM = "/api/contact-email/confirm"


def link(worker_engine: Engine, tenant_id: uuid.UUID, email: str) -> str:
    """The fragment of a freshly mailed link for email, as the job mints it, asked for by one of
    the business's owners."""
    with tenant_context(tenant_id) as session:
        request_id = session.execute(
            text(
                "SELECT request_id FROM change_contact_email(:e, true,"
                " (SELECT user_id FROM memberships WHERE role = 'owner' ORDER BY id LIMIT 1))"
            ),
            {"e": email},
        ).scalar_one()
    secret = secrets.token_urlsafe(32)
    with worker_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        assert conn.execute(
            text("SELECT * FROM mint_contact_email_token(:id, :h)"),
            {"id": request_id, "h": hashlib.sha256(secret.encode()).digest()},
        ).first()
    return f"{tenant_id}.{secret}"


# F5.1 FENCE. Wrong impl: an answer that names the business or the address, or a confirm that is
# not audited.
def test_a_live_link_confirms_the_address(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    confirm_contact_email(people.a, "old@example.com")
    token = link(worker_engine, people.a, "new@example.com")

    response = new_client(app).post(CONFIRM, json={"token": token})

    assert (response.status_code, response.content) == (204, b"")
    assert response.headers["cache-control"] == "no-store"
    assert saved_settings(people.a)["contact_email"] == "new@example.com"
    assert waiting(migrate_engine, people.a) is None
    assert contact_events(migrate_engine, people.a)[-1] == (
        "setting_changed",
        None,
        {"old": "old@example.com", "new": "new@example.com"},
    )


# F5.2, F5.3 FENCE. Wrong impl: a different answer for any of these (the endpoint would tell
# whether a business, an address or a link exists), or a link that works twice.
def test_every_dead_link_gets_the_same_answer(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    set_role(people.b, people.only_b, "owner")  # b's link needs an owner who asked for it
    used = link(worker_engine, people.b, "used@example.com")
    assert new_client(app).post(CONFIRM, json={"token": used}).status_code == 204
    resent = link(worker_engine, people.a, "resent@example.com")
    with tenant_context(people.a) as session:
        session.execute(text("SELECT resend_contact_email()"))
    changed = link(worker_engine, people.a, "changed@example.com")
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "x@example.com"}).status_code == 200
    lapsed_tenant, secret = link(worker_engine, people.b, "lapsed@example.com").split(".")
    with migrate_tenant_context(migrate_engine, people.b) as conn:
        conn.execute(
            text("UPDATE contact_email_requests SET expires_at = now() - interval '1 second'")
        )
    dead = [
        "nonsense",
        f"{uuid.uuid4()}.{secrets.token_urlsafe(32)}",
        f"{people.a}.{secrets.token_urlsafe(32)}",
        f"{people.a}.short",
        f"{people.a}.{secret}",  # the lapsed link under another business's id
        f"{lapsed_tenant}.{secret}",
        used,
        resent,
        changed,
    ]

    answers = [new_client(app).post(CONFIRM, json={"token": token}) for token in dead]

    assert {(a.status_code, a.text) for a in answers} == {(400, '{"code":"invalid_token"}')}
    assert saved_settings(people.a) == {}


# F5.4 FENCE. Wrong impl: no limit, or one counted after the lookup (a guess then costs a
# database transaction before it is refused).
def test_the_eleventh_try_from_one_address_is_refused_before_any_lookup(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = new_client(app, fresh_address())
    for _ in range(10):
        assert client.post(CONFIRM, json={"token": "nonsense"}).status_code == 400

    def no_lookup(*args: object, **kwargs: object) -> None:
        raise AssertionError("looked up")

    monkeypatch.setattr(contact_email, "tenant_context", no_lookup)
    over = client.post(CONFIRM, json={"token": f"{people.a}.{secrets.token_urlsafe(32)}"})

    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})


# F5.6 FENCE. Wrong impl: a GET that confirms (a mail scanner's prefetch would), or a form post.
def test_only_a_json_post_confirms(people: People, app: FastAPI, worker_engine: Engine) -> None:
    token = link(worker_engine, people.a, "shop@example.com")
    client = new_client(app)

    assert client.get(CONFIRM, params={"token": token}).status_code == 405
    assert client.post(CONFIRM, data={"token": token}).status_code == 415
    assert saved_settings(people.a) == {}


# F5.5 FENCE. Wrong impl: the token, its hash or the address in a log line.
def test_a_confirm_logs_ids_only(
    people: People, app: FastAPI, worker_engine: Engine, log_lines: Any
) -> None:
    token = link(worker_engine, people.a, "zzyzx-confirm@example.com")
    secret = token.split(".")[1]

    assert new_client(app).post(CONFIRM, json={"token": token}).status_code == 204

    lines = log_lines()
    (confirmed,) = [line for line in lines if line["msg"] == "contact email confirmed"]
    assert confirmed["tenant_id"] == str(people.a)
    logged = str(lines)
    for value in (secret, hashlib.sha256(secret.encode()).hexdigest(), "zzyzx-confirm"):
        assert value not in logged


STATE = "/api/settings/contact-email"


def lapse(migrate_engine: Engine, tenant_id: uuid.UUID) -> None:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        conn.execute(
            text("UPDATE contact_email_requests SET expires_at = now() - interval '1 second'")
        )


def queue_link(tenant_id: uuid.UUID, email: str) -> None:
    """A request with a link queued and not yet minted, as the PUT leaves one."""
    with tenant_context(tenant_id) as session:
        session.execute(text("SELECT change_contact_email(:e, true)"), {"e": email})


def state_of(body: dict[str, Any]) -> tuple[Any, ...]:
    return (body["state"], body["confirmed"], body["requested"], body["link_expired"])


# F7.5, F7.6 FENCE. Wrong impl: a state read from the setting alone, a waiting address read as
# live, or "sent" taken from the mint (a queued link would then read as never sent).
def test_the_state_says_which_address_clients_see_and_which_waits(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    def state() -> tuple[Any, ...]:
        response = owner.get(STATE)
        assert response.headers["cache-control"] == "no-store"
        return state_of(response.json())

    assert state() == ("none", None, None, False)
    with tenant_context(people.a) as session:  # no link due, as 0088 leaves a moved address
        session.execute(text("SELECT change_contact_email('moved@example.com', false)"))
    assert state() == ("unconfirmed", None, "moved@example.com", False)
    queue_link(people.a, "moved@example.com")
    assert state() == ("sent", None, "moved@example.com", False)  # queued, not yet minted
    lapse(migrate_engine, people.a)
    assert state() == ("unconfirmed", None, "moved@example.com", True)
    with tenant_context(people.a) as session:
        session.execute(text("SELECT change_contact_email(NULL, false)"))
    confirm_contact_email(people.a, "shop@example.com")
    assert state() == ("confirmed", "shop@example.com", None, False)
    queue_link(people.a, "next@example.com")
    body = owner.get(STATE).json()
    assert state_of(body) == ("sent", "shop@example.com", "next@example.com", False)
    assert body["expires_at"] is not None


# S9 FENCE. Wrong impl: a worker member reading the waiting address (nobody confirmed it, and it
# may be a third party's).
def test_only_owners_see_the_waiting_address(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    confirm_contact_email(people.a, "shop@example.com")
    queue_link(people.a, "next@example.com")

    body = signed_in(app, people.a, people.only_a).get(STATE).json()

    assert state_of(body) == ("sent", "shop@example.com", None, False)


def test_the_state_needs_a_session(app: FastAPI) -> None:
    assert new_client(app).get(STATE).status_code == 401


# F7.2 FENCE. Wrong impl: limits per business only (a ring of businesses floods one inbox), keyed
# on the raw address (aliases of one inbox each get their own count), or a refused save that still
# saves the other keys sent with it.
def test_the_fourth_link_in_an_hour_to_one_address_is_refused(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    local = uuid.uuid4().hex[:12]
    aliases = [f"{local}@gmail.com", f"{local[:4]}.{local[4:]}@gmail.com", f"{local}+x@gmail.com"]
    for alias in aliases:
        assert put_settings(owner, {"contact_email": alias}).status_code == 200
        assert put_settings(owner, {"contact_email": None}).status_code == 200
    before = waiting(migrate_engine, people.a)

    over = put_settings(owner, {"language": "pt", "contact_email": f"{local}@googlemail.com"})

    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})
    assert (waiting(migrate_engine, people.a), saved_settings(people.a)) == (before, {})
    assert len(queued(migrate_engine, people.a)) == 3


@pytest.fixture
def owners(people: People, app: FastAPI, migrate_engine: Engine) -> Iterator[list[TestClient]]:
    """Owners of four businesses: a and b, and two more made here."""
    extra: list[tuple[uuid.UUID, uuid.UUID]] = []
    for name in ("c", "d"):
        with migrate_engine.begin() as conn:
            tenant = conn.scalar(
                text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": name}
            )
        user = add_user(migrate_engine)
        add_membership(tenant, user, "owner")
        extra.append((tenant, user))
    set_role(people.b, people.only_b, "owner")
    yield [
        signed_in(app, people.a, people.both),
        signed_in(app, people.b, people.only_b),
        *(signed_in(app, tenant, user) for tenant, user in extra),
    ]
    for tenant, user in extra:
        with migrate_tenant_context(migrate_engine, tenant) as conn:
            conn.execute(text("DELETE FROM sessions WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM settings"))
            conn.execute(text("DELETE FROM memberships"))
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


def send_and_clear(owner: TestClient, address: str, times: int) -> None:
    for _ in range(times):
        assert put_settings(owner, {"contact_email": address}).status_code == 200
        assert put_settings(owner, {"contact_email": None}).status_code == 200


def hits(key: str) -> int:
    return limits.count(key, timedelta(days=1))


# Security review 1 FENCE. Wrong impl: one count per address shared by every business, which lets
# other businesses use up a business's links to its own inbox.
def test_other_businesses_cannot_use_up_a_business_links_to_an_address(
    owners: list[TestClient],
) -> None:
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    a, b, c, _ = owners
    send_and_clear(b, address, 3)
    send_and_clear(c, address, 2)  # five links to the address today, from two others

    assert put_settings(a, {"contact_email": address}).status_code == 200


def senders(migrate_engine: Engine, address: str) -> int:
    """The businesses counted today toward the inbox of address."""
    key = contact_email.address_key(limits.address_for_limits(address))
    with migrate_engine.connect() as conn:
        found: int = conn.scalar(
            text(
                "SELECT count(*) FROM contact_email_senders"
                f" WHERE address_key = :k AND {contact_email.LIVE}"
            ),
            {"k": key},
        )
    return found


TODAY = {"code": "contact_email_requests_today"}


# Security review FENCE. Wrong impl: no cap on how many businesses mail one inbox (a ring of them
# floods it), a cap that blocks a business that already mailed it today, or a refusal that is
# still counted (a refused business would then pass the cap as one that already mailed it).
def test_a_fourth_business_mailing_one_address_in_a_day_is_refused(
    migrate_engine: Engine, owners: list[TestClient]
) -> None:
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    a, b, c, d = owners
    for owner in (a, b, c):
        send_and_clear(owner, address, 1)

    refused = put_settings(d, {"contact_email": address})
    again = put_settings(d, {"contact_email": address})

    assert [refused.json(), again.json()] == [TODAY, TODAY]
    assert refused.status_code == 429
    assert senders(migrate_engine, address) == 3
    assert put_settings(c, {"contact_email": address}).status_code == 200


# Security review FENCE (targeted lockout). Wrong impl: other businesses able to keep a business
# from an address it once had confirmed, by mailing it first.
def test_a_business_that_had_the_address_confirmed_is_never_kept_from_it(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    worker_engine: Engine,
    owners: list[TestClient],
) -> None:
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    a, b, c, d = owners
    token = link(worker_engine, people.a, address)
    assert new_client(app).post(CONFIRM, json={"token": token}).status_code == 204
    assert put_settings(a, {"contact_email": None}).status_code == 200
    with migrate_engine.begin() as conn:  # a's link of the day lapsed: only the confirm is left
        conn.execute(
            text("DELETE FROM contact_email_senders WHERE tenant_id = :t"), {"t": people.a}
        )
    for owner in (b, c, d):
        send_and_clear(owner, address, 1)

    assert put_settings(a, {"contact_email": address}).status_code == 200


# Security review FENCE (race). Wrong impl: "first link of the day" inferred from a counter, which
# two businesses racing can both pass.
def test_two_businesses_racing_for_the_last_place_get_one(
    app_engine: Engine, migrate_engine: Engine, owners: list[TestClient]
) -> None:
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    a, b, c, d = owners
    for owner in (a, b):
        send_and_clear(owner, address, 1)
    results: list[int] = []

    def put(owner: TestClient) -> None:
        results.append(put_settings(owner, {"contact_email": address}).status_code)

    key = contact_email.address_key(limits.address_for_limits(address))
    with app_engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"),
            {"c": contact_email.SENDERS_LOCK, "k": key},
        )
        threads = [threading.Thread(target=put, args=(owner,)) for owner in (c, d)]
        for thread in threads:
            thread.start()
        wait_until_blocked(app_engine, 2)
    for thread in threads:
        thread.join()

    assert sorted(results) == [200, 429]
    assert senders(migrate_engine, address) == 3


# Security review FENCE (race, a request undone mid-race). Wrong impl: a failed request that gives
# back its business's place while a link from that business stands, so a fourth business passes.
def test_a_failed_request_gives_back_nothing_and_holds_its_place_briefly(
    people: People, migrate_engine: Engine, owners: list[TestClient]
) -> None:
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    a, b, c, d = owners
    send_and_clear(b, address, 1)
    send_and_clear(c, address, 3)
    assert put_settings(c, {"contact_email": address}).status_code == 429  # c's own hourly limit
    assert senders(migrate_engine, address) == 2  # c still counts: its links stand
    send_and_clear(a, address, 1)
    assert put_settings(d, {"contact_email": address}).json() == TODAY

    other = f"{uuid.uuid4().hex[:12]}@example.com"
    for owner in (b, c):
        send_and_clear(owner, other, 1)
    sent = contact_email.sent_key(people.a, limits.address_for_limits(other))
    for _ in range(5):
        limits.charge(sent, timedelta(days=1))
    assert put_settings(a, {"contact_email": other}).json() == {"code": "rate_limited"}
    assert senders(migrate_engine, other) == 2  # over its own limit, a takes no place
    key = contact_email.address_key(limits.address_for_limits(other))
    with migrate_engine.begin() as conn:  # a claim whose request then failed
        conn.execute(
            text(
                "INSERT INTO contact_email_senders (address_key, tenant_id, claimed_at)"
                " VALUES (:k, :t, now())"
            ),
            {"k": key, "t": people.a},
        )
    assert put_settings(d, {"contact_email": other}).json() == TODAY  # holds its place a while
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE contact_email_senders SET claimed_at = now() - interval '6 minutes'"
                " WHERE tenant_id = :t AND linked_at IS NULL"
            ),
            {"t": people.a},
        )
    assert put_settings(d, {"contact_email": other}).status_code == 200  # and lets it go


def test_a_business_sends_one_address_five_links_a_day(people: People, app: FastAPI) -> None:
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    sent = contact_email.sent_key(people.a, limits.address_for_limits(address))
    for _ in range(5):
        limits.charge(sent, timedelta(days=1))

    over = put_settings(signed_in(app, people.a, people.both), {"contact_email": address})

    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})
    assert hits(sent) == 5  # rolled back with the request


# Wrong impl: a count kept for a link refused by this business's own limit, or for a save that
# queues no link.
def test_a_save_that_queues_no_link_counts_nothing(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    sent = contact_email.sent_key(people.a, limits.address_for_limits(address))
    send_and_clear(owner, address, 3)
    assert put_settings(owner, {"contact_email": address}).status_code == 429  # 4th this hour
    confirm_contact_email(people.a, address)
    assert put_settings(owner, {"contact_email": address}).status_code == 200  # kept: no link

    assert hits(sent) == 3


# Security review FENCE (pool). Wrong impl: any limiter work on a second pooled connection once the
# request holds a row lock (a flood then fills the pool with requests each waiting on another),
# including a refund in a finally, or a key saved before the contact email's claim.
def test_nothing_takes_a_second_connection_once_a_row_is_locked(
    people: People, app: FastAPI, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def spy(module: Any, name: str) -> None:
        real = getattr(module, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(f"{name} in request" if kwargs.get("db") is not None else f"{name} own")
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, call)

    for name in ("hit", "charge", "refund"):
        spy(limits, name)
    spy(contact_email, "claim")
    locking = ("change_contact_email", "resend_contact_email", "INSERT INTO settings")

    def seen(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        calls.extend(name for name in locking if name in statement)

    owner = signed_in(app, people.a, people.both)
    event.listen(app_engine, "before_cursor_execute", seen)
    try:
        for resend in (False, True):
            calls.clear()
            if resend:
                answer = owner.post(RESEND, json={})
            else:
                answer = put_settings(owner, {"language": "nl", "contact_email": "a@x.nl"})
            assert answer.status_code == 200
            first = min(calls.index(name) for name in locking if name in calls)
            assert calls[:first] == ["claim own"], calls
            assert [c for c in calls[first:] if c.endswith(" own")] == [], calls
    finally:
        event.remove(app_engine, "before_cursor_execute", seen)


# S3 FENCE. Wrong impl: a limit checked against a state read before the request row was locked,
# so a save that turns out to queue a link is not counted.
def test_every_queued_link_is_counted(
    people: People, app: FastAPI, migrate_engine: Engine, app_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "first@example.com"}).status_code == 200
    results: list[int] = []

    def put(address: str) -> None:
        results.append(put_settings(owner, {"contact_email": address}).status_code)

    with migrate_tenant_context(migrate_engine, people.a) as conn:
        conn.execute(text("SELECT 1 FROM contact_email_requests FOR UPDATE"))
        threads = [
            threading.Thread(target=put, args=(a,)) for a in ("b@example.com", "c@example.com")
        ]
        for thread in threads:
            thread.start()
        wait_until_blocked(app_engine, 2)
    for thread in threads:
        thread.join()

    assert results == [200, 200]
    with migrate_engine.connect() as conn:
        hits = conn.scalar(
            text("SELECT hits FROM rate_limits WHERE key = :k"),
            {"k": f"contact_email:tenant:{people.a}"},
        )
    assert hits == len(queued(migrate_engine, people.a)) == 3


RESEND = "/api/settings/contact-email/confirmation"


# F7.3 FENCE. Wrong impl: CurrentSession on the resend (a worker member mails a third party), or
# a resend with nothing waiting that queues a job anyway.
def test_only_an_owner_resends_and_only_a_waiting_address(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    worker = signed_in(app, people.a, people.only_a)

    nothing = owner.post(RESEND, json={})
    assert (nothing.status_code, nothing.json()) == (409, {"code": "no_contact_email_request"})
    queue_link(people.a, "shop@example.com")
    refused = worker.post(RESEND, json={})
    assert (refused.status_code, refused.json()) == (403, {"code": "owner_only"})
    assert queued(migrate_engine, people.a) == []


# F7.4 FENCE. Wrong impl: a resend that keeps the id or the hash (the old link would still work,
# or the new job would mint for a request that no longer has its id).
def test_a_resend_kills_the_old_link_and_queues_a_new_one(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    old = link(worker_engine, people.a, "shop@example.com")

    response = owner.post(RESEND, json={})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert state_of(response.json()) == ("sent", None, "shop@example.com", False)
    request = waiting(migrate_engine, people.a)
    assert request is not None and request.token_hash is None
    assert queued(migrate_engine, people.a) == [f"email.contact_email:{people.a}:{request.id}"]
    assert new_client(app).post(CONFIRM, json={"token": old}).status_code == 400
    assert contact_events(migrate_engine, people.a)[-1] == (
        "contact_email_requested",
        people.both,
        None,
    )
    with migrate_tenant_context(migrate_engine, people.a) as conn:  # the resender now asks
        assert conn.scalar(text("SELECT requested_by FROM contact_email_requests")) == people.both


def test_a_resend_counts_toward_the_same_limits(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": "shop@example.com"}).status_code == 200
    for _ in range(2):
        assert owner.post(RESEND, json={}).status_code == 200
    before = waiting(migrate_engine, people.a)

    over = owner.post(RESEND, json={})

    assert (over.status_code, over.json()) == (429, {"code": "rate_limited"})
    assert waiting(migrate_engine, people.a) == before


def notices(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[str]:
    with migrate_engine.connect() as conn:
        return list(
            conn.scalars(
                text(
                    "SELECT payload ->> 'recipient_id' FROM jobs WHERE tenant_id = :t"
                    " AND kind = 'email.send' AND payload ->> 'template' = 'contact_email_changed'"
                ),
                {"t": tenant_id},
            )
        )


# S2 FENCE. Wrong impl: no notice (an inbox an attacker confirmed keeps client replies unseen), a
# notice to a worker member, or one for a change that is not in effect.
def test_the_owners_are_told_when_the_address_in_effect_changes(
    people: People, app: FastAPI, migrate_engine: Engine, worker_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"contact_email": None}).status_code == 200
    assert put_settings(owner, {"contact_email": "typed@example.com"}).status_code == 200
    assert notices(migrate_engine, people.a) == []

    token = link(worker_engine, people.a, "shop@example.com")
    assert new_client(app).post(CONFIRM, json={"token": token}).status_code == 204
    assert notices(migrate_engine, people.a) == [str(people.both)]

    assert put_settings(owner, {"contact_email": None}).status_code == 200
    assert notices(migrate_engine, people.a) == [str(people.both)] * 2
    assert notices(migrate_engine, people.b) == []


# Wrong impl: a link queued uncounted when another save changed the address between the unlocked
# read and the lock, or a count kept for a link that was not queued.
@pytest.mark.parametrize("guess", ["false", "true"])
def test_a_save_raced_by_another_is_refused_as_busy(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    guess: str,
) -> None:
    owner = signed_in(app, people.a, people.both)
    address = f"{uuid.uuid4().hex[:12]}@example.com"
    if guess == "true":
        assert put_settings(owner, {"contact_email": address}).status_code == 200
    before = waiting(migrate_engine, people.a)
    sent = contact_email.sent_key(people.a, limits.address_for_limits(address))
    counted = hits(sent)
    monkeypatch.setattr(
        contact_email, "WILL_SEND", text(f"SELECT {guess} WHERE CAST(:e AS text) IS NOT NULL")
    )

    raced = put_settings(owner, {"contact_email": address})

    assert (raced.status_code, raced.json()) == (503, {"code": "busy"})
    assert (waiting(migrate_engine, people.a), hits(sent)) == (before, counted)
