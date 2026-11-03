import asyncio
import base64
import io
import json
import string
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from mailgun.handlers.error_handler import ApiError
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from sqlalchemy import Engine, text

from app import mail
from app.db import SessionLocal, tenant_context
from app.jobs import JobKind, enqueue, run_once
from app.mail import LOCALES, TEMPLATES, render
from app.worker import KINDS
from tests.conftest import (
    MAILGUN_CALLS,
    People,
    closed,
    end_membership,
    jobs_as_the_worker,
    migrate_tenant_context,
    points,
    save_setting,
    sent_to,
)


@pytest.fixture
def clean_outbox(people: People, migrate_engine: Engine) -> Iterator[None]:
    yield
    # As the migrate role: the app role has no DELETE on email_outbox (ZB-10).
    for tenant_id in (people.a, people.b):
        with migrate_tenant_context(migrate_engine, tenant_id) as conn:
            conn.execute(text("DELETE FROM email_outbox"))


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

    with jobs_as_the_worker():  # as the worker runs jobs (ZB-08)
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


# ZIF-112 fence: a removed member's row is kept, so users alone no longer says "in this tenant".
# Kills: the lookup without the live-membership join (a removed member is emailed). Positive
# control: a live member of the same business is.
def test_no_email_goes_to_a_removed_member(people: People, clean_outbox: None) -> None:
    end_membership(people.a, people.only_a)
    calls = len(MAILGUN_CALLS)

    send_hello(people.a, people.only_a)
    assert len(MAILGUN_CALLS) == calls
    assert subjects_sent_to(people.only_a) == []
    with tenant_context(people.a) as session:
        assert session.scalar(text("SELECT count(*) FROM email_outbox")) == 0

    send_hello(people.a, people.both)
    assert len(MAILGUN_CALLS) == calls + 1
    assert len(subjects_sent_to(people.both)) == 1


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
# timeout, the click-tracking option going missing (links carry secrets), the open-tracking option
# going missing (ZIF-175: a pixel in the HTML part would report every open), and a From off the
# sending domain (fails DMARC alignment).
def test_mail_goes_to_mailguns_eu_mime_route_with_the_message_as_built() -> None:
    address = f"{uuid.uuid4()}@example.com"

    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>")

    (sent,) = sent_to(address)
    assert sent["URL"] == "https://api.eu.mailgun.net/v3/test.ziftbook.invalid/messages.mime"
    assert sent["Auth"] == ("api", "test-key")
    assert sent["Timeout"] == (10.0, 10.0)
    assert sent["Form"] == {"to": address, "o:tracking-clicks": "no", "o:tracking-opens": "no"}
    assert sent["Headers"]["From"] == ["ziftbook <no-reply@test.ziftbook.invalid>"]
    assert sent["Headers"]["To"] == [address]
    assert (sent["Subject"], sent["Text"], sent["Html"]) == ("Subject", "Body\n", "<p>Body</p>\n")


def test_a_us_account_uses_mailguns_us_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_MAILGUN_REGION", "us")
    address = f"{uuid.uuid4()}@example.com"

    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>")

    (sent,) = sent_to(address)
    assert sent["URL"] == "https://api.mailgun.net/v3/test.ziftbook.invalid/messages.mime"


# ZIF-151 fence: no key (staging before the owner adds it) fails the send, so the job keeps its
# email for the retry. Kills logging and returning as if sent, which would mark the outbox 'sent'.
@pytest.mark.parametrize("unset", ["ZIF_MAILGUN_API_KEY", "ZIF_MAILGUN_DOMAIN"])
def test_with_no_mail_transport_the_send_fails(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    monkeypatch.delenv(unset)
    address = f"{uuid.uuid4()}@example.com"

    with pytest.raises(mail.MailNotConfigured):
        mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>")

    assert [c for c in MAILGUN_CALLS if c["To"] == address] == []


# ZIF-151 fence: only a 200 means queued. Kills raise_for_status alone, which lets a redirect (the
# SDK never follows one) through as sent.
def test_a_redirect_is_not_a_sent_email(mailgun_replies: dict[str, int | Exception]) -> None:
    address = f"{uuid.uuid4()}@example.com"
    mailgun_replies[address] = 302

    with pytest.raises(requests.HTTPError):
        mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>")


# ZIF-151 fence: a dropped connection fails the send too (the SDK wraps it in its ApiError), so the
# job keeps its email. Kills catching the SDK's errors as if sent.
def test_a_dropped_connection_is_not_a_sent_email(
    mailgun_replies: dict[str, int | Exception],
) -> None:
    address = f"{uuid.uuid4()}@example.com"
    mailgun_replies[address] = requests.ConnectionError()

    with pytest.raises(ApiError):
        mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>")

    assert sent_to(address) == []


# ZIF-99 F7 fence: Reply-To is normalised like `to`, so a stored value written around the API cannot
# become a list. Kills no normalise in _send, and the Mailpit path ignoring reply_to.
def test_a_reply_to_is_normalised_and_a_list_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    address = f"{uuid.uuid4()}@example.com"

    with pytest.raises(ValueError, match="not an email address"):
        mail.deliver(
            "hello", address, "Subject", "Body\n", "<p>Body</p>", reply_to="a@x.com, victim@y.com"
        )
    assert sent_to(address) == []

    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>", reply_to=" Shop@X.com ")
    (sent,) = sent_to(address)
    assert sent["Headers"]["Reply-To"] == ["shop@x.com"]

    requests: list[urllib.request.Request] = []

    def capture(request: urllib.request.Request, timeout: float) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(b'{"ID": "x"}')

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    monkeypatch.setenv("ZIF_MAILPIT_URL", "http://mailpit.test:8025/")
    monkeypatch.delenv("ZIF_MAILGUN_API_KEY")
    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>", reply_to=" Shop@X.com ")
    (request,) = requests
    assert isinstance(request.data, bytes)
    assert json.loads(request.data)["ReplyTo"] == [{"Email": "shop@x.com"}]


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

    mail.deliver(
        "hello", address, "Subject", "Body\n", "<p>Body</p>"
    )  # the key is still set: Mailgun
    assert requests == [] and len(sent_to(address)) == 1

    monkeypatch.delenv("ZIF_MAILGUN_API_KEY")
    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>", ics=b"BEGIN:VCALENDAR\r\n")

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
        "HTML": "<p>Body</p>",
        "Attachments": [
            {
                "Content": base64.b64encode(b"BEGIN:VCALENDAR\r\n").decode(),
                "Filename": "booking.ics",
                "ContentType": "text/calendar; method=PUBLISH; charset=utf-8",
            }
        ],
    }


# M5 fence: a delivery counts once, as sent or as failed (the error still raises).
# Kills: counting before _send (a failure counted as sent), a failure not counted.
def test_a_delivery_counts_as_sent_or_failed_by_template(
    metric_reader: InMemoryMetricReader, mailgun_replies: dict[str, int | Exception]
) -> None:
    sent, refused = f"{uuid.uuid4()}@example.com", f"{uuid.uuid4()}@example.com"
    mailgun_replies[sent] = 200
    mailgun_replies[refused] = 503

    mail.deliver("hello", sent, "Subject", "Body\n", "<p>Body</p>")
    with pytest.raises(requests.HTTPError):
        mail.deliver("hello", refused, "Subject", "Body\n", "<p>Body</p>")

    assert {
        (p.attributes["email.template"], p.attributes["email.outcome"]): p.value
        for p in points(metric_reader, "ziftbook.email.deliveries")
    } == {("hello", "sent"): 1, ("hello", "failed"): 1}
    closed(metric_reader)


def send_template(tenant_id: uuid.UUID, recipient_id: uuid.UUID, template: str) -> None:
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.send",
            f"email.send:{tenant_id}:{template}:{recipient_id}:{uuid.uuid4()}",
            {"recipient_id": str(recipient_id), "template": template},
            tenant_id=tenant_id,
        )

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    with jobs_as_the_worker():
        asyncio.run(run_until_idle())


# ZIF-232 S2 FENCE: the owners' notice that the contact email in effect changed names the business
# (an owner may own several) in the body only, and links to Settings in the owner's language. Kills
# a template whose $business or $link email.send leaves unfilled (the job fails), and an address
# in it (the notice reaches every owner, the new address need not be theirs to read).
def test_the_owners_hear_when_the_contact_email_changes(
    people: People, clean_outbox: None, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example.test")
    name = "Studio $link Zzyzx"
    with migrate_engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET name = :n WHERE id = :t"), {"n": name, "t": people.a})

    send_template(people.a, people.both, "contact_email_changed")

    (sent,) = sent_to(f"{people.both}@example.com")
    assert sent["Subject"] == "Het contactadres voor je klanten is gewijzigd"  # both chose Dutch
    assert f"„{name}”" in sent["Text"].splitlines()
    assert "https://app.example.test/nl/settings" in sent["Text"].split()
    assert name not in sent["Subject"] and "@" not in sent["Text"]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("template", ["contact_email_changed", "contact_email_confirm"])
def test_the_contact_email_templates_have_no_em_dash(template: str, locale: str) -> None:
    assert "—" not in (TEMPLATES / f"{template}.{locale}.txt").read_text()


def _every(template: str, locale: str, **values: str) -> dict[str, str]:
    """Every placeholder the template names, as a value that says which one it is."""
    names = string.Template((TEMPLATES / f"{template}.{locale}.txt").read_text()).get_identifiers()
    link = "https://app.test/x#t"
    return {n: link if n in ("link", "sign_in") else f"{n.upper()}-VALUE" for n in names} | values


def _parts(raw: bytes) -> list[str]:
    """The content types of a message, depth first."""
    from email import message_from_bytes, policy

    return [
        part.get_content_type() for part in message_from_bytes(raw, policy=policy.default).walk()
    ]


# ZIF-175 guard. Kills: a template that renders only a text part, or HTML that leaves a placeholder.
def test_every_template_renders_a_text_and_an_html_part_in_every_locale() -> None:
    for template in {path.name.split(".")[0] for path in TEMPLATES.glob("*.txt")}:
        for locale in LOCALES:
            subject, body, html = mail.compose(template, locale, _every(template, locale))
            assert subject and body.strip(), (template, locale)
            assert html.startswith("<!doctype html>"), (template, locale)
            assert f'<html lang="{locale}"' in html, (template, locale)
            assert "$" not in body and "$" not in html, (template, locale)


HOSTILE = '<script>alert("x")</script> O\'Brien & Co'


# ZIF-175 fence: every value is untrusted in HTML (client names, messages and business names come
# from strangers). Kills: a value substituted unescaped, a value parsed as markup (a button or a
# heading from a client's message), and the text part losing the value as typed.
def test_values_are_escaped_in_html_and_kept_as_typed_in_text() -> None:
    hostile = {
        "business": HOSTILE,
        "client": HOSTILE,
        "service": HOSTILE,
        "message": f"{HOSTILE}\n[Pay here](https://evil.test)\n# Heading",
    }
    for template in ("booking_new", "booking_declined_message", "invite"):
        values = {k: v for k, v in hostile.items() if k in _every(template, "en")}
        subject, body, html = mail.compose(template, "en", _every(template, "en", **values))
        assert "<script>" not in html and 'href="https://evil.test"' not in html, template
        assert html.count("<h1") == 1, template  # the message's "# Heading" stays text
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; O&#x27;Brien &amp; Co" in html
        assert HOSTILE in body, template


# ZIF-175 fence. Kills: a button whose link is not a web address (javascript: in an href).
def test_a_button_link_must_be_a_web_address() -> None:
    with pytest.raises(ValueError, match="link"):
        mail.compose("sign_up", "en", {"link": "javascript:alert(1)"})


# ZIF-175 fence: the HTML is an alternative to the text, and the calendar file stays an attachment
# beside both. Kills: HTML replacing the text part, HTML first (text-only clients then show the
# markup), and the .ics inside the alternative (clients would show it instead of the body).
def test_the_message_is_text_and_html_alternatives_with_the_calendar_beside_them() -> None:
    address = f"{uuid.uuid4()}@example.com"

    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>")
    mail.deliver("hello", address, "Subject", "Body\n", "<p>Body</p>", ics=b"BEGIN:VCALENDAR\r\n")

    with_ics, plain = (call["Raw"].encode() for call in sent_to(address))  # sent_to: newest first
    assert _parts(plain) == ["multipart/alternative", "text/plain", "text/html"]
    assert _parts(with_ics) == [
        "multipart/mixed",
        "multipart/alternative",
        "text/plain",
        "text/html",
        "text/calendar",
    ]


# ZIF-175 guard: no dash punctuation the copy style rules out (em or en dashes), in any template
# or in the layout's own words.
def test_no_template_or_layout_word_has_a_dash() -> None:
    for path in TEMPLATES.glob("*.txt"):
        assert not {"\N{EM DASH}", "\N{EN DASH}"} & set(path.read_text()), path.name
    for words in (mail.FOOTER, mail.LINK_FALLBACK):
        assert not {"\N{EM DASH}", "\N{EN DASH}"} & set("".join(words.values()))


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("Hello,\n\nA paragraph.", "one heading"),
        ("# One\n\n# Two", "one heading"),
        ("# One\n\n[A]($link)\n\n[B]($link)", "one button"),
    ],
)
def test_a_template_needs_one_heading_and_at_most_one_button(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, error: str
) -> None:
    # ZIF-175 fence: the HTML shows one heading and one button, so a file with another count would
    # drop words the text part keeps. Kills _parse accepting zero or two headings, or two buttons.
    (tmp_path / "odd.en.txt").write_text(f"Subject\n{body}\n")
    monkeypatch.setattr(mail, "TEMPLATES", tmp_path)
    with pytest.raises(ValueError, match=error):
        mail.render("odd", "en")


# ZIF-175 fence (security review S1): no heading carries a value. A heading is the platform's own
# voice, right under its mark, and mail clients turn a domain inside it into a link; a business
# name is chosen by whoever signs up. Kills a $placeholder in any template's heading.
def test_no_heading_carries_a_value() -> None:
    for path in TEMPLATES.glob("*.txt"):
        template, locale, _ = path.name.split(".")
        headings = [parts for kind, parts in mail._parse(template, locale)[1] if kind == "heading"]
        assert not any("$" in line for parts in headings for line in parts), path.name


# ZIF-175 fence: only a booking email names the business above its heading; its reader already
# deals with that business. An invite goes to anyone a stranger names, so the name stays in its
# sentence. Kills the eyebrow on the invite.
def test_only_booking_emails_name_the_business_above_the_heading() -> None:
    name = "ziftbook Security: verify at ziftbook-help.example"
    invite = mail.compose("invite", "en", {"business": name, "link": "https://app.test/x"})[2]
    flood = mail.compose("booking_flood", "en", {"business": name, "link": "https://app.test/x"})[2]
    eyebrow = f'letter-spacing:.02em">{name}</p>'
    assert name in invite and eyebrow not in invite
    assert eyebrow in flood
