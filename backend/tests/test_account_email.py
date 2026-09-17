import asyncio
import hashlib
import json
import os
import smtplib
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import mail
from app.db import SessionLocal
from app.jobs import Job, enqueue, run_once
from app.mail import LOCALES, render
from app.worker import KINDS
from tests.conftest import People

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"
APP_URL = "https://app.example.test"


@pytest.fixture(autouse=True)
def bound(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # A trailing slash, as someone might configure it: links must not get a double slash.
    monkeypatch.setenv("ZIF_APP_URL", f"{APP_URL}/")
    SessionLocal.configure(bind=app_engine)
    yield
    SessionLocal.configure(bind=None)


def request(app_engine: Engine, purpose: str, email: str, locale: str) -> Job:
    """What the endpoint does: a pending token row and the job that mails it."""
    with app_engine.begin() as conn:
        token_id = conn.scalar(
            text("SELECT start_email_token(:p, :e, :l)"), {"p": purpose, "e": email, "l": locale}
        )
    return Job(uuid.uuid4(), "email.token", None, {"token_id": str(token_id)})


def inbox(email: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"query": f"to:{email}"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as response:
        messages: list[dict[str, Any]] = json.load(response)["messages"]
    return messages


def message(message_id: str) -> tuple[str, dict[str, list[str]]]:
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{message_id}", timeout=5) as response:
        body = json.load(response)["Text"]
    with urllib.request.urlopen(
        f"{MAILPIT}/api/v1/message/{message_id}/headers", timeout=5
    ) as response:
        headers: dict[str, list[str]] = json.load(response)
    return body, headers


def stored_hash(migrate_engine: Engine, email: str) -> bytes | None:
    with migrate_engine.connect() as conn:
        value: bytes | None = conn.scalar(
            text("SELECT token_hash FROM email_tokens WHERE email = :e"), {"e": email}
        )
    return value


def link_in(body: str) -> str:
    (link,) = [word for word in body.split() if word.startswith(APP_URL)]
    return link


def test_every_account_template_puts_its_link_in_every_locale() -> None:
    for template, values in (
        ("sign_up", {"link": "https://x.test/marker"}),
        ("password_reset", {"link": "https://x.test/marker"}),
        ("sign_up_registered", {"sign_in": "https://x.test/marker"}),
        ("invite", {"link": "https://x.test/marker", "business": "Studio Ana"}),
    ):
        for locale in LOCALES:
            subject, body = render(template, locale, values)
            assert subject and "$" not in body
            for value in values.values():
                assert value in body


def test_a_sign_up_link_carries_its_token_only_in_the_fragment(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    email = f"{uuid.uuid4()}@example.com"
    job = request(app_engine, "sign_up", email, "pt")

    mail.send_token(job)

    (sent,) = inbox(email)
    body, headers = message(sent["ID"])
    link = link_in(body)
    page, token = link.split("#")
    assert page == f"{APP_URL}/pt/sign-up/complete" and "?" not in link
    assert sent["Subject"] == "Configure seu negócio no ziftbook"
    assert hashlib.sha256(token.encode()).digest() == stored_hash(migrate_engine, email)
    assert headers["X-Mailgun-Track-Clicks"] == ["no"]


def test_a_reset_link_uses_the_users_own_language(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = f"{people.both}@example.com"  # both chose Dutch
    mail.send_token(request(app_engine, "password_reset", email, "pt"))

    (sent,) = inbox(email)
    body, headers = message(sent["ID"])
    page, token = link_in(body).split("#")
    assert headers["X-Mailgun-Track-Clicks"] == ["no"]
    assert sent["Subject"] == "Stel je ziftbook-wachtwoord opnieuw in"
    assert page == f"{APP_URL}/nl/reset-password"
    assert hashlib.sha256(token.encode()).digest() == stored_hash(migrate_engine, email)


def test_a_reset_for_an_unknown_email_sends_nothing(
    app_engine: Engine, migrate_engine: Engine
) -> None:
    email = f"{uuid.uuid4()}@example.com"

    mail.send_token(request(app_engine, "password_reset", email, "en"))

    assert inbox(email) == []
    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(text("SELECT count(*) FROM email_tokens WHERE email = :e"), {"e": email})
            == 0
        )


def test_a_sign_up_for_a_registered_email_says_to_sign_in_without_a_link(
    people: People, app_engine: Engine
) -> None:
    email = (
        f"{people.both}@example.com"  # both chose Dutch; the request came from a Portuguese page
    )

    mail.send_token(request(app_engine, "sign_up", email, "pt"))

    (sent,) = inbox(email)
    body, _ = message(sent["ID"])
    assert sent["Subject"] == "Je hebt al een ziftbook-account"
    assert f"{APP_URL}/nl/sign-in" in body and "#" not in body


def test_sending_again_replaces_the_link(app_engine: Engine, migrate_engine: Engine) -> None:
    email = f"{uuid.uuid4()}@example.com"
    job = request(app_engine, "sign_up", email, "en")

    mail.send_token(job)
    (first,) = inbox(email)
    mail.send_token(job)
    (second,) = [m for m in inbox(email) if m["ID"] != first["ID"]]

    token = link_in(message(second["ID"])[0]).split("#")[1]
    assert token != link_in(message(first["ID"])[0]).split("#")[1]
    assert hashlib.sha256(token.encode()).digest() == stored_hash(migrate_engine, email)


def test_a_failed_send_keeps_the_request_for_the_retry(
    people: People, app_engine: Engine, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A registered sign-up deletes its row when minted; if the email never left, the retry needs it.
    email = f"{people.only_b}@example.com"
    job = request(app_engine, "sign_up", email, "en")

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("SMTP unavailable")

    monkeypatch.setattr(mail, "deliver", broken)
    with pytest.raises(OSError):
        mail.send_token(job)

    with migrate_engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT count(*) FROM email_tokens WHERE id = :id"),
                {"id": job.payload["token_id"]},
            )
            == 1
        )


def test_the_worker_runs_account_email_jobs(app_engine: Engine) -> None:
    # What the endpoints will do: enqueue by kind name, for the worker's registered handler.
    email = f"{uuid.uuid4()}@example.com"
    token_id = request(app_engine, "sign_up", email, "en").payload["token_id"]
    with SessionLocal.begin() as session:
        enqueue(session, "email.token", f"email.token:{token_id}", {"token_id": token_id})

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())

    assert [m["Subject"] for m in inbox(email)] == ["Set up your business on ziftbook"]


def test_an_address_that_reads_as_a_list_reaches_no_second_inbox(app_engine: Engine) -> None:
    victim = f"{uuid.uuid4()}@example.com"
    stored = f"{uuid.uuid4()}@example.com, {victim}"

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        mail.send_token(request(app_engine, "sign_up", stored, "en"))

    assert inbox(victim) == []
