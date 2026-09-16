import asyncio
import json
import os
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.db import SessionLocal, tenant_context
from app.jobs import enqueue, run_once
from app.mail import LOCALES, TEMPLATES, render
from app.worker import KINDS
from tests.conftest import People, save_setting

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"


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
    query = urllib.parse.urlencode({"query": f"to:{user_id}@example.com"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as response:
        return [message["Subject"] for message in json.load(response)["messages"]]


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
