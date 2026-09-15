"""Email over SMTP: Mailpit in development, Mailgun's EU endpoint when deployed. One code path."""

import hashlib
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from string import Template

from sqlalchemy import text

from app.config import MailSettings
from app.db import SessionLocal, tenant_context
from app.jobs import Job

TEMPLATES = Path(__file__).parent / "mail_templates"
LOCALES = ("en", "nl", "pt")


def render(template: str, locale: str, values: dict[str, str] | None = None) -> tuple[str, str]:
    """A template is one text file per locale: the first line is the subject, the rest the body.

    With values, $placeholders in the body are filled in; a missing one raises.
    """
    if not re.fullmatch(r"[a-z_]+", template):
        raise ValueError(f"invalid template name {template!r}")
    lines = (TEMPLATES / f"{template}.{locale}.txt").read_text().splitlines()
    body = "\n".join(lines[1:]).strip() + "\n"
    return lines[0], Template(body).substitute(values) if values is not None else body


def deliver(to: str, subject: str, body: str, headers: dict[str, str] | None = None) -> None:
    """Hand one plain-text message for one address to the SMTP server."""
    settings = MailSettings()
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    try:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        # to_addrs: exactly the one stored address, even if its text reads as a list ("a, b").
        smtp.send_message(message, to_addrs=[to])
    finally:
        # Once the server accepted the message, a failed goodbye must not undo it (and resend).
        try:
            smtp.quit()
        except smtplib.SMTPException, OSError:
            smtp.close()


# The page each purpose's link opens; the token rides in the fragment, which no server ever sees.
PAGES = {"sign_up": "sign-up/complete", "password_reset": "reset-password"}


def send_token(job: Job) -> None:
    """The email.token job: mail a single-use sign-up or password-reset link. Payload: token_id.

    The token exists only in this email; the database keeps its hash. Minting and sending share a
    transaction, so a failed send leaves the request for the retry, which mints a new token.
    """
    token = secrets.token_urlsafe(32)
    app_url = MailSettings().app_url.rstrip("/")
    with SessionLocal.begin() as session:
        row = session.execute(
            text("SELECT * FROM mint_email_token(:id, :hash)"),
            {"id": job.payload["token_id"], "hash": hashlib.sha256(token.encode()).digest()},
        ).first()
        if row is None:  # gone, expired, or a reset for an email with no account
            return
        if row.registered:
            subject, body = render(
                "sign_up_registered", row.locale, {"sign_in": f"{app_url}/{row.locale}/sign-in"}
            )
        else:
            link = f"{app_url}/{row.locale}/{PAGES[row.purpose]}#{token}"
            subject, body = render(row.purpose, row.locale, {"link": link})
        # Mailgun would otherwise rewrite the link, token included, through its tracking domain.
        deliver(row.email, subject, body, {"X-Mailgun-Track-Clicks": "no"})


def send(job: Job) -> None:
    """The email.send job. Payload: recipient_id (a user), template.

    The address and language come from users, inside the job's tenant: someone who isn't a member of
    that tenant (any more) gets nothing. Delivery is at least once: a crash between the SMTP handoff
    and recording it sends again.
    """
    if job.tenant_id is None:
        raise ValueError("email.send needs a tenant")
    payload = job.payload

    # The outbox keeps who, which template and the subject; never the rendered body.
    with tenant_context(job.tenant_id) as session:
        recipient = session.execute(
            text("SELECT email, locale FROM users WHERE id = :id"), {"id": payload["recipient_id"]}
        ).first()
        if recipient is None:
            # Not (or no longer) a member of this tenant. If a crash left a 'pending' row after the
            # SMTP handoff, it stays pending: only the audit trail is off, nobody is emailed.
            return
        # ponytail: English when the user has no language; the tenant default comes with settings.
        subject, body = render(payload["template"], recipient.locale or "en")
        status = session.execute(
            text("""
            INSERT INTO email_outbox (tenant_id, job_id, recipient_id, template, subject)
            VALUES (:tenant_id, :job_id, :recipient_id, :template, :subject)
            ON CONFLICT (tenant_id, job_id) DO UPDATE SET subject = excluded.subject
            RETURNING status
            """),
            {
                "tenant_id": job.tenant_id,
                "job_id": job.id,
                "recipient_id": payload["recipient_id"],
                "template": payload["template"],
                "subject": subject,
            },
        ).scalar_one()
    if status == "sent":
        return

    deliver(recipient.email, subject, body)

    with tenant_context(job.tenant_id) as session:
        session.execute(
            text("UPDATE email_outbox SET status = 'sent', sent_at = now() WHERE job_id = :job_id"),
            {"job_id": job.id},
        )
