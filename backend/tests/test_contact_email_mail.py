"""The email.contact_email job (ZIF-232): the worker mints and mails the link that confirms a
business's contact email, to that address only."""

import hashlib
import uuid
from datetime import timedelta
from email import message_from_string
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from app import mail
from app.db import SessionLocal, tenant_context
from app.jobs import Job, enqueue
from app.worker import KINDS
from tests.conftest import (
    MAILGUN_CALLS,
    People,
    fresh_email,
    migrate_tenant_context,
    save_setting,
)
from tests.test_account_email import (  # noqa: F401
    APP_URL,
    as_the_worker,
    bound,
    inbox,
    link_in,
    message,
)
from tests.test_invite_email import run_jobs


def requested(tenant_id: uuid.UUID, email: str, send: bool = True) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        request_id: uuid.UUID = session.execute(
            text(
                "SELECT request_id FROM change_contact_email(:e, :s,"
                " (SELECT user_id FROM memberships WHERE role = 'owner' ORDER BY id LIMIT 1))"
            ),
            {"e": email, "s": send},
        ).scalar_one()
    return request_id


def job_for(tenant_id: uuid.UUID | None, request_id: uuid.UUID) -> Job:
    return Job(uuid4(), "email.contact_email", tenant_id, {"request_id": str(request_id)})


def stored(migrate_engine: Engine, tenant_id: uuid.UUID) -> Any:
    with migrate_tenant_context(migrate_engine, tenant_id) as conn:
        return conn.execute(
            text("SELECT token_hash, expires_at - now() AS lifetime FROM contact_email_requests")
        ).first()


def secret_of(sent: dict[str, Any]) -> str:
    return link_in(message(sent["ID"])[0]).split("#")[1].split(".")[1]


def confirms(tenant_id: uuid.UUID, secret: str) -> bool:
    with tenant_context(tenant_id) as session:
        return (
            session.execute(
                text("SELECT * FROM confirm_contact_email(:h)"),
                {"h": hashlib.sha256(secret.encode()).digest()},
            ).first()
            is not None
        )


# F4.1 FENCE. Wrong impl: the token in a query string, a Reply-To, the wrong business in the
# link, the business name in the subject, or a link the confirm does not take.
def test_the_job_mails_the_address_a_link_that_confirms_it(
    people: People, migrate_engine: Engine
) -> None:
    email = fresh_email()
    request_id = requested(people.a, email)

    mail.send_contact_email(job_for(people.a, request_id))

    (sent,) = inbox(email)
    body, options = message(sent["ID"])
    link = link_in(body)
    page, fragment = link.split("#")
    assert page == f"{APP_URL}/en/account/contact-email" and "?" not in link
    tenant, _, secret = fragment.partition(".")
    assert tenant == str(people.a)
    token_hash, lifetime = stored(migrate_engine, people.a)
    assert token_hash == hashlib.sha256(secret.encode()).digest()
    assert timedelta(hours=23, minutes=59) < lifetime <= timedelta(hours=24)
    assert options["o:tracking-clicks"] == "no"
    assert message_from_string(sent["Raw"]).get("reply-to") is None
    assert sent["Subject"] == "Confirm this email address for ziftbook"
    assert "“a”" in body.splitlines()
    assert confirms(people.a, secret)


@pytest.mark.parametrize(
    ("language", "subject"),
    [
        ("nl", "Bevestig dit e-mailadres voor ziftbook"),
        ("pt", "Confirme este endereço de e-mail para o ziftbook"),
    ],
)
def test_the_link_and_the_email_follow_the_business_language(
    people: People, language: str, subject: str
) -> None:
    save_setting(people.a, "language", language)
    email = fresh_email()

    mail.send_contact_email(job_for(people.a, requested(people.a, email)))

    (sent,) = inbox(email)
    assert sent["Subject"] == subject
    assert link_in(message(sent["ID"])[0]).startswith(
        f"{APP_URL}/{language}/account/contact-email#"
    )


# F4.2 FENCE. Wrong impl: mailing the payload's or the setting's address, or the request's after
# it was replaced, resent, cleared or confirmed.
@pytest.mark.parametrize("after", ["replaced", "resent", "cleared", "never queued"])
def test_a_job_for_a_request_no_longer_due_sends_nothing(people: People, after: str) -> None:
    email = fresh_email()
    request_id = requested(people.a, email, send=after != "never queued")
    with tenant_context(people.a) as session:
        if after == "replaced":
            session.execute(text("SELECT change_contact_email(:e, true)"), {"e": fresh_email()})
        elif after == "resent":
            session.execute(text("SELECT resend_contact_email()"))
        elif after == "cleared":
            session.execute(text("SELECT change_contact_email(NULL, true)"))

    mail.send_contact_email(job_for(people.a, request_id))

    assert inbox(email) == []


# Review S8 / NIT 2 FENCE. Wrong impl: the worker mailing whatever the request holds, so an address
# written around the API (a row 0088 moved, or a compromised API's) reaches a list or a header.
@pytest.mark.parametrize(
    "stored_email",
    ["a@b.nl\r\nbcc: x@y.nl", "a@b.nl, victim@y.nl", "shop <a@b.nl>", "not-an-address"],
)
def test_the_job_mails_only_an_address_the_api_would_take(
    people: People, migrate_engine: Engine, stored_email: str
) -> None:
    with migrate_tenant_context(migrate_engine, people.a) as conn:  # as 0088 leaves a moved row
        request_id = conn.scalar(
            text(
                "INSERT INTO contact_email_requests (tenant_id, email, expires_at)"
                " VALUES (current_setting('app.tenant_id')::uuid, :e, now() + interval '1 day')"
                " RETURNING id"
            ),
            {"e": stored_email},
        )
    before = len(MAILGUN_CALLS)

    mail.send_contact_email(job_for(people.a, request_id))

    assert len(MAILGUN_CALLS) == before
    assert stored(migrate_engine, people.a).token_hash is None  # no link minted either


def test_a_job_after_the_confirm_sends_nothing(people: People) -> None:
    email = fresh_email()
    request_id = requested(people.a, email)
    mail.send_contact_email(job_for(people.a, request_id))
    assert confirms(people.a, secret_of(inbox(email)[0]))

    mail.send_contact_email(job_for(people.a, request_id))

    assert len(inbox(email)) == 1


def test_the_job_mints_only_within_its_own_business(people: People, migrate_engine: Engine) -> None:
    email = fresh_email()
    request_id = requested(people.a, email)

    mail.send_contact_email(job_for(people.b, request_id))

    assert inbox(email) == []
    assert stored(migrate_engine, people.a).token_hash is None


# F4.3 FENCE. Wrong impl: the hash committed before the send (a failed send leaves a link nobody
# got, and the retry's link would sit beside it).
def test_a_failed_send_leaves_no_hash_and_the_retry_mints_anew(
    people: People, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = fresh_email()
    request_id = requested(people.a, email)

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("Mailgun unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(mail, "deliver", broken)
        with pytest.raises(OSError):
            mail.send_contact_email(job_for(people.a, request_id))
    assert stored(migrate_engine, people.a).token_hash is None

    mail.send_contact_email(job_for(people.a, request_id))
    assert confirms(people.a, secret_of(inbox(email)[0]))


# F4.4 FENCE. Wrong impl: a kind missing from the worker (the next release's jobs would sit
# forever), or a grace shorter than the link a queued request promises.
def test_the_kind_is_registered_for_as_long_as_a_queued_link_lives() -> None:
    kind = KINDS["email.contact_email"]
    assert kind.handler is mail.send_contact_email
    assert kind.timeout < 60
    assert kind.grace >= timedelta(hours=24)


def test_the_job_needs_a_business() -> None:
    with pytest.raises(ValueError):
        mail.send_contact_email(job_for(None, uuid4()))


def test_the_job_runs_through_the_worker(people: People) -> None:
    email = fresh_email()
    request_id = requested(people.a, email)
    with SessionLocal.begin() as session:
        enqueue(
            session,
            "email.contact_email",
            f"email.contact_email:{people.a}:{request_id}",
            {"request_id": str(request_id)},
            tenant_id=people.a,
        )

    run_jobs()

    assert [m["Subject"] for m in inbox(email)] == ["Confirm this email address for ziftbook"]


# F4.5 FENCE. Wrong impl: a log line with the address, the link, its secret or the business.
def test_the_job_logs_no_address_link_or_business(
    people: People, migrate_engine: Engine, log_lines: Any
) -> None:
    name = "Studio Zzyzx-232"
    with migrate_engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET name = :n WHERE id = :t"), {"n": name, "t": people.a})
    email = fresh_email()

    mail.send_contact_email(job_for(people.a, requested(people.a, email)))

    secret = secret_of(inbox(email)[0])
    logged = str(log_lines())
    assert "email sent" in logged
    for value in (email, secret, hashlib.sha256(secret.encode()).hexdigest(), name):
        assert value not in logged
