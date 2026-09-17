import asyncio
import hashlib
import uuid
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from app import mail
from app.db import SessionLocal
from app.jobs import Job, enqueue, run_once
from app.worker import KINDS
from tests.conftest import People, fresh_email, save_setting
from tests.test_account_email import APP_URL, bound, inbox, link_in, message  # noqa: F401
from tests.test_invites_db import pending


def run_jobs() -> None:
    async def loop() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(loop())


def invite_row(
    migrate_engine: Engine, tenant_id: uuid.UUID, invite_id: uuid.UUID, select: str
) -> Any:
    """One invite's columns, read as migrate: FORCE row-level security needs a tenant set first."""
    with migrate_engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        return conn.execute(
            text(f"SELECT {select} FROM invites WHERE id = :i"), {"i": invite_id}
        ).one()


# The job mints a token, mails its link with no query string, resets the expiry, and
# tags the message so Mailgun doesn't rewrite the link.
def test_send_invite_mints_and_mails_a_link(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    invite_id = pending(people.a, email)

    mail.send_invite(Job(uuid4(), "email.invite", people.a, {"invite_id": str(invite_id)}))

    (sent,) = inbox(email)
    body, headers = message(sent["ID"])
    link = link_in(body)
    page, fragment = link.split("#")
    assert page == f"{APP_URL}/en/invite" and "?" not in link
    tenant, _, secret = fragment.partition(".")
    assert tenant == str(people.a)
    assert len(secret) >= 43  # 32 random bytes, so never a UUID
    token_hash, lifetime = invite_row(
        migrate_engine, people.a, invite_id, "token_hash, expires_at - now()"
    )
    assert token_hash == hashlib.sha256(secret.encode()).digest()
    assert timedelta(days=6, hours=23, minutes=59) < lifetime <= timedelta(days=7)
    assert headers["X-Mailgun-Track-Clicks"] == ["no"]
    assert sent["Subject"] == "You're invited to join a business on ziftbook"


# The link, page and subject follow the business's language, not the recipient's (a
# fresh email has none).
@pytest.mark.parametrize(
    "language,page,subject",
    [
        ("pt", "pt", "Você recebeu um convite para um negócio no ziftbook"),
        ("nl", "nl", "Je bent uitgenodigd voor een bedrijf op ziftbook"),
        (None, "en", "You're invited to join a business on ziftbook"),
    ],
)
def test_send_invite_uses_the_business_language(
    people: People, app_engine: Engine, language: str | None, page: str, subject: str
) -> None:
    if language is not None:
        save_setting(people.a, "language", language)
    email = fresh_email()
    invite_id = pending(people.a, email)

    mail.send_invite(Job(uuid4(), "email.invite", people.a, {"invite_id": str(invite_id)}))

    (sent,) = inbox(email)
    body, _ = message(sent["ID"])
    assert sent["Subject"] == subject
    assert link_in(body).startswith(f"{APP_URL}/{page}/invite#")


# A deleted invite (a resend gives it a new id, the same case) sends nothing and the job
# still completes cleanly.
def test_send_invite_for_a_gone_invite_sends_nothing(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    invite_id = pending(people.a, email)
    with migrate_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        conn.execute(text("DELETE FROM invites WHERE id = :i"), {"i": invite_id})
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.invite",
            f"email.invite:{people.a}:{invite_id}",
            {"invite_id": str(invite_id)},
            tenant_id=people.a,
        )

    run_jobs()

    assert inbox(email) == []
    with migrate_engine.connect() as conn:
        row = conn.execute(
            text("""
            SELECT completed_at, last_error FROM jobs
            WHERE dedupe_key = :key
            """),
            {"key": f"email.invite:{people.a}:{invite_id}"},
        ).one()
    assert row.completed_at is not None
    assert row.last_error is None


# An owner-chosen business name never breaks the template or duplicates the link.
def test_send_invite_puts_the_business_name_on_its_own_line(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    name = "Studio $link ${x} Ana"
    with migrate_engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET name = :n WHERE id = :t"), {"n": name, "t": people.a})
    email = fresh_email()
    invite_id = pending(people.a, email)

    mail.send_invite(Job(uuid4(), "email.invite", people.a, {"invite_id": str(invite_id)}))

    (sent,) = inbox(email)
    body, _ = message(sent["ID"])
    assert f"“{name}”" in body.splitlines()
    assert body.count("http") == 1
    assert name not in sent["Subject"]


# The job only mints inside its own tenant; an invite of another business is untouched.
def test_send_invite_only_mints_within_its_own_tenant(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    invite_id = pending(people.a, email)

    mail.send_invite(Job(uuid4(), "email.invite", people.b, {"invite_id": str(invite_id)}))

    assert inbox(email) == []
    (token_hash,) = invite_row(migrate_engine, people.a, invite_id, "token_hash")
    assert token_hash is None


# Running the job again mints and sends a new link; only the newest one still works.
def test_running_the_job_again_sends_a_new_link(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    invite_id = pending(people.a, email)
    mail.send_invite(Job(uuid4(), "email.invite", people.a, {"invite_id": str(invite_id)}))
    (first,) = inbox(email)
    first_secret = link_in(message(first["ID"])[0]).split("#")[1].split(".")[1]

    mail.send_invite(Job(uuid4(), "email.invite", people.a, {"invite_id": str(invite_id)}))
    (second,) = [m for m in inbox(email) if m["ID"] != first["ID"]]
    second_secret = link_in(message(second["ID"])[0]).split("#")[1].split(".")[1]

    assert second_secret != first_secret
    (stored,) = invite_row(migrate_engine, people.a, invite_id, "token_hash")
    assert stored == hashlib.sha256(second_secret.encode()).digest()
    assert stored != hashlib.sha256(first_secret.encode()).digest()


# Minting and sending share a transaction; a failed send leaves no hash for the retry.
def test_a_failed_send_leaves_no_hash(
    people: People,
    app_engine: Engine,
    migrate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = fresh_email()
    invite_id = pending(people.a, email)

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("SMTP unavailable")

    monkeypatch.setattr(mail, "deliver", broken)
    with pytest.raises(OSError):
        mail.send_invite(Job(uuid4(), "email.invite", people.a, {"invite_id": str(invite_id)}))

    (token_hash,) = invite_row(migrate_engine, people.a, invite_id, "token_hash")
    assert token_hash is None


# Registration bounds, the no-tenant guard, and the kind reachable through the worker.
def test_email_invite_kind_is_registered_with_safe_bounds() -> None:
    kind = KINDS["email.invite"]
    assert kind.timeout < 60
    assert kind.grace > timedelta(minutes=15)


def test_send_invite_needs_a_tenant() -> None:
    with pytest.raises(ValueError):
        mail.send_invite(Job(uuid4(), "email.invite", None, {"invite_id": str(uuid4())}))


def test_send_invite_runs_through_the_worker(
    people: People, app_engine: Engine, migrate_engine: Engine
) -> None:
    email = fresh_email()
    invite_id = pending(people.a, email)
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.invite",
            f"email.invite:{people.a}:{invite_id}",
            {"invite_id": str(invite_id)},
            tenant_id=people.a,
        )

    run_jobs()

    assert [m["Subject"] for m in inbox(email)] == ["You're invited to join a business on ziftbook"]
