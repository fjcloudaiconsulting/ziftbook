"""The never-log fence: real flows, at DEBUG, checked for personal data reaching a log line.

CONTRIBUTING "Logging": ids only, never a password, token or its hash, cookie, email address,
name, business or service name, IP address, user agent, request body, query string or header.
"""

import asyncio
import hashlib
import json
import smtplib
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from app import auth
from app.jobs import run_once
from app.mail import render
from app.main import create_app
from app.worker import KINDS
from tests.conftest import (
    People,
    add_password,
    email_of,
    fresh_email,
    mailed,
    member_id,
    new_client,
    put_settings,
    signed_in,
    token_in,
)

UA = {"User-Agent": "zif-never-log-agent/7"}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def client_for(app: FastAPI) -> Any:
    client = new_client(app)
    client.headers.update(UA)
    return client


def test_no_personal_data_ever_reaches_a_log(
    people: People,
    app: FastAPI,
    migrate_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    never: list[str] = ["2001:db8:", "@example.com", UA["User-Agent"]]
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
    secret(sign_up_email)
    secret(sign_up_password)
    secret(business_name)

    anon = client_for(app)
    signed_up = anon.post("/api/sign-up", json={"email": sign_up_email, "locale": "en"})
    assert signed_up.status_code == 202
    signup_token = token_in(mailed(sign_up_email))
    token_secret(signup_token)
    response = anon.post(
        "/api/sign-up/complete",
        content=json.dumps(
            {"token": signup_token, "password": sign_up_password, "business_name": business_name}
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
    assert put_settings(owner, {"timezone": timezone}).status_code == 200

    # 5: PATCH /api/members/{id} role change, as the owner.
    target = member_id(people.a, people.only_a)
    assert owner.patch(f"/api/members/{target}", json={"role": "owner"}).status_code == 200

    # 6: POST /api/services with a distinctive name.
    service_name = "Service Qwyx-4"
    secret(service_name)
    assert (
        owner.post(
            "/api/services",
            json={
                "name": {"en": service_name},
                "price": {"amount_minor": 1000},
                "duration_minutes": 15,
            },
        ).status_code
        == 201
    )

    # 7: invite flow — send, list, a wrong token, then accept with a brand-new account.
    invite_email = fresh_email()
    invite_password = "Invite-Pw7-55"
    secret(invite_email)
    secret(invite_password)

    assert owner.post("/api/invites", json={"email": invite_email}).status_code == 201
    assert owner.get("/api/invites").status_code == 200

    invite_token = token_in(mailed(invite_email))
    token_secret(invite_token)

    # Same shape, wrong digest: exercises the "no matching invite" path, not just the regex one.
    tenant_part, _, secret_part = invite_token.partition(".")
    flipped = "A" if secret_part[-1] != "A" else "B"
    wrong_token = f"{tenant_part}.{secret_part[:-1]}{flipped}"
    wrong_accept = client_for(app).post(
        "/api/invites/accept", json={"token": wrong_token, "password": invite_password}
    )
    assert wrong_accept.status_code == 400

    accepted = client_for(app).post(
        "/api/invites/accept", json={"token": invite_token, "password": invite_password}
    )
    assert accepted.status_code == 201
    cookie_secret(accepted)

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

    # 9: forced SMTP failure on a fresh sign-up link, run directly.
    fail_email = fresh_email()
    secret(fail_email)
    assert (
        client_for(app).post("/api/sign-up", json={"email": fail_email, "locale": "en"}).status_code
        == 202
    )

    def refuse(
        self: smtplib.SMTP, msg: Any, *, to_addrs: list[str] | None = None, **kwargs: Any
    ) -> None:
        to = (to_addrs or [msg["To"]])[0]
        raise smtplib.SMTPRecipientsRefused({to: (550, f"<{to}> unknown".encode())})

    monkeypatch.setattr(smtplib.SMTP, "send_message", refuse)
    try:
        asyncio.run(run_once(KINDS))
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

    lines = log_lines()
    assert any(line["msg"] == "access" for line in lines)
    unhandled = [line for line in lines if line["msg"] == "unhandled error"]
    assert len(unhandled) == 1
    failed = [line for line in lines if line["msg"] == "job failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "SMTPRecipientsRefused"
    sent = {line["template"] for line in lines if line["msg"] == "email sent"}
    assert {"sign_up", "sign_up_registered", "password_reset", "invite"} <= sent
    email_failed = [line for line in lines if line["msg"] == "email failed"]
    assert [(line["template"], line["error"]) for line in email_failed] == [
        ("sign_up", "SMTPRecipientsRefused")
    ]
