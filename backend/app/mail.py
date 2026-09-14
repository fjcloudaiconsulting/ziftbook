"""Email over SMTP: Mailpit in development, Mailgun's EU endpoint when deployed. One code path."""

import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import text

from app.config import MailSettings
from app.db import tenant_context
from app.jobs import Job

TEMPLATES = Path(__file__).parent / "mail_templates"
LOCALES = ("en", "nl", "pt")


def render(template: str, locale: str) -> tuple[str, str]:
    """A template is one text file per locale: the first line is the subject, the rest the body."""
    if not re.fullmatch(r"[a-z_]+", template):
        raise ValueError(f"invalid template name {template!r}")
    lines = (TEMPLATES / f"{template}.{locale}.txt").read_text().splitlines()
    return lines[0], "\n".join(lines[1:]).strip() + "\n"


def send(job: Job) -> None:
    """The email.send job. Payload: recipient_id, to, template, locale.

    Delivery is at least once: a crash between the SMTP handoff and recording it sends again.
    """
    if job.tenant_id is None:
        raise ValueError("email.send needs a tenant")
    payload = job.payload
    locale = str(payload.get("locale", "en"))
    if locale not in LOCALES:
        locale = "en"
    subject, body = render(payload["template"], locale)

    # The outbox keeps who, which template and the subject; never the rendered body.
    with tenant_context(job.tenant_id) as session:
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

    settings = MailSettings()
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = payload["to"]
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)

    with tenant_context(job.tenant_id) as session:
        session.execute(
            text("UPDATE email_outbox SET status = 'sent', sent_at = now() WHERE job_id = :job_id"),
            {"job_id": job.id},
        )
