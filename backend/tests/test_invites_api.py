"""POST/GET/DELETE /api/invites and the lookup/accept endpoints an invite link opens."""

import hashlib
import json
import uuid
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from app import auth, mail, passwords
from app.db import tenant_context
from app.jobs import Job
from app.main import create_app
from tests.conftest import (
    PASSWORD,
    People,
    add_password,
    email_of,
    events,
    failing,
    fresh_address,
    fresh_email,
    new_client,
    set_role,
    signed_in,
)
from tests.test_account_email import bound, inbox, link_in, message  # noqa: F401
from tests.test_invite_email import run_jobs
from tests.test_invites_db import NewAccount, minted, new_accounts, pending  # noqa: F401
from tests.test_password_auth_db import HASH


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def mint(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, invite_id: uuid.UUID) -> str:
    """Run the invite email job with delivery spied on, and return the link's fragment. No
    Mailpit round trip."""
    captured: dict[str, str] = {}

    def spy(to: str, subject: str, body: str, headers: dict[str, str] | None = None) -> None:
        captured["body"] = body

    monkeypatch.setattr(mail, "deliver", spy)
    mail.send_invite(Job(uuid4(), "email.invite", tenant_id, {"invite_id": str(invite_id)}))
    return link_in(captured["body"]).split("#", 1)[1]


def invite_row_count(tenant_id: uuid.UUID) -> int:
    with tenant_context(tenant_id) as session:
        result = session.scalar(text("SELECT count(*) FROM invites"))
    return int(result)


def invite_jobs(migrate_engine: Engine) -> int:
    with migrate_engine.connect() as conn:
        count = conn.scalar(text("SELECT count(*) FROM jobs WHERE kind = 'email.invite'"))
    return int(count)


# ---------------------------------------------------------------------------
# Create, list, revoke
# ---------------------------------------------------------------------------


# I1: a normalised email, a job queued with the right dedupe key and payload.
def test_creating_an_invite_queues_its_email_job(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = owner.post("/api/invites", json={"email": " New@Example.com "})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["email"] == "new@example.com"
    assert body["expired"] is False
    invite_id = body["id"]
    # By tenant and kind, not dedupe_key: a wrong key must fail the assertions below, not vanish
    # from a WHERE clause and raise NoResultFound instead.
    with migrate_engine.connect() as conn:
        job = conn.execute(
            text("""
            SELECT kind, tenant_id, dedupe_key, payload FROM jobs
            WHERE tenant_id = :t AND kind = 'email.invite' ORDER BY id DESC LIMIT 1
            """),
            {"t": people.a},
        ).one()
    assert job.kind == "email.invite"
    assert str(job.tenant_id) == str(people.a)
    assert job.dedupe_key == f"email.invite:{people.a}:{invite_id}"
    assert job.payload == {"invite_id": invite_id}


# I2: a resend kills the old link before its job even runs.
def test_resending_an_invite_kills_the_old_link(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()

    first = owner.post("/api/invites", json={"email": email})
    assert first.status_code == 201
    run_jobs()
    (sent1,) = inbox(email)
    token1 = link_in(message(sent1["ID"])[0]).split("#", 1)[1]
    assert owner.post("/api/invites/lookup", json={"token": token1}).status_code == 200

    second = owner.post("/api/invites", json={"email": email})
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]

    # Before the second job even runs, the first link is already dead.
    assert owner.post("/api/invites/lookup", json={"token": token1}).status_code == 400

    run_jobs()
    new_messages = [m for m in inbox(email) if m["ID"] != sent1["ID"]]
    assert len(new_messages) == 1
    token2 = link_in(message(new_messages[0]["ID"])[0]).split("#", 1)[1]
    assert owner.post("/api/invites/lookup", json={"token": token2}).status_code == 200
    assert owner.post("/api/invites/lookup", json={"token": token1}).status_code == 400


# I2b: resending an already-expired invite is live again, in both the 201 body and the listing.
def test_resending_an_expired_invite_is_live_again(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()
    first = owner.post("/api/invites", json={"email": email}).json()
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("UPDATE invites SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": first["id"]},
        )

    resend = owner.post("/api/invites", json={"email": email})

    assert resend.status_code == 201
    assert resend.json()["expired"] is False
    listing = owner.get("/api/invites").json()
    (item,) = [i for i in listing if i["id"] == resend.json()["id"]]
    assert item["expired"] is False


# I3: a worker gets 403 on every invite endpoint, and nothing is written.
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/invites", {"email": "someone@example.com"}),
        ("POST", "/api/invites", []),
        ("GET", "/api/invites", None),
        ("DELETE", "/api/invites/{id}", {}),
    ],
)
def test_a_worker_cannot_manage_invites(
    people: People, app: FastAPI, migrate_engine: Engine, method: str, path: str, body: Any
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    path = path.format(id=uuid.uuid7())

    response = worker.request(method, path, json=body)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert invite_row_count(people.a) == 0
    assert invite_jobs(migrate_engine) == 0


# I4: inviting an existing member of this business is 409; a member of another business is fine.
def test_inviting_an_existing_member_is_refused(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    same_business = owner.post("/api/invites", json={"email": email_of(people.only_a)})
    assert (same_business.status_code, same_business.json()) == (
        409,
        {"code": "already_member"},
    )
    assert owner.post("/api/invites", json={"email": email_of(people.both)}).status_code == 409
    assert invite_row_count(people.a) == 0
    assert invite_jobs(migrate_engine) == 0
    assert owner.post("/api/invites", json={"email": email_of(people.only_b)}).status_code == 201


# I5: per business+email, and per business, rate limits.
def test_invite_rate_limits(
    people: People, app: FastAPI, app_engine: Engine, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()

    for _ in range(3):
        assert owner.post("/api/invites", json={"email": email}).status_code == 201
    assert owner.post("/api/invites", json={"email": email}).status_code == 429

    set_role(people.b, people.only_b, "owner")
    owner_b = signed_in(app, people.b, people.only_b)
    assert owner_b.post("/api/invites", json={"email": email}).status_code == 201

    key = f"invite:tenant:{people.a}"
    with migrate_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO rate_limits (key, window_start, hits)
            VALUES (:k, now() - interval '23 hours', 50)
            ON CONFLICT (key) DO UPDATE
              SET window_start = excluded.window_start, hits = excluded.hits
            """),
            {"k": key},
        )
    assert owner.post("/api/invites", json={"email": fresh_email()}).status_code == 429
    with migrate_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE rate_limits SET window_start = now() - interval '25 hours' WHERE key = :k"
            ),
            {"k": key},
        )
    assert owner.post("/api/invites", json={"email": fresh_email()}).status_code == 201


# I6: strict body validation, no role smuggling.
@pytest.mark.parametrize(
    "body",
    [
        {"email": "nope"},
        {"email": "x@y.co", "role": "owner"},
        {},
        [],
    ],
)
def test_invite_body_is_strictly_validated(people: People, app: FastAPI, body: Any) -> None:
    owner = signed_in(app, people.a, people.both)

    response = owner.post("/api/invites", json=body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert invite_row_count(people.a) == 0


# I7: the list is scoped to the business, ordered by id, with an expired flag and no hash.
def test_listing_invites(people: People, app: FastAPI, migrate_engine: Engine) -> None:
    owner = signed_in(app, people.a, people.both)
    live = owner.post("/api/invites", json={"email": fresh_email()}).json()
    expired = owner.post("/api/invites", json={"email": fresh_email()}).json()
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("UPDATE invites SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": expired["id"]},
        )
    set_role(people.b, people.only_b, "owner")
    signed_in(app, people.b, people.only_b).post("/api/invites", json={"email": fresh_email()})

    response = owner.get("/api/invites")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = response.json()
    assert [item["id"] for item in items] == [live["id"], expired["id"]]
    assert [item["expired"] for item in items] == [False, True]
    for item in items:
        assert set(item.keys()) == {"id", "email", "expires_at", "expired"}


# I8: revoking kills the link and any not-yet-run job; a foreign or unknown id is 404.
def test_revoking_an_invite(people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = signed_in(app, people.a, people.both)
    created = owner.post("/api/invites", json={"email": fresh_email()}).json()
    invite_id = created["id"]
    token = mint(monkeypatch, people.a, uuid.UUID(invite_id))

    response = owner.request("DELETE", f"/api/invites/{invite_id}", json={})

    assert response.status_code == 204
    assert response.text == ""
    assert owner.post("/api/invites/lookup", json={"token": token}).status_code == 400

    set_role(people.b, people.only_b, "owner")
    owner_b = signed_in(app, people.b, people.only_b)
    other = owner_b.post("/api/invites", json={"email": fresh_email()}).json()

    assert owner.request("DELETE", f"/api/invites/{other['id']}", json={}).status_code == 404
    assert owner.request("DELETE", f"/api/invites/{uuid.uuid7()}", json={}).status_code == 404
    with tenant_context(people.b) as session:
        still_there = session.scalar(
            text("SELECT true FROM invites WHERE id = :i"), {"i": other["id"]}
        )
    assert still_there is True

    # A real POST (its own queued job, not mint()'s spy), revoked before the job ever runs: the
    # job finds no row and sends nothing.
    queued_email = fresh_email()
    queued = owner.post("/api/invites", json={"email": queued_email}).json()
    assert owner.request("DELETE", f"/api/invites/{queued['id']}", json={}).status_code == 204
    run_jobs()
    assert inbox(queued_email) == []


# I9: audit events for invite, revoke and accept, and no email anywhere in them.
def test_invite_audit_events(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    new_accounts: list[NewAccount],  # noqa: F811
) -> None:
    owner = signed_in(app, people.a, people.both)
    email = fresh_email()
    created = owner.post("/api/invites", json={"email": email}).json()

    invited = events(migrate_engine, tenant_id=people.a, action="member_invited")
    assert invited[-1]["target"] == f"invite:{created['id']}"
    assert invited[-1]["actor_user_id"] == people.both
    assert invited[-1]["details"] is None

    revoked_email = fresh_email()
    second = owner.post("/api/invites", json={"email": revoked_email}).json()
    assert owner.request("DELETE", f"/api/invites/{second['id']}", json={}).status_code == 204
    revoked = events(migrate_engine, tenant_id=people.a, action="invite_revoked")
    assert revoked[-1]["target"] == f"invite:{second['id']}"
    assert revoked[-1]["actor_user_id"] == people.both
    assert revoked[-1]["details"] is None

    accepted_email = fresh_email()
    third = owner.post("/api/invites", json={"email": accepted_email}).json()
    token = mint(monkeypatch, people.a, uuid.UUID(third["id"]))
    accepted = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD}
    )
    assert accepted.status_code == 201
    new_user_id = uuid.UUID(accepted.json()["user_id"])
    new_accounts.append(NewAccount(tenant_id=people.a, user_id=new_user_id))
    accept_events = events(migrate_engine, tenant_id=people.a, action="invite_accepted")
    assert accept_events[-1]["target"] == f"user:{new_user_id}"
    assert accept_events[-1]["actor_user_id"] == new_user_id
    sign_ins = events(migrate_engine, tenant_id=people.a, action="sign_in_succeeded")
    assert sign_ins[-1]["actor_user_id"] == new_user_id

    every_event = events(migrate_engine, tenant_id=people.a)
    dumped = json.dumps(every_event, default=str)
    assert email not in dumped
    assert revoked_email not in dumped
    assert accepted_email not in dumped


# I10: recording and enqueueing share the create transaction.
def test_a_failed_audit_write_leaves_no_invite_or_job(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signed_in(app, people.a, people.both)
    failing(monkeypatch, auth, "record")

    response = owner.post("/api/invites", json={"email": fresh_email()})

    assert response.status_code == 500
    assert invite_row_count(people.a) == 0
    assert invite_jobs(migrate_engine) == 0


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


# L1: the link's business name and email, and whether the email already has an account.
def test_lookup_reports_business_and_account_status(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signed_in(app, people.a, people.both)
    fresh = fresh_email()
    created = owner.post("/api/invites", json={"email": fresh}).json()
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))

    response = new_client(app).post("/api/invites/lookup", json={"token": token})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"business_name": "a", "email": fresh, "has_account": False}

    known = owner.post("/api/invites", json={"email": email_of(people.only_b)}).json()
    token2 = mint(monkeypatch, people.a, uuid.UUID(known["id"]))
    response2 = new_client(app).post("/api/invites/lookup", json={"token": token2})
    assert response2.json()["has_account"] is True


# L2: every unusable link is a byte-identical 400 invalid_token; a too-long token is 422.
def test_lookup_of_an_unusable_link(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    new_accounts: list[NewAccount],  # noqa: F811
) -> None:
    invite_id, secret_a = minted(people.a, fresh_email())
    bad_tokens = [
        "abc",
        f"not-a-uuid.{secret_a}",
        f"{people.a}",
        f"{people.a}.",
        f"{people.a}.{secret_a}.x",
        f"{people.a}.{secret_a[:-1]}",
        f"{uuid.uuid7()}.{secret_a}",
        f"{people.a}.{'x' * 43}",
    ]

    expired_id, expired_secret = minted(people.a, fresh_email())
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(
            text("UPDATE invites SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": expired_id},
        )
    bad_tokens.append(f"{people.a}.{expired_secret}")
    bad_tokens.append(f"{people.b}.{secret_a}")  # a's token under b's id

    revoked_id, revoked_secret = minted(people.a, fresh_email())
    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM invites WHERE id = :i"), {"i": revoked_id})
    bad_tokens.append(f"{people.a}.{revoked_secret}")

    accept_id, accept_secret = minted(people.a, fresh_email())
    with tenant_context(people.a) as session:
        accepted = session.execute(
            text("SELECT * FROM accept_invite(:h, :p)"),
            {"h": hashlib.sha256(accept_secret.encode()).digest(), "p": HASH},
        ).one()
    new_accounts.append(NewAccount(tenant_id=people.a, user_id=accepted.account_id))
    bad_tokens.append(f"{people.a}.{accept_secret}")

    # A fresh client (and so IP) per call: this fence is about the answer's shape, not the lookup
    # rate limit (L4 covers that), and 12 calls on one IP would trip it.
    baseline = new_client(app).post("/api/invites/lookup", json={"token": bad_tokens[0]})
    assert (baseline.status_code, baseline.json()) == (400, {"code": "invalid_token"})
    for token in bad_tokens[1:]:
        response = new_client(app).post("/api/invites/lookup", json={"token": token})
        assert (response.status_code, response.json()) == (400, {"code": "invalid_token"})
        for header in baseline.headers:
            if header.lower() != "date":
                assert response.headers.get(header) == baseline.headers.get(header)

    long_response = new_client(app).post("/api/invites/lookup", json={"token": "y" * 101})
    assert long_response.status_code == 422


# L3: lookup is read-only; the link still accepts afterwards.
def test_lookup_does_not_consume_the_link(
    people: People,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    new_accounts: list[NewAccount],  # noqa: F811
) -> None:
    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": fresh_email()})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    client = new_client(app)

    assert client.post("/api/invites/lookup", json={"token": token}).status_code == 200
    assert client.post("/api/invites/lookup", json={"token": token}).status_code == 200
    response = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})

    assert response.status_code == 201
    new_accounts.append(
        NewAccount(tenant_id=people.a, user_id=uuid.UUID(response.json()["user_id"]))
    )


# L4: lookup is limited per IP, not globally, and never touches the sign-in budget.
def test_lookup_is_rate_limited_per_ip(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)
    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": email_of(people.only_b)})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    client = new_client(app)

    for _ in range(10):
        assert client.post("/api/invites/lookup", json={"token": token}).status_code == 200
    assert client.post("/api/invites/lookup", json={"token": token}).status_code == 429

    other = new_client(app)
    assert other.post("/api/invites/lookup", json={"token": token}).status_code == 200

    # The 10 lookups of only_b's link must not have spent any of its per-account sign-in budget.
    sign_in = new_client(app).post(
        "/api/session", json={"email": email_of(people.only_b), "password": PASSWORD}
    )
    assert sign_in.status_code == 200


# ---------------------------------------------------------------------------
# Accept
# ---------------------------------------------------------------------------


# C1: a fresh email creates an account, joins as a worker, signs in, and the link dies.
def test_accepting_creates_an_account_and_signs_in(
    people: People,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    new_accounts: list[NewAccount],  # noqa: F811
) -> None:
    email = fresh_email()
    created = (
        signed_in(app, people.a, people.both).post("/api/invites", json={"email": email}).json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    client = new_client(app)

    response = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})

    assert response.status_code == 201
    body = response.json()
    assert (body["tenant_id"], body["role"], body["email"]) == (str(people.a), "worker", email)
    assert "__Host-session" in response.cookies
    assert response.headers["cache-control"] == "no-store"
    new_accounts.append(NewAccount(tenant_id=people.a, user_id=uuid.UUID(body["user_id"])))

    session_response = client.get("/api/session")
    assert session_response.status_code == 200
    session_body = session_response.json()
    assert (session_body["tenant_id"], session_body["role"]) == (str(people.a), "worker")
    sign_in_response = new_client(app).post(
        "/api/session", json={"email": email, "password": PASSWORD}
    )
    assert sign_in_response.status_code == 200
    replay = new_client(app).post(
        "/api/invites/accept", json={"token": token, "password": PASSWORD}
    )
    assert replay.status_code == 400


# C2: an existing account, correct password, joins a second business without touching their hash,
# and (weak as it is) without ever running the new-password policy on it.
def test_accepting_with_an_existing_account_and_correct_password(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_password(migrate_engine, people.only_b, "short")
    with migrate_engine.connect() as conn:
        before = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": email_of(people.only_b)})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    client = new_client(app)
    hashed: list[str] = []

    def spy(password: str) -> str:
        hashed.append(password)
        return ""

    monkeypatch.setattr(passwords, "hash_password", spy)

    response = client.post("/api/invites/accept", json={"token": token, "password": "short"})

    assert response.status_code == 201
    assert response.json()["tenant_id"] == str(people.a)
    assert hashed == []
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert role == "worker"
    with tenant_context(people.b) as session:
        still_in_b = session.scalar(
            text("SELECT true FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert still_in_b is True
    with migrate_engine.connect() as conn:
        after = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    assert after == before
    # only_b's oldest business is b (added before a in `people`): the new session must be in the
    # invite's business, not wherever account_by_email would have put a plain sign-in.
    session_check = client.get("/api/session")
    assert session_check.status_code == 200
    assert (session_check.json()["tenant_id"], session_check.json()["role"]) == (
        str(people.a),
        "worker",
    )


# C3: an existing account, wrong password, is refused and changes nothing.
def test_accepting_with_an_existing_account_and_wrong_password(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)
    with migrate_engine.connect() as conn:
        before = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": email_of(people.only_b)})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    address = fresh_address()
    client = new_client(app, address)

    response = client.post(
        "/api/invites/accept", json={"token": token, "password": "totally-wrong-pass"}
    )

    assert (response.status_code, response.json()) == (401, {"code": "invalid_credentials"})
    assert "__Host-session" not in response.cookies
    with tenant_context(people.a) as session:
        member = session.scalar(
            text("SELECT true FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert member is None
    with tenant_context(people.a) as session:
        invite_gone = session.scalar(
            text("SELECT true FROM invites WHERE id = :i"), {"i": created["id"]}
        )
    assert invite_gone is True
    with migrate_engine.connect() as conn:
        after = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": people.only_b}
        )
    assert after == before
    matching = [e for e in events(migrate_engine, ip=address) if e["action"] == "sign_in_failed"]
    assert len(matching) == 1
    assert matching[0]["target"] == f"user:{people.only_b}"
    assert matching[0]["tenant_id"] is None

    ok = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})
    assert ok.status_code == 201


# C3b: an empty password is refused exactly like any other wrong one; verify() is not skipped.
def test_accepting_with_an_existing_account_and_an_empty_password(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)
    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": email_of(people.only_b)})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    client = new_client(app)

    response = client.post("/api/invites/accept", json={"token": token, "password": ""})

    assert (response.status_code, response.json()) == (401, {"code": "invalid_credentials"})
    assert "__Host-session" not in response.cookies
    with tenant_context(people.a) as session:
        member = session.scalar(
            text("SELECT true FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert member is None


# C4: a password reset mid-accept is refused.
def test_accepting_when_the_password_was_reset_meanwhile(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)
    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": email_of(people.only_b)})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    verify = passwords.verify

    def verify_then_reset(stored: str | None, password: str) -> bool:
        matched = verify(stored, password)
        with migrate_engine.begin() as conn:
            conn.execute(
                text("UPDATE password_credentials SET hash = :h WHERE user_id = :u"),
                {"h": passwords.hash_password("some-other-password-99"), "u": people.only_b},
            )
        return matched

    monkeypatch.setattr(passwords, "verify", verify_then_reset)
    client = new_client(app)

    response = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})

    assert response.status_code == 401
    with tenant_context(people.a) as session:
        member = session.scalar(
            text("SELECT true FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert member is None
    with tenant_context(people.a) as session:
        sessions = session.scalar(
            text("SELECT count(*) FROM sessions WHERE user_id = :u"), {"u": people.only_b}
        )
    assert sessions == 0


# C5: an account created between find() and accept_invite() is 409, and nothing new is written.
def test_accepting_when_an_account_appears_meanwhile(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = fresh_email()
    created = (
        signed_in(app, people.a, people.both).post("/api/invites", json={"email": email}).json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    real_hash = passwords.hash_password
    made: list[uuid.UUID] = []

    def racing(password: str) -> str:
        result = real_hash(password)
        user_id = uuid.uuid7()
        with migrate_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e)"), {"i": user_id, "e": email}
            )
            conn.execute(
                text("INSERT INTO password_credentials (user_id, hash) VALUES (:u, :h)"),
                {"u": user_id, "h": HASH},
            )
        made.append(user_id)
        return result

    monkeypatch.setattr(passwords, "hash_password", racing)
    client = new_client(app)

    response = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})

    assert (response.status_code, response.json()) == (409, {"code": "account_exists"})
    with tenant_context(people.a) as session:
        invite_gone = session.scalar(
            text("SELECT true FROM invites WHERE id = :i"), {"i": created["id"]}
        )
    assert invite_gone is True
    with migrate_engine.connect() as conn:
        cred_hash = conn.scalar(
            text("SELECT hash FROM password_credentials WHERE user_id = :u"), {"u": made[0]}
        )
    assert cred_hash == HASH
    with tenant_context(people.a) as session:
        member = session.scalar(
            text("SELECT true FROM memberships WHERE user_id = :u"), {"u": made[0]}
        )
    assert member is None

    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM password_credentials WHERE user_id = :u"), {"u": made[0]})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": made[0]})


# C6: accepting an invite for someone already a member is 409, and the invite is used up.
def test_accepting_when_already_a_member(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A direct insert, not the create endpoint: only_a is already a member of a, so the endpoint
    # would refuse the invite outright. This is the race the endpoint can't see coming.
    add_password(migrate_engine, people.only_a, PASSWORD)
    invite_id = pending(people.a, email_of(people.only_a))
    token = mint(monkeypatch, people.a, invite_id)
    client = new_client(app)

    response = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})

    assert (response.status_code, response.json()) == (409, {"code": "already_member"})
    assert "__Host-session" not in response.cookies
    with tenant_context(people.a) as session:
        # accept_invite deletes the invite before it discovers the conflict: the link is used up
        # even though the role is left alone.
        invite_gone = session.scalar(
            text("SELECT true FROM invites WHERE id = :i"), {"i": invite_id}
        )
    assert invite_gone is None
    with tenant_context(people.a) as session:
        role = session.scalar(
            text("SELECT role FROM memberships WHERE user_id = :u"), {"u": people.only_a}
        )
    assert role == "worker"


# C7: a dead link is refused before any password is checked or hashed.
def test_accepting_checks_the_password_before_the_link(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch, no_hashing: list[str]
) -> None:
    email = fresh_email()
    created = (
        signed_in(app, people.a, people.both).post("/api/invites", json={"email": email}).json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    client = new_client(app)

    short = client.post("/api/invites/accept", json={"token": token, "password": "short"})
    assert (short.status_code, short.json()) == (422, {"code": "password_too_short"})
    assert client.post("/api/invites/lookup", json={"token": token}).status_code == 200

    common = client.post("/api/invites/accept", json={"token": token, "password": "password1234"})
    assert (common.status_code, common.json()) == (422, {"code": "password_too_common"})

    dead = client.post("/api/invites/accept", json={"token": "not-a-uuid.x", "password": PASSWORD})
    assert (dead.status_code, dead.json()) == (400, {"code": "invalid_token"})
    assert no_hashing == []


# C8: accept is limited per IP and (for the existing-account path) per account.
def test_accept_rate_limits(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_password(migrate_engine, people.only_b, PASSWORD)
    address = fresh_address()
    client = new_client(app, address)
    for _ in range(10):
        response = client.post(
            "/api/invites/accept", json={"token": "nope.nope", "password": PASSWORD}
        )
        assert response.status_code == 400
    over = client.post("/api/invites/accept", json={"token": "nope.nope", "password": PASSWORD})
    assert over.status_code == 429

    # Per account: 10 wrong sign-ins for only_b, from fresh IPs, exhaust its own budget.
    for _ in range(10):
        wrong = new_client(app).post(
            "/api/session", json={"email": email_of(people.only_b), "password": "wrong-pass-xx"}
        )
        assert wrong.status_code == 401

    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": email_of(people.only_b)})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    final = new_client(app).post("/api/invites/accept", json={"token": token, "password": PASSWORD})
    assert final.status_code == 429
    with tenant_context(people.a) as session:
        member = session.scalar(
            text("SELECT true FROM memberships WHERE user_id = :u"), {"u": people.only_b}
        )
    assert member is None


# C9: accepting and signing in share one transaction.
def test_a_failed_sign_in_leaves_the_invite_unused_and_no_account(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = fresh_email()
    created = (
        signed_in(app, people.a, people.both).post("/api/invites", json={"email": email}).json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    failing(monkeypatch, auth, "start")
    client = new_client(app)

    response = client.post("/api/invites/accept", json={"token": token, "password": PASSWORD})

    assert response.status_code == 500
    with tenant_context(people.a) as session:
        invite_still_there = session.scalar(
            text("SELECT true FROM invites WHERE id = :i"), {"i": created["id"]}
        )
    assert invite_still_there is True
    with migrate_engine.connect() as conn:
        made = conn.scalar(text("SELECT true FROM users WHERE email = :e"), {"e": email})
    assert made is None


# C10: content-type guard, method guard, and Referrer-Policy on both success and error.
def test_accept_and_lookup_json_only_and_method_guards(
    people: People, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = new_client(app)

    plain_lookup = client.request(
        "POST", "/api/invites/lookup", headers={"content-type": "text/plain"}
    )
    assert plain_lookup.status_code == 415
    plain_accept = client.request(
        "POST", "/api/invites/accept", headers={"content-type": "text/plain"}
    )
    assert plain_accept.status_code == 415

    assert client.get("/api/invites/accept").status_code == 405

    created = (
        signed_in(app, people.a, people.both)
        .post("/api/invites", json={"email": fresh_email()})
        .json()
    )
    token = mint(monkeypatch, people.a, uuid.UUID(created["id"]))
    good = client.post("/api/invites/lookup", json={"token": token})
    assert good.headers["referrer-policy"] == "no-referrer"
    bad = client.post("/api/invites/lookup", json={"token": "not-a-uuid.x"})
    assert bad.headers["referrer-policy"] == "no-referrer"
