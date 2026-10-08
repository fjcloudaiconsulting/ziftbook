import asyncio
import base64
import io
import json
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
import requests
from mailgun.handlers.error_handler import ApiError
from sqlalchemy import Engine, text

from app import mail
from app.db import SessionLocal, tenant_context
from app.jobs import JobKind, enqueue, run_once
from app.mail import LOCALES, TEMPLATES, render
from app.worker import KINDS
from tests.conftest import MAILGUN_CALLS, People, save_setting, sent_to


@pytest.fixture
def clean_outbox(people: People) -> Iterator[None]:
    yield
    for tenant_id in (people.a, people.b):
        with tenant_context(tenant_id) as session:
            session.execute(text("DELETE FROM email_outbox"))


def send_hello(tenant_id: uuid.UUID, recipient_id: uuid.UUID) -> uuid.UUID:
    job_key = f"email.send:{tenant_id}:hello:{recipient_id}:{uuid.uuid4()}"
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.send",
            job_key,
            # "to" as older payloads carried it: ignored, the address comes from users.
            {"recipient_id": str(recipient_id), "template": "hello", "to": "old@example.com"},
            tenant_id=tenant_id,
        )

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())
    with SessionLocal.begin() as session:
        job_id: uuid.UUID = session.scalar(
            text("SELECT id FROM jobs WHERE dedupe_key = :k"), {"k": job_key}
        )
    return job_id


def subjects_sent_to(user_id: uuid.UUID) -> list[str]:
    return [message["Subject"] for message in sent_to(f"{user_id}@example.com")]


def test_every_template_exists_in_every_locale() -> None:
    for template in {path.name.split(".")[0] for path in TEMPLATES.glob("*.txt")}:
        for locale in LOCALES:
            subject, body = render(template, locale)
            assert subject and body.strip()


@pytest.mark.parametrize(
    ("recipient", "subject"),
    # nl; neither the person nor the business has a language
    [("both", "Hallo van ziftbook"), ("only_a", "Hello from ziftbook")],
)
def test_an_email_goes_to_the_recipients_address_in_their_language(
    people: People, clean_outbox: None, recipient: str, subject: str
) -> None:
    # The payload holds only the recipient's id: the address and language come from users.
    user_id = getattr(people, recipient)
    send_hello(people.a, user_id)

    assert subjects_sent_to(user_id) == [subject]
    with tenant_context(people.a) as session:
        row = session.execute(text("SELECT * FROM email_outbox")).mappings().one()
    assert (row["recipient_id"], row["template"], row["status"]) == (user_id, "hello", "sent")
    assert set(row) == {
        "id",
        "tenant_id",
        "job_id",
        "recipient_id",
        "template",
        "subject",
        "status",
        "sent_at",
    }


def test_no_email_goes_to_someone_outside_the_tenant(people: People, clean_outbox: None) -> None:
    job_id = send_hello(people.a, people.only_b)

    assert subjects_sent_to(people.only_b) == []
    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT count(*) FROM email_outbox")) == 0
    with SessionLocal.begin() as session:
        # Completed, not failed: retrying can't make them a member.
        completed = session.scalar(
            text("SELECT completed_at FROM jobs WHERE id = :id"), {"id": job_id}
        )
    assert completed is not None


def test_a_recipient_with_no_language_gets_the_business_languages_mail(
    people: People, clean_outbox: None
) -> None:
    save_setting(people.a, "language", "pt")

    send_hello(people.a, people.only_a)  # only_a has no language of their own
    send_hello(people.a, people.both)  # both chose nl themselves

    assert subjects_sent_to(people.only_a) == ["Olá do ziftbook"]
    assert subjects_sent_to(people.both) == ["Hallo van ziftbook"]


Lines = Callable[[], list[dict[str, Any]]]


def email_events(lines: Lines, job_id: uuid.UUID) -> list[dict[str, Any]]:
    # trace_id/span_id: asserted on their own in test_tracing.py's T10, not against a fixed value
    # here.
    return [
        {k: v for k, v in line.items() if k not in ("ts", "logger", "trace_id", "span_id")}
        for line in lines()
        if line["msg"].startswith("email") and line.get("job_id") == str(job_id)
    ]


# ZIF-95: a sent email logs its template and job, never the address, subject or body.
def test_a_sent_email_logs_its_template_and_job(
    people: People, clean_outbox: None, log_lines: Lines
) -> None:
    job_id = send_hello(people.a, people.only_a)

    assert email_events(log_lines, job_id) == [
        {
            "level": "INFO",
            "msg": "email sent",
            "job_id": str(job_id),
            "job_kind": "email.send",
            "tenant_id": str(people.a),
            "template": "hello",
        }
    ]


# ZIF-95: a failed send logs its template, the error's class and Mailgun's status, and still fails
# the job, so it is retried. Its own kind: a refused send must not fail anyone else's queued email.
# ZIF-151 fence: one POST only (the SDK's default retry policy would make four), and the 503 raises
# (without raise_for_status the job would complete with nothing sent).
def test_a_failed_email_logs_the_error_class_and_the_job_retries(
    people: People,
    clean_outbox: None,
    app_engine: Engine,
    log_lines: Lines,
    mailgun_replies: dict[str, int | Exception],
) -> None:
    mailgun_replies[f"{people.only_a}@example.com"] = 503
    calls = len(MAILGUN_CALLS)
    kind = f"test.mail.{uuid.uuid4().hex}"
    with SessionLocal.begin() as session:
        enqueue(
            session,
            kind,
            f"{kind}:1",
            {"recipient_id": str(people.only_a), "template": "hello"},
            tenant_id=people.a,
        )
    try:
        asyncio.run(run_once({kind: JobKind(mail.send, 30, timedelta(hours=1))}))
        with SessionLocal() as session:
            job_id, completed = session.execute(
                text("SELECT id, completed_at FROM jobs WHERE kind = :kind"), {"kind": kind}
            ).one()

        assert email_events(log_lines, job_id) == [
            {
                "level": "WARNING",
                "msg": "email failed",
                "job_id": str(job_id),
                "job_kind": kind,
                "tenant_id": str(people.a),
                "template": "hello",
                "error": "HTTPError",
                "status": 503,
            }
        ]
        assert [c["Status"] for c in MAILGUN_CALLS[calls:]] == [503]
        assert completed is None
        assert [line["error"] for line in log_lines() if line["msg"] == "job failed"] == [
            "HTTPError"
        ]
        assert subjects_sent_to(people.only_a) == []
    finally:
        with app_engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE kind = :kind"), {"kind": kind})


# ZIF-151 fence: the request the SDK sends. Kills the messages route (the calendar part would be
# retyped by Mailgun), the US host for an EU account, a dropped domain, the SDK's 60s default
# timeout, the click-tracking option going missing (links carry secrets), and a From off the
# sending domain (fails DMARC alignment).
def test_mail_goes_to_mailguns_eu_mime_route_with_the_message_as_built() -> None:
    address = f"{uuid.uuid4()}@example.com"

    mail.deliver("hello", address, "Subject", "Body\n")

    (sent,) = sent_to(address)
    assert sent["URL"] == "https://api.eu.mailgun.net/v3/test.ziftbook.invalid/messages.mime"
    assert sent["Auth"] == ("api", "test-key")
    assert sent["Timeout"] == (10.0, 10.0)
    assert sent["Form"] == {"to": address, "o:tracking-clicks": "no"}
    assert sent["Headers"]["From"] == ["ziftbook <no-reply@test.ziftbook.invalid>"]
    assert sent["Headers"]["To"] == [address]
    assert (sent["Subject"], sent["Text"]) == ("Subject", "Body\n")


def test_a_us_account_uses_mailguns_us_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_MAILGUN_REGION", "us")
    address = f"{uuid.uuid4()}@example.com"

    mail.deliver("hello", address, "Subject", "Body\n")

    (sent,) = sent_to(address)
    assert sent["URL"] == "https://api.mailgun.net/v3/test.ziftbook.invalid/messages.mime"


# ZIF-151 fence: no key (staging before the owner adds it) fails the send, so the job keeps its
# email for the retry. Kills logging and returning as if sent, which would mark the outbox 'sent'.
@pytest.mark.parametrize("unset", ["ZIF_MAILGUN_API_KEY", "ZIF_MAILGUN_DOMAIN"])
def test_with_no_mail_transport_the_send_fails(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    monkeypatch.delenv(unset)
    address = f"{uuid.uuid4()}@example.com"

    with pytest.raises(mail.MailNotConfigured):
        mail.deliver("hello", address, "Subject", "Body\n")

    assert [c for c in MAILGUN_CALLS if c["To"] == address] == []


# ZIF-151 fence: only a 200 means queued. Kills raise_for_status alone, which lets a redirect (the
# SDK never follows one) through as sent.
def test_a_redirect_is_not_a_sent_email(mailgun_replies: dict[str, int | Exception]) -> None:
    address = f"{uuid.uuid4()}@example.com"
    mailgun_replies[address] = 302

    with pytest.raises(requests.HTTPError):
        mail.deliver("hello", address, "Subject", "Body\n")


# ZIF-151 fence: a dropped connection fails the send too (the SDK wraps it in its ApiError), so the
# job keeps its email. Kills catching the SDK's errors as if sent.
def test_a_dropped_connection_is_not_a_sent_email(
    mailgun_replies: dict[str, int | Exception],
) -> None:
    address = f"{uuid.uuid4()}@example.com"
    mailgun_replies[address] = requests.ConnectionError()

    with pytest.raises(ApiError):
        mail.deliver("hello", address, "Subject", "Body\n")

    assert sent_to(address) == []


# ZIF-151 fence: development sends through Mailpit's HTTP API (never SMTP), and only when there is
# no Mailgun key. Kills a wrong payload, and Mailpit winning over a deployment's key.
def test_development_mail_goes_to_mailpits_http_api(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def capture(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        assert timeout == 10
        requests.append(request)
        return io.BytesIO(b'{"ID": "x"}')

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    monkeypatch.setenv("ZIF_MAILPIT_URL", "http://mailpit.test:8025/")
    address = f"{uuid.uuid4()}@example.com"

    mail.deliver("hello", address, "Subject", "Body\n")  # the key is still set: Mailgun
    assert requests == [] and len(sent_to(address)) == 1

    monkeypatch.delenv("ZIF_MAILGUN_API_KEY")
    mail.deliver("hello", address, "Subject", "Body\n", ics=b"BEGIN:VCALENDAR\r\n")

    (request,) = requests
    assert (request.full_url, request.get_method()) == (
        "http://mailpit.test:8025/api/v1/send",
        "POST",
    )
    assert request.get_header("Content-type") == "application/json"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {
        "From": {"Email": "no-reply@test.ziftbook.invalid", "Name": "ziftbook"},
        "To": [{"Email": address}],
        "Subject": "Subject",
        "Text": "Body\n",
        "Attachments": [
            {
                "Content": base64.b64encode(b"BEGIN:VCALENDAR\r\n").decode(),
                "Filename": "booking.ics",
                "ContentType": "text/calendar; method=PUBLISH; charset=utf-8",
            }
        ],
    }
