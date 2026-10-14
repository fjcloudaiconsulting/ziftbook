"""ZIF-71: the admin second factor over HTTP, its three guards, and the admin sign-in limiter."""

import base64
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import admin_auth, auth, totp
from app.admin_auth import ADMIN_COOKIE
from app.main import create_app
from tests.conftest import PASSWORD, add_password, add_user, email_of, fresh_address, new_client
from tests.test_second_factor_db import factor, issue_ticket

PASS = "/api/admin/session/second-factor"
BEGIN = "/api/admin/totp/enrolment"
CONFIRM = "/api/admin/totp/enrolment/confirm"
OTHER_KEY = base64.b64encode(
    b"k" * 32
).decode()  # valid, but not the key the secret was sealed with


@pytest.fixture
def app(bound: None) -> FastAPI:
    return create_app()


@pytest.fixture
def an_admin(app_engine: Engine, migrate_engine: Engine, bound: None) -> uuid.UUID:
    """A platform admin with a password, not enrolled."""
    user_id = add_user(app_engine)
    add_password(migrate_engine, user_id, PASSWORD)
    with migrate_engine.begin() as conn:
        conn.execute(text("INSERT INTO platform_admins (user_id) VALUES (:u)"), {"u": user_id})
    return user_id


def sign_in(app: FastAPI, user_id: uuid.UUID, address: str | None = None) -> TestClient:
    """A half session: the password only."""
    client = new_client(app, address)
    response = client.post(
        "/api/admin/session", json={"email": email_of(user_id), "password": PASSWORD}
    )
    assert response.status_code == 200, response.content
    return client


def step_now() -> int:
    return int(time.time()) // totp.STEP


@dataclass(frozen=True)
class Verified:
    client: TestClient  # its session has passed the second factor, at step t0
    secret: bytes
    codes: list[str]
    t0: int
    begun: Response
    confirmed: Response


def verified_client(app: FastAPI, migrate_engine: Engine, user_id: uuid.UUID) -> Verified:
    """CLI enrol, then sign in, begin and confirm over HTTP.

    Step rule: confirm used t0 (last_step, read as migrate), so a pass at t0 is a replay. A later
    leg that expects "passes" sends totp.code(s, t0 + 1), valid while the clock's step is t0 to
    t0 + 2: at least 60 s after this returns. A leg that expects a refusal never needs a fresh step.
    """
    ticket = issue_ticket(user_id)  # before sign-in: enrol ends the admin's sessions
    client = sign_in(app, user_id)
    begun = client.post(BEGIN, json={"ticket": ticket})
    assert begun.status_code == 200, begun.content
    s = base64.b32decode(begun.json()["secret"])
    confirmed = client.post(CONFIRM, json={"code": totp.code(s, step_now())})
    assert confirmed.status_code == 200, confirmed.content
    t0 = factor(migrate_engine, user_id)["last_step"]
    return Verified(client, s, confirmed.json()["recovery_codes"], t0, begun, confirmed)


def wrong_code(v: Verified) -> str:
    """Six ASCII digits that are no code of any step the server could accept during this test."""
    taken = {totp.code(v.secret, step) for step in range(v.t0 - 1, v.t0 + 4)}
    return next(c for c in (f"{i:06d}" for i in range(1_000_000)) if c not in taken)


def failed_attempts(migrate_engine: Engine, user_id: uuid.UUID) -> int:
    count: int = factor(migrate_engine, user_id)["failed_attempts"]
    return count


def audit(migrate_engine: Engine, user_id: uuid.UUID) -> list[tuple[str, Any]]:
    """This admin's platform audit events, oldest first: (action, details)."""
    with migrate_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action, details FROM platform_audit_events "
                "WHERE actor_user_id = :u ORDER BY id"
            ),
            {"u": user_id},
        )
        return [(row.action, row.details) for row in rows]


def answer(response: Response) -> tuple[int, Any]:
    return response.status_code, response.json()


def probe(app: FastAPI, router: APIRouter) -> str:
    """Mount one route behind that router's guard; its path."""
    guarded = APIRouter(tags=["probe"], dependencies=router.dependencies)
    path = f"/probe/{uuid.uuid4().hex}"

    @guarded.get(path)
    def reached() -> dict[str, bool]:
        return {"reached": True}

    app.include_router(guarded)
    return path


# fence: a read route (admin_router) refuses a session that has only passed the password. Kills
# admin_router guarded by the half guard. Injected by swapping its dependency to admin_session.
def test_admin_router_refuses_a_half_session(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    path = probe(app, admin_auth.admin_router)
    v = verified_client(app, migrate_engine, an_admin)

    half = sign_in(app, an_admin)
    assert answer(half.get(path)) == (403, {"code": "second_factor_required"})
    assert answer(v.client.get(path)) == (200, {"reached": True})
    assert answer(new_client(app).get(path)) == (401, {"code": "unauthenticated"})


# fence: a write route (admin_write_router) needs the factor within 15 minutes, decided in SQL.
# Kills a missing or wrong freshness interval. Injected by '15 minutes' -> '8 hours' in 0039, then
# separately by admin_recently_verified skipping the fresh check.
def test_fresh_factor_is_15_minutes(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    path = probe(app, admin_auth.admin_write_router)
    v = verified_client(app, migrate_engine, an_admin)
    id_hash = auth.hash_token(v.client.cookies[ADMIN_COOKIE])

    def passed_ago(minutes: int) -> None:
        with migrate_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE admin_sessions SET second_factor_at = now() - make_interval(mins => :m)"
                    " WHERE id_hash = :id"
                ),
                {"m": minutes, "id": id_hash},
            )

    passed_ago(16)
    assert answer(v.client.get(path)) == (403, {"code": "second_factor_stale"})
    passed_ago(14)
    assert answer(v.client.get(path)) == (200, {"reached": True})
    half = sign_in(app, an_admin)
    assert answer(half.get(path)) == (403, {"code": "second_factor_required"})


# fence: wrong recovery codes from one address can't lock the admin out of them. Kills a per-user
# recovery limit. Injected by keying the limit on the user id.
def test_recovery_codes_cannot_be_locked_by_others(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    v = verified_client(app, migrate_engine, an_admin)
    attacker = sign_in(app, an_admin, fresh_address())  # holds the password

    tries = [attacker.post(PASS, json={"code": totp.new_code()}).status_code for _ in range(30)]

    assert tries == [401] * 10 + [429] * 20
    admin = sign_in(app, an_admin, fresh_address())
    assert admin.post(PASS, json={"code": v.codes[0]}).status_code == 200


# fence: with the key gone, a TOTP code is unavailable and counts nothing, and a recovery code still
# gets in. Kills a recovery path behind the key check, or a failure counted without the key.
# Injected by moving the key check before the code-format branch.
def test_no_key_still_lets_recovery_codes_in(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    v = verified_client(app, migrate_engine, an_admin)
    half = sign_in(app, an_admin)
    monkeypatch.delenv("ZIF_TOTP_KEY")

    refused = half.post(PASS, json={"code": totp.code(v.secret, v.t0 + 1)})

    assert answer(refused) == (503, {"code": "second_factor_unavailable"})
    assert failed_attempts(migrate_engine, an_admin) == 0
    assert half.post(PASS, json={"code": v.codes[0]}).status_code == 200
    assert half.get("/api/admin/session").json()["second_factor"] == "passed"


# fence: a key that can't open the secret is an operator mistake, never a wrong code. Kills
# InvalidTag treated as a failure (one wrong key would lock every admin). Injected by catching
# InvalidTag as a wrong secret.
def test_wrong_key_is_unavailable_not_a_failure(
    app: FastAPI,
    migrate_engine: Engine,
    an_admin: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    v = verified_client(app, migrate_engine, an_admin)
    half = sign_in(app, an_admin)
    monkeypatch.setenv("ZIF_TOTP_KEY", OTHER_KEY)

    refused = half.post(PASS, json={"code": totp.code(v.secret, v.t0 + 1)})

    assert answer(refused) == (503, {"code": "second_factor_unavailable"})
    assert failed_attempts(migrate_engine, an_admin) == 0
    assert "ERROR" in {line["level"] for line in log_lines()}


# fence: wrong passwords from one address don't lock the admin out of another. Kills ZIF-70's
# per-email key (anyone who knows the email locks the console). Injected by restoring
# email_key("admin_sign_in", credentials.email).
def test_sign_in_limit_is_per_email_and_address(app: FastAPI, an_admin: uuid.UUID) -> None:
    attacker = new_client(app, fresh_address())
    wrong = {"email": email_of(an_admin), "password": "wrong-password-123"}

    tries = [attacker.post("/api/admin/session", json=wrong).status_code for _ in range(6)]

    assert tries == [401] * 5 + [429]
    sign_in(app, an_admin, fresh_address())  # asserts 200


# fence: the secret and the recovery codes are never kept by a cache. Kills a missing no-store.
# Injected by deleting the header line on confirm.
def test_enrolment_answers_are_not_cached(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    v = verified_client(app, migrate_engine, an_admin)

    assert v.begun.headers["cache-control"] == "no-store"
    assert v.confirmed.headers["cache-control"] == "no-store"


# fence: the 10th wrong code answers locked, once, and a recovery code unlocks, audited. Kills a
# lock that answers 401 forever (the web app could never offer recovery), or no unlock record.
def test_lock_is_reported_and_audited(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    v = verified_client(app, migrate_engine, an_admin)
    half = sign_in(app, an_admin)
    bad = wrong_code(v)

    tries = [half.post(PASS, json={"code": bad}) for _ in range(10)]

    assert [t.status_code for t in tries] == [401] * 9 + [403]
    assert tries[-1].json() == {"code": "second_factor_locked"}
    actions = [action for action, _ in audit(migrate_engine, an_admin)]
    assert actions.count("second_factor_locked") == 1
    right = half.post(PASS, json={"code": totp.code(v.secret, v.t0 + 1)})
    assert answer(right) == (403, {"code": "second_factor_locked"})
    assert half.post(PASS, json={"code": v.codes[0]}).status_code == 200
    events = audit(migrate_engine, an_admin)
    assert ("recovery_code_used", {"codes_left": 9}) in events
    assert "second_factor_unlocked" in [action for action, _ in events]


# guard: the whole way, from a password-only session to a passed factor and back.
def test_enrol_and_sign_in_end_to_end(
    app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID
) -> None:
    first = sign_in(app, an_admin)
    assert first.get("/api/admin/session").json()["second_factor"] == "enrolment_required"

    v = verified_client(app, migrate_engine, an_admin)

    begun = v.begun.json()
    assert begun["qr"].startswith("data:image/svg+xml")
    assert base64.b32decode(begun["secret"]) == v.secret
    assert "issuer=ZiftBook" in begun["otpauth_uri"]
    assert len(set(v.codes)) == 10
    assert v.client.get("/api/admin/session").json()["second_factor"] == "passed"
    assert v.client.request("DELETE", "/api/admin/session", json={}).status_code == 204
    again = sign_in(app, an_admin)
    assert again.get("/api/admin/session").json()["second_factor"] == "required"
    passed = again.post(PASS, json={"code": totp.code(v.secret, v.t0 + 1)})
    assert passed.status_code == 200
    assert passed.headers["cache-control"] == "no-store"
    assert passed.json()["second_factor"] == "passed"
    actions = [action for action, _ in audit(migrate_engine, an_admin)]
    assert {"totp_enrolled", "second_factor_passed"} <= set(actions)


# guard: a wrong ticket is refused, audited, and begins nothing.
def test_wrong_ticket(app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID) -> None:
    issue_ticket(an_admin)
    client = sign_in(app, an_admin)

    refused = client.post(BEGIN, json={"ticket": totp.new_code()})

    assert answer(refused) == (401, {"code": "invalid_ticket"})
    assert factor(migrate_engine, an_admin)["secret_ct"] is None
    assert "totp_enrolment_failed" in [action for action, _ in audit(migrate_engine, an_admin)]


# guard: a pass after failures says how many, in the audit row and a WARNING.
def test_passed_after_failures_warns(
    app: FastAPI,
    migrate_engine: Engine,
    an_admin: uuid.UUID,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    v = verified_client(app, migrate_engine, an_admin)
    half = sign_in(app, an_admin)
    bad = wrong_code(v)
    assert [half.post(PASS, json={"code": bad}).status_code for _ in range(3)] == [401] * 3

    assert half.post(PASS, json={"code": totp.code(v.secret, v.t0 + 1)}).status_code == 200

    passed = [
        d for action, d in audit(migrate_engine, an_admin) if action == "second_factor_passed"
    ]
    assert passed[-1] == {"method": "totp", "failed_since_last": 3}
    warned = [line for line in log_lines() if line["msg"] == "second factor passed after failures"]
    assert [line["level"] for line in warned] == ["WARNING"]


# guard: who-am-I tells the web app what to show next.
def test_who_am_i_states(app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID) -> None:
    states = [sign_in(app, an_admin).get("/api/admin/session").json()["second_factor"]]
    v = verified_client(app, migrate_engine, an_admin)
    half = sign_in(app, an_admin)
    states.append(half.get("/api/admin/session").json()["second_factor"])
    assert half.post(PASS, json={"code": totp.code(v.secret, v.t0 + 1)}).status_code == 200
    states.append(half.get("/api/admin/session").json()["second_factor"])

    assert states == ["enrolment_required", "required", "passed"]


# fence: no typed code is a 500, and non-ASCII digits are never a TOTP attempt. Kills \d or isdigit
# routing Arabic-Indic digits into the TOTP branch (a counted failure, or compare_digest's
# TypeError on a non-ASCII str). Injected by swapping the pattern to \d{6}. The surrogatepass
# backstop is fenced directly in test_totp.py.
def test_code_input_never_500s(app: FastAPI, migrate_engine: Engine, an_admin: uuid.UUID) -> None:
    verified_client(app, migrate_engine, an_admin)
    half = sign_in(app, an_admin)

    arabic = half.post(PASS, json={"code": "٠١٢٣٤٥"})

    assert answer(arabic) == (401, {"code": "invalid_code"})
    assert failed_attempts(migrate_engine, an_admin) == 0
    for typed in ("\ud800", "x" * 41):
        # Encoded here: json.dumps escapes a lone surrogate the way a browser can send it.
        refused = half.post(
            PASS, content=json.dumps({"code": typed}), headers={"content-type": "application/json"}
        )
        assert answer(refused) == (422, {"code": "invalid_request"})
