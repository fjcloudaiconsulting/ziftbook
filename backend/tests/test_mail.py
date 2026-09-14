import asyncio
import json
import os
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.db import SessionLocal, tenant_context
from app.jobs import enqueue, run_once
from app.mail import LOCALES, TEMPLATES, render
from app.worker import KINDS

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"


@pytest.fixture
def tenant(migrated: None, app_engine: Engine) -> Iterator[uuid.UUID]:
    engine = create_engine(os.environ["ZIF_DATABASE_URL"], poolclass=NullPool)
    SessionLocal.configure(bind=engine)
    with app_engine.begin() as conn:
        tenant_id = conn.scalar(text("INSERT INTO tenants (name) VALUES ('Mail') RETURNING id"))
    yield tenant_id
    with tenant_context(tenant_id) as session:
        session.execute(text("DELETE FROM email_outbox"))
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})  # jobs cascade
    SessionLocal.configure(bind=None)
    engine.dispose()


def test_every_template_exists_in_every_locale() -> None:
    for template in {path.name.split(".")[0] for path in TEMPLATES.glob("*.txt")}:
        for locale in LOCALES:
            subject, body = render(template, locale)
            assert subject and body.strip()


def test_an_enqueued_email_arrives_and_the_outbox_keeps_no_body(tenant: uuid.UUID) -> None:
    to = f"{uuid.uuid4().hex}@example.com"
    recipient_id = uuid.uuid4()
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.send",
            f"email.send:{tenant}:{to}",
            {"recipient_id": str(recipient_id), "to": to, "template": "hello", "locale": "nl"},
            tenant_id=tenant,
        )

    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())

    query = urllib.parse.urlencode({"query": f"to:{to}"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as response:
        messages = json.load(response)["messages"]
    assert [m["Subject"] for m in messages] == ["Hallo van ziftbook"]

    with tenant_context(tenant) as session:
        row = session.execute(text("SELECT * FROM email_outbox")).mappings().one()
    assert (row["recipient_id"], row["template"], row["status"]) == (recipient_id, "hello", "sent")
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
