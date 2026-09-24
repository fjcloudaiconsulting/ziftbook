"""Email over SMTP: Mailpit in development, Mailgun's EU endpoint when deployed. One code path."""

import hashlib
import logging
import re
import secrets
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from string import Template
from typing import get_args
from uuid import UUID
from zoneinfo import ZoneInfo

from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import business_settings, tracing
from app.business_settings import Locale
from app.config import MailSettings
from app.db import SessionLocal, tenant_context
from app.jobs import Job

TEMPLATES = Path(__file__).parent / "mail_templates"
LOCALES = get_args(Locale)

# Numeric, day-first: no Babel, no weekday/month names (those would be copy in code, and the .ics
# already puts the event in the client's own calendar with their own formatting).
DATE_FORMAT = {"en": "%d/%m/%Y", "nl": "%d-%m-%Y", "pt": "%d/%m/%Y"}

_ICS_CONTROL_CHARS = re.compile(
    "[\x00-\x09\x0b\x0c\x0e-\x1f\x7f\x80-\x9f\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]"
)

logger = logging.getLogger(__name__)


def render(template: str, locale: str, values: dict[str, str] | None = None) -> tuple[str, str]:
    """A template is one text file per locale: the first line is the subject, the rest the body.

    With values, $placeholders in the body are filled in; a missing one raises.
    """
    if not re.fullmatch(r"[a-z_]+", template):
        raise ValueError(f"invalid template name {template!r}")
    lines = (TEMPLATES / f"{template}.{locale}.txt").read_text().splitlines()
    body = "\n".join(lines[1:]).strip() + "\n"
    return lines[0], Template(body).substitute(values) if values is not None else body


def deliver(
    template: str,
    to: str,
    subject: str,
    body: str,
    headers: dict[str, str] | None = None,
    ics: bytes | None = None,
) -> None:
    """Hand one message for one address to the SMTP server, and log it by template.

    With ics, the message becomes multipart/mixed with the plain-text body first, as a calendar
    part (text/calendar; method=PUBLISH).

    The job's id comes from its bound context. Never the address, subject or body, and on failure
    the error's class only (SMTPRecipientsRefused quotes the address); the error still propagates,
    so the job is retried.
    """
    try:
        _send(to, subject, body, headers, ics)
    except Exception as error:
        logger.warning("email failed", extra={"template": template, "error": type(error).__name__})
        raise
    logger.info("email sent", extra={"template": template})


def _send(
    to: str,
    subject: str,
    body: str,
    headers: dict[str, str] | None,
    ics: bytes | None,
) -> None:
    settings = MailSettings()
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    if ics is not None:
        message.add_attachment(
            ics,
            maintype="text",
            subtype="calendar",
            filename="booking.ics",
            params={"method": "PUBLISH", "charset": "utf-8"},
        )
    # Never the recipient, subject, body or template values: only the server this deployment talks
    # to.
    with tracing.span(
        "smtp send",
        SpanKind.CLIENT,
        {"server.address": settings.smtp_host, "server.port": settings.smtp_port},
    ):
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
            template = "sign_up_registered"
            subject, body = render(
                template, row.locale, {"sign_in": f"{app_url}/{row.locale}/sign-in"}
            )
        else:
            template = row.purpose
            link = f"{app_url}/{row.locale}/{PAGES[row.purpose]}#{token}"
            subject, body = render(template, row.locale, {"link": link})
        # Mailgun would otherwise rewrite the link, token included, through its tracking domain.
        deliver(template, row.email, subject, body, {"X-Mailgun-Track-Clicks": "no"})


def send_invite(job: Job) -> None:
    """The email.invite job: mail the link that accepts an invite. Payload: invite_id.

    Runs in the invite's business. A resent or revoked invite has another id or none, so a queued
    job for the old one sends nothing. Minting and sending share a transaction, as in send_token:
    a failed send leaves no hash, and the retry mints a new one.
    """
    if job.tenant_id is None:
        raise ValueError("email.invite needs a tenant")
    token = secrets.token_urlsafe(32)
    app_url = MailSettings().app_url.rstrip("/")
    with tenant_context(job.tenant_id) as session:
        invite = session.execute(
            text("""
            UPDATE invites i SET token_hash = :hash, expires_at = now() + interval '7 days'
            WHERE i.id = :id
            RETURNING i.email, (SELECT t.name FROM tenants t WHERE t.id = i.tenant_id) AS business
            """),
            {"id": job.payload["invite_id"], "hash": hashlib.sha256(token.encode()).digest()},
        ).first()
        if invite is None:  # revoked, resent (new id) or accepted
            return
        language = business_settings.read(session).language
        link = f"{app_url}/{language}/invite#{job.tenant_id}.{token}"
        subject, body = render("invite", language, {"link": link, "business": invite.business})
        # Mailgun would otherwise rewrite the link, secret included, through its tracking domain.
        deliver("invite", invite.email, subject, body, {"X-Mailgun-Track-Clicks": "no"})


def _outbox(
    session: Session, job: Job, recipient_id: UUID | str, template: str, subject: str
) -> str:
    """Insert or find this job's outbox row and return its status ('pending' or 'sent')."""
    status: str = session.execute(
        text("""
        INSERT INTO email_outbox (tenant_id, job_id, recipient_id, template, subject)
        VALUES (:tenant_id, :job_id, :recipient_id, :template, :subject)
        ON CONFLICT (tenant_id, job_id) DO UPDATE SET subject = excluded.subject
        RETURNING status
        """),
        {
            "tenant_id": job.tenant_id,
            "job_id": job.id,
            "recipient_id": recipient_id,
            "template": template,
            "subject": subject,
        },
    ).scalar_one()
    return status


def _mark_sent(job: Job) -> None:
    assert job.tenant_id is not None  # every caller already checked this before sending
    with tenant_context(job.tenant_id) as session:
        session.execute(
            text("UPDATE email_outbox SET status = 'sent', sent_at = now() WHERE job_id = :job_id"),
            {"job_id": job.id},
        )


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
        # The person's own language, else their business's.
        locale = recipient.locale or business_settings.read(session).language
        subject, body = render(payload["template"], locale)
        status = _outbox(session, job, payload["recipient_id"], payload["template"], subject)
    if status == "sent":
        return

    deliver(payload["template"], recipient.email, subject, body)
    _mark_sent(job)


# The status a template announces; the email goes only if the booking still has it.
STATUS_FOR = {
    "booking_received": "pending",
    "booking_request": "pending",
    "booking_confirmed": "confirmed",
    "booking_new": "confirmed",
    "booking_reminder": "confirmed",
    "booking_declined": "declined",
    "booking_cancelled": "cancelled_by_merchant",
    "booking_cancelled_by_client": "cancelled_by_client",
}

BOOKING = text("""
SELECT c.email AS client_email, c.locale AS client_locale, c.name AS client_name,
       t.name AS business, b.client_id, b.status, b.starts_at, b.ends_at, b.expires_at,
       b.service_name, now() AS now
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
JOIN tenants t ON t.id = b.tenant_id
WHERE b.id = :id
""")

MERCHANT = text("""
SELECT u.email, u.locale FROM users u JOIN memberships m ON m.user_id = u.id WHERE u.id = :id
""")


def _local_text(value: dict[str, str], locale: str) -> str:
    """value[locale], else the first present of LOCALES."""
    if value.get(locale):
        return value[locale]
    for fallback in LOCALES:
        if value.get(fallback):
            return value[fallback]
    return ""


def ics(
    booking_id: UUID | str, starts_at: datetime, ends_at: datetime, summary: str, now: datetime
) -> bytes:
    """A stdlib-only VCALENDAR/PUBLISH, stable UID per booking. See ZIF-53 SS3.3."""

    def escape(value: str) -> str:
        # C0 (minus CR/LF, handled below), DEL, C1, and the Unicode line/paragraph separators:
        # none of those belong in a TEXT value, and left in they either break folding (a control
        # character isn't a line boundary iCalendar understands) or render as garbage.
        value = _ICS_CONTROL_CHARS.sub(" ", value)
        value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        return value.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")

    def stamp(when: datetime) -> str:
        return when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    def fold(line: str) -> str:
        """RFC 5545 line folding: no physical line over 75 octets, never mid-character. A
        continuation line's leading space counts toward its own 75-octet budget."""
        if len(line.encode()) <= 75:
            return line
        parts: list[bytes] = []
        chunk = bytearray()
        budget = 75
        for char in line:
            piece = char.encode()
            if len(chunk) + len(piece) > budget:
                parts.append(bytes(chunk))
                chunk = bytearray()
                budget = 74  # the folded line's leading space eats one octet of the next 75
            chunk += piece
        if chunk:
            parts.append(bytes(chunk))
        return "\r\n ".join(part.decode() for part in parts)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ziftbook//booking//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{booking_id}@ziftbook.com",
        "SEQUENCE:0",
        f"DTSTAMP:{stamp(now)}",
        f"DTSTART:{stamp(starts_at)}",
        f"DTEND:{stamp(ends_at)}",
        f"SUMMARY:{escape(summary)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(fold(line) for line in lines) + "\r\n").encode()


def send_booking(job: Job) -> None:
    """The email.booking job: one client or merchant booking email, or the 24h-ahead reminder.

    Payload: booking_id, template, and (merchant) user_id, or (reminder) starts_at. See ZIF-53 SS3.
    """
    if job.tenant_id is None:
        raise ValueError("email.booking needs a tenant")
    payload = job.payload
    template = payload["template"]
    if template not in STATUS_FOR:
        raise ValueError(f"unknown booking template {template!r}")
    app_url = MailSettings().app_url.rstrip("/")

    with tenant_context(job.tenant_id) as session:
        row = session.execute(BOOKING, {"id": payload["booking_id"]}).first()
        if row is None:
            return
        if row.status != STATUS_FOR[template]:
            return
        if template in ("booking_received", "booking_request") and (
            row.expires_at is None or row.expires_at <= row.now
        ):
            return
        if template == "booking_reminder" and (
            row.starts_at <= row.now
            or datetime.fromisoformat(payload["starts_at"]) != row.starts_at
        ):
            return

        user_id = payload.get("user_id")
        if user_id is None:
            recipient_id = row.client_id
            if row.client_email is None:  # erased, or merchant-created with no address
                return
            recipient_email, recipient_locale = row.client_email, row.client_locale
        else:
            merchant = session.execute(MERCHANT, {"id": user_id}).first()
            if merchant is None:  # RLS: a member only through a membership in THIS tenant
                return
            recipient_id = user_id
            recipient_email, recipient_locale = merchant.email, merchant.locale

        settings = business_settings.read(session)
        locale = recipient_locale or settings.language
        zone = settings.timezone
        starts_local = row.starts_at.astimezone(ZoneInfo(zone))
        values = {
            "business": row.business,
            "service": _local_text(row.service_name, locale),
            "date": starts_local.strftime(DATE_FORMAT[locale]),
            "time": starts_local.strftime("%H:%M"),
            "zone": zone,
        }
        if user_id is not None:
            values["client"] = row.client_name
            values["link"] = f"{app_url}/{locale}"
        if template == "booking_request":
            expires_local = row.expires_at.astimezone(ZoneInfo(zone))
            values["expires"] = (
                expires_local.strftime(f"{DATE_FORMAT[locale]} %H:%M") + f" ({zone})"
            )
        subject, body = render(template, locale, values)
        status = _outbox(session, job, recipient_id, template, subject)
    if status == "sent":
        return

    booking_ics = None
    if template == "booking_confirmed":
        summary = f"{values['service']} - {row.business}"
        booking_ics = ics(
            payload["booking_id"], row.starts_at, row.ends_at, summary, datetime.now(UTC)
        )
    deliver(template, recipient_email, subject, body, ics=booking_ics)
    _mark_sent(job)
