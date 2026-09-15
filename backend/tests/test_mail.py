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
from tests.conftest import People

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"


@pytest.fixture
def outbox(people: People) -> Iterator[People]:
    yield people
    for tenant_id in (people.a, people.b):
        with tenant_context(tenant_id) as session:
            session.execute(text("DELETE FROM email_outbox"))


def send_hello(tenant_id: uuid.UUID, recipient_id: uuid.UUID) -> None:
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.send",
            f"email.send:{tenant_id}:hello:{recipient_id}:{uuid.uuid4()}",
            {"recipient_id": str(recipient_id), "template": "hello"},
            tenant_id=tenant_id,
        )

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())


def subjects_sent_to(user_id: uuid.UUID) -> list[str]:
    query = urllib.parse.urlencode({"query": f"to:{user_id}@example.com"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as response:
        return [message["Subject"] for message in json.load(response)["messages"]]


def test_every_template_exists_in_every_locale() -> None:
    for template in {path.name.split(".")[0] for path in TEMPLATES.glob("*.txt")}:
        for locale in LOCALES:
            subject, body = render(template, locale)
            assert subject and body.strip()


def test_an_email_goes_to_the_recipients_address_in_their_language(outbox: People) -> None:
    # The payload holds only the recipient's id: the address and language come from users.
    send_hello(outbox.a, outbox.both)

    assert subjects_sent_to(outbox.both) == ["Hallo van ziftbook"]
    with tenant_context(outbox.a) as session:
        row = session.execute(text("SELECT * FROM email_outbox")).mappings().one()
    assert (row["recipient_id"], row["template"], row["status"]) == (outbox.both, "hello", "sent")
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


def test_no_email_goes_to_someone_outside_the_tenant(outbox: People) -> None:
    send_hello(outbox.a, outbox.only_b)

    assert subjects_sent_to(outbox.only_b) == []
    with tenant_context(outbox.a) as session:
        assert session.scalar(text("SELECT count(*) FROM email_outbox")) == 0
