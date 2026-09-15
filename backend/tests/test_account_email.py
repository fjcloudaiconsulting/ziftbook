import hashlib
import json
import os
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import mail
from app.db import SessionLocal
from app.jobs import Job
from app.mail import LOCALES, render
from tests.conftest import People

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"
APP_URL = "https://app.example.test"


@pytest.fixture(autouse=True)
def bound(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ZIF_APP_URL", APP_URL)
    configured = SessionLocal.kw.get("bind") is None  # the people fixture may have bound it
    if configured:
        SessionLocal.configure(bind=app_engine)
    yield
    if configured:
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
    for template, placeholder in (
        ("sign_up", "link"),
        ("password_reset", "link"),
        ("sign_up_registered", "sign_in"),
    ):
        for locale in LOCALES:
            subject, body = render(template, locale, {placeholder: "https://x.test/marker"})
            assert subject and "https://x.test/marker" in body and "$" not in body


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
    # The job holds the row's id, never the token.
    assert set(job.payload) == {"token_id"}


def test_a_reset_link_uses_the_users_own_language(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = f"{people.both}@example.com"  # both chose Dutch
    mail.send_token(request(app_engine, "password_reset", email, "pt"))

    (sent,) = inbox(email)
    body, _ = message(sent["ID"])
    page, token = link_in(body).split("#")
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
    email = f"{people.only_a}@example.com"

    mail.send_token(request(app_engine, "sign_up", email, "en"))

    (sent,) = inbox(email)
    body, _ = message(sent["ID"])
    assert sent["Subject"] == "You already have a ziftbook account"
    assert f"{APP_URL}/en/sign-in" in body and "#" not in body


def test_sending_again_replaces_the_link(app_engine: Engine, migrate_engine: Engine) -> None:
    email = f"{uuid.uuid4()}@example.com"
    job = request(app_engine, "sign_up", email, "en")

    mail.send_token(job)
    mail.send_token(job)

    tokens = [link_in(message(m["ID"])[0]).split("#")[1] for m in inbox(email)]
    assert len(set(tokens)) == 2
    stored = stored_hash(migrate_engine, email)
    assert sum(hashlib.sha256(t.encode()).digest() == stored for t in tokens) == 1


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
