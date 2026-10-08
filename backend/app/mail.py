"""Email through Mailgun's HTTP API when deployed, Mailpit's HTTP API in development. Never SMTP."""

import base64
import hashlib
import json
import logging
import re
import secrets
import urllib.request
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from string import Template
from typing import Any, Literal, get_args
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import requests
from mailgun.client import Client
from mailgun.config import RetryPolicy
from opentelemetry.trace import SpanKind
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import business_settings, passwords, tracing
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


class MailNotConfigured(RuntimeError):
    """Neither Mailgun nor Mailpit is configured: the email stays queued for the retry."""


# Mailgun's regional API hosts (the EU one keeps the data in the EU).
MAILGUN_API = {"eu": "api.eu.mailgun.net", "us": "api.mailgun.net"}


def transport(settings: MailSettings) -> Literal["mailgun", "mailpit", "off"]:
    """The way mail goes. The key wins, so a stray Mailpit URL cannot divert a deployment's mail."""
    if settings.mailgun_api_key.get_secret_value():
        return "mailgun" if settings.mailgun_domain else "off"
    return "mailpit" if settings.mailpit_url else "off"


def deliver(
    template: str,
    to: str,
    subject: str,
    body: str,
    *,
    ics: bytes | None = None,
) -> None:
    """Hand one message for one address to Mailgun (Mailpit in development), and log it by template.

    Click tracking is always off: Mailgun would otherwise rewrite every link through its tracking
    domain, and most carry a secret. With ics, the message becomes multipart/mixed with the
    plain-text body first, and a calendar part (text/calendar; method=PUBLISH).

    The job's id comes from its bound context. Never the address, subject or body, and on failure
    the error's class only, plus the HTTP status when there was one (a 401 is a bad key, a 400 a bad
    message); the error still propagates, so the job is retried.
    """
    try:
        _send(to, subject, body, ics)
    except Exception as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        logger.warning(
            "email failed",
            extra={"template": template, "error": type(error).__name__}
            | ({"status": status} if status is not None else {}),
        )
        raise
    logger.info("email sent", extra={"template": template})


def _send(to: str, subject: str, body: str, ics: bytes | None) -> None:
    settings = MailSettings()
    way = transport(settings)
    if way == "off":
        raise MailNotConfigured("set ZIF_MAILGUN_API_KEY and ZIF_MAILGUN_DOMAIN")
    # Exactly the one stored address: Mailgun splits `to` on commas, so a row that reads as a list
    # ("a@x, victim@y", written around the API) must not reach a second inbox.
    to = passwords.normalise_email(to)
    sender = f"no-reply@{settings.mailgun_domain or 'localhost'}"
    message = EmailMessage()
    message["From"] = f"ziftbook <{sender}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if ics is not None:
        message.add_attachment(
            ics,
            maintype="text",
            subtype="calendar",
            filename="booking.ics",
            params={"method": "PUBLISH", "charset": "utf-8"},
        )
    mailpit = urlsplit(settings.mailpit_url or "")
    host, port = (
        (MAILGUN_API[settings.mailgun_region], 443)
        if way == "mailgun"
        else (mailpit.hostname or "", mailpit.port or (443 if mailpit.scheme == "https" else 80))
    )
    # Never the recipient, subject, body or template values: only the server this deployment talks
    # to.
    with tracing.span("email send", SpanKind.CLIENT, {"server.address": host, "server.port": port}):
        if way == "mailgun":
            # No SDK retries: its default retries a timed-out POST (a second copy) and would outlast
            # the job's 30s timeout. The job runner retries instead. 10s per phase.
            # ponytail: no cap on the whole send; the job's 30s timeout stands in, and a send still
            # running past it can duplicate (at least once, as documented on send()).
            with Client(
                auth=("api", settings.mailgun_api_key.get_secret_value()),
                api_url=f"https://{host}",
                timeout=(10.0, 10.0),
                retry_policy=RetryPolicy(max_retries=0),
            ) as client:
                # The MIME as built above, so the calendar part goes out exactly as typed.
                response = client.mimemessage.create(
                    data={"to": to, "o:tracking-clicks": "no"},
                    files={"message": ("message.mime", message.as_bytes())},
                    domain=settings.mailgun_domain,
                )
            # Only a 200 is "queued": a redirect (never followed) must not mark the email sent.
            if response.status_code != 200:
                status = response.status_code
                raise requests.HTTPError(f"Mailgun answered {status}", response=response)
        else:
            # Mailpit's HTTP send API: development only, never SMTP.
            payload: dict[str, Any] = {
                "From": {"Email": sender, "Name": "ziftbook"},
                "To": [{"Email": to}],
                "Subject": subject,
                "Text": body,
            }
            if ics is not None:
                payload["Attachments"] = [
                    {
                        "Content": base64.b64encode(ics).decode(),
                        "Filename": "booking.ics",
                        "ContentType": "text/calendar; method=PUBLISH; charset=utf-8",
                    }
                ]
            request = urllib.request.Request(
                f"{mailpit.geturl().rstrip('/')}/api/v1/send",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10):
                pass


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
        deliver(template, row.email, subject, body)


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
        deliver("invite", invite.email, subject, body)


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
    that tenant (any more) gets nothing. Delivery is at least once: a crash between the Mailgun
    handoff and recording it sends again.
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
            # Mailgun handoff, it stays pending: only the audit trail is off, nobody is emailed.
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
    "booking_client_cancelled": "cancelled_by_client",
    "booking_client_rescheduled": "confirmed",
    # ZIF-57: the team is told when a colleague moves a booking of theirs.
    "booking_moved_team": "confirmed",
}

# ZIF-54 D1: the client templates whose $link is the guest booking link, minted here at send time.
LINKED = frozenset({"booking_received", "booking_confirmed", "booking_reminder"})

BOOKING = text("""
SELECT c.email AS client_email, c.locale AS client_locale, c.name AS client_name,
       t.name AS business, b.client_id, b.status, b.starts_at, b.ends_at, b.expires_at,
       b.service_name, b.reschedule_count, b.decline_message, b.worker_display_name, now() AS now
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
JOIN tenants t ON t.id = b.tenant_id
WHERE b.id = :id
""")

INSERT_LINK = text("""
INSERT INTO booking_links (token_hash, tenant_id, booking_id)
VALUES (:hash, current_setting('app.tenant_id')::uuid, :booking_id)
""")

MERCHANT = text("""
SELECT u.email, u.locale, m.role FROM users u JOIN memberships m ON m.user_id = u.id
WHERE u.id = :id
""")


def _quoted(message: str) -> str:
    """Every line prefixed "> " (a blank one is ">"), a run of blank lines collapsed to one."""
    lines: list[str] = []
    for line in message.split("\n"):
        if line.strip():
            lines.append(f"> {line}")
        elif lines[-1:] != [">"]:
            lines.append(">")
    return "\n".join(lines)


def _local_text(value: dict[str, str], locale: str) -> str:
    """value[locale], else the first present of LOCALES."""
    if value.get(locale):
        return value[locale]
    for fallback in LOCALES:
        if value.get(fallback):
            return value[fallback]
    return ""


def ics(
    booking_id: UUID | str,
    starts_at: datetime,
    ends_at: datetime,
    summary: str,
    now: datetime,
    *,
    sequence: int = 0,
) -> bytes:
    """A stdlib-only VCALENDAR/PUBLISH, stable UID per booking. See ZIF-53 SS3.3.

    sequence (ZIF-54): the booking's reschedule_count. RFC 5545: a calendar client applies a
    PUBLISH VEVENT only when its SEQUENCE is >= the one it already holds, so a reschedule's new
    .ics (same UID) must carry a higher SEQUENCE or the client's calendar app silently ignores it.
    """

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
        f"SEQUENCE:{sequence}",
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

    Payload: booking_id, template, and (merchant) user_id, or (reminder/reschedule) starts_at,
    or (merchant reschedule) previous_starts_at. See ZIF-53 SS3, ZIF-54 D1/D10.
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
        # ZIF-54 D10. Generic: any template whose payload carries starts_at (the reminder, and
        # both reschedule emails) skips itself once the booking has moved on again -- an `:r1`
        # email that runs after `:r2` already changed starts_at sends nothing (test 32). The
        # reminder keeps its own extra check: due, but not yet past, the start.
        if "starts_at" in payload and datetime.fromisoformat(payload["starts_at"]) != row.starts_at:
            return
        if template == "booking_reminder" and row.starts_at <= row.now:
            return

        user_id = payload.get("user_id")
        role = None  # the merchant recipient's, ZIF-143
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
            recipient_email, recipient_locale, role = merchant.email, merchant.locale, merchant.role

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
        token: str | None = None
        if user_id is not None:
            # Merchant emails keep the console link, never the manage link (D10): a manage link
            # reaching staff is a threat the console link cannot be.
            values["client"] = row.client_name
            values["link"] = f"{app_url}/{locale}"
            if "previous_starts_at" in payload:
                previous_local = datetime.fromisoformat(payload["previous_starts_at"]).astimezone(
                    ZoneInfo(zone)
                )
                values["old_date"] = previous_local.strftime(DATE_FORMAT[locale])
                values["old_time"] = previous_local.strftime("%H:%M")
        elif template in LINKED:
            # D1: mint and set values["link"] BEFORE render() (the body needs it), and only insert
            # the hash if _outbox (below) reports 'pending' -- a token minted for a dedupe no-op is
            # dropped unused, never stored.
            token = secrets.token_urlsafe(32)
            values["link"] = f"{app_url}/{locale}/booking#{job.tenant_id}.{token}"
        file = template
        if (
            template == "booking_moved_team"
            and "member_changed" in payload
            and row.worker_display_name
        ):
            # Only the file differs, as booking_declined_message below: the status gate, the outbox
            # row and the log keep the template's own name.
            file = "booking_moved_team_member"
            values["member"] = row.worker_display_name
        if (
            template == "booking_request"
            and role == "worker"
            and not settings.workers_answer_requests
        ):
            # ZIF-143: a worker who can't answer learns of the request, worded for the owner
            # answering it. Only the file differs, as below.
            file = "booking_request_team_member"
        if template == "booking_request":
            expires_local = row.expires_at.astimezone(ZoneInfo(zone))
            values["expires"] = (
                expires_local.strftime(f"{DATE_FORMAT[locale]} %H:%M") + f" ({zone})"
            )
        if template == "booking_declined" and row.decline_message is not None:
            # ZIF-121: only the file rendered differs. `template` stays booking_declined for
            # STATUS_FOR, the outbox row and the log. The text is a value, never rescanned, and
            # quoted so a merchant cannot pass their words off as the platform's own footer.
            file = "booking_declined_message"
            values["message"] = _quoted(row.decline_message)
        subject, body = render(file, locale, values)
        status = _outbox(session, job, recipient_id, template, subject)
        if status == "pending" and token is not None:
            session.execute(
                INSERT_LINK,
                {
                    "hash": hashlib.sha256(token.encode()).digest(),
                    "booking_id": payload["booking_id"],
                },
            )
    if status == "sent":
        return

    booking_ics = None
    if template == "booking_confirmed":
        summary = f"{values['service']} - {row.business}"
        booking_ics = ics(
            payload["booking_id"],
            row.starts_at,
            row.ends_at,
            summary,
            datetime.now(UTC),
            sequence=row.reschedule_count,
        )
    deliver(template, recipient_email, subject, body, ics=booking_ics)
    _mark_sent(job)
