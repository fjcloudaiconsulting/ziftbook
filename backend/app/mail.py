"""Email through Mailgun's HTTP API when deployed, Mailpit's HTTP API in development. Never SMTP."""

import base64
import hashlib
import json
import logging
import re
import secrets
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html import escape
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

from app import business_settings, currencies, limits, passwords, tracing
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

# ZB-03: client-facing booking emails per address per hour; over it the send is skipped.
CLIENT_MAIL_PER_RECIPIENT_HOUR = 10
# The same address across every business, so several businesses cannot add up to a flood.
CLIENT_MAIL_PER_RECIPIENT_ANY_HOUR = 30

logger = logging.getLogger(__name__)


# A template body is blocks split by a blank line: "# " opens the heading, "| " lines are the
# booking card, "> $value" a quoted message, "[Label]($link)" the one button, anything else a
# paragraph. The structure comes from the file alone: values are filled in after, never parsed.
_BUTTON = re.compile(r"\[([^\]\n]+)\]\((\$\w+)\)")
Block = tuple[Literal["heading", "card", "quote", "button", "paragraph"], list[str]]

# The layout's own words: the footer under every email, and the fallback under its button.
FOOTER = {"en": "Sent by ziftbook", "nl": "Verstuurd door ziftbook", "pt": "Enviado pelo ziftbook"}
LINK_FALLBACK = {
    "en": "If the button doesn't work, paste this link into your browser:",
    "nl": "Werkt de knop niet? Plak deze link in je browser:",
    "pt": "Se o botão não funcionar, cole este link no seu navegador:",
}


def _parse(template: str, locale: str) -> tuple[str, list[Block]]:
    if not re.fullmatch(r"[a-z_]+", template):
        raise ValueError(f"invalid template name {template!r}")
    subject, *lines = (TEMPLATES / f"{template}.{locale}.txt").read_text().splitlines()
    blocks: list[Block] = []
    for chunk in re.split(r"\n\s*\n", "\n".join(lines).strip()):
        rows = chunk.split("\n")
        if button := _BUTTON.fullmatch(chunk):
            blocks.append(("button", [button[1], button[2]]))
        elif chunk.startswith("# "):
            blocks.append(("heading", [chunk[2:]]))
        elif all(row.startswith("| ") for row in rows):
            blocks.append(("card", [row[2:] for row in rows]))
        elif chunk.startswith("> "):
            blocks.append(("quote", [chunk[2:]]))
        else:
            blocks.append(("paragraph", rows))
    return subject, blocks


def _text(blocks: list[Block], locale: str, fill: Callable[[str], str]) -> str:
    """The text part: a card or paragraph line that fills in empty is left out, and so is a block
    left with nothing; the button is its label and the link on a line of its own."""
    said: list[str] = []
    for kind, parts in blocks:
        if kind == "button":
            said.append(f"{fill(parts[0])}:\n{fill(parts[1])}")
        elif kind == "quote":
            quoted = fill(parts[0])
            if quoted.strip():
                said.append(_quoted(quoted))
        elif lines := [line for line in map(fill, parts) if line.strip()]:
            said.append("\n".join(lines))
    return "\n\n".join([*said, FOOTER[locale]]) + "\n"


def render(template: str, locale: str, values: dict[str, str] | None = None) -> tuple[str, str]:
    """A template is one text file per locale: the first line is the subject, the rest the body.

    Returns the subject and the text part. With values, $placeholders in the subject and the body
    are filled in; a missing one raises.
    """
    subject, blocks = _parse(template, locale)
    if values is None:
        return subject, _text(blocks, locale, lambda part: part)
    return Template(subject).substitute(values), _text(
        blocks, locale, lambda part: Template(part).substitute(values)
    )


def compose(template: str, locale: str, values: dict[str, str]) -> tuple[str, str, str]:
    """The subject, the text part and the HTML part of one email."""
    subject, body = render(template, locale, values)
    return subject, body, _html(template, locale, subject, values)


# Brand colours from frontend/app/tokens.css, as hex: mail clients know no oklch().
INK, MUTED, PRIMARY, SURFACE, LINE = "#281816", "#665755", "#b31513", "#f7f2f1", "#e6dbda"
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"  # the landing wordmark's own fallback

# Inline styles carry the light design (Gmail drops <style> in places); clients that honour
# prefers-color-scheme (Apple Mail, iOS, Outlook for Mac) get the dark one from here.
_HEAD = f"""<style>
body{{margin:0;padding:0;-webkit-text-size-adjust:100%}}
a{{color:{PRIMARY}}}
@media (max-width:520px){{.zb-pad{{padding-left:20px!important;padding-right:20px!important}}
.zb-outer{{padding:16px 8px!important}}.zb-h1{{font-size:24px!important}}}}
@media (prefers-color-scheme:dark){{
.zb-bg{{background:#17100f!important}}.zb-panel{{background:#241a19!important}}
.zb-card{{background:#2f2322!important;border-color:#423432 #423432 #423432 {PRIMARY}!important}}
.zb-ink{{color:#f4edeb!important}}.zb-muted{{color:#baaeac!important}}
.zb-link{{color:#f08f83!important}}.zb-quote{{border-color:#f08f83!important}}}}
</style>"""


def _html(template: str, locale: str, subject: str, values: dict[str, str]) -> str:
    """The HTML part: a table layout with inline styles, no web fonts and no images. Every value is
    escaped (client names, messages and business names come from strangers), and so is the file's
    own text, so nothing a value holds can become markup."""
    safe = {key: escape(value) for key, value in values.items()}

    def fill(part: str) -> str:
        return Template(escape(part)).substitute(safe)

    prose = f"font-family:{SANS};font-size:16px;line-height:1.55;color:{INK}"
    small = f"font-family:{SANS};font-size:13px;line-height:1.5;color:{MUTED}"
    _, blocks = _parse(template, locale)
    heading, preview, body = "", "", []
    for kind, parts in blocks:
        if kind == "heading":
            heading = fill(parts[0])
        elif kind == "button":
            link = Template(parts[1]).substitute(values)
            if not link.startswith(("https://", "http://")):
                raise ValueError("a button's link must be a web address")
            href = escape(link)
            body.append(
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
                ' style="margin:28px 0 12px"><tr><td style="border-radius:8px"'
                f' bgcolor="{PRIMARY}"><a href="{href}" style="display:inline-block;'
                f"background:{PRIMARY};border:14px solid {PRIMARY};border-left-width:28px;"
                f"border-right-width:28px;border-radius:8px;font-family:{SANS};font-size:16px;"
                'font-weight:600;line-height:1.2;color:#ffffff;text-decoration:none">'
                f"{fill(parts[0])}</a></td></tr></table>"
                f'<p class="zb-muted" style="margin:0 0 24px;{small}">'
                f"{escape(LINK_FALLBACK[locale])}<br>"
                f'<a class="zb-link" href="{href}" style="color:{PRIMARY};word-break:break-all">'
                f"{href}</a></p>"
            )
        elif kind == "quote":
            quoted = fill(parts[0])
            if quoted.strip():  # the text part's quoting, so both keep the same lines
                lines = [line[2:] for line in _quoted(quoted).split("\n")]
                body.append(
                    f'<p class="zb-ink zb-quote" style="margin:0 0 16px;padding:4px 0 4px 16px;'
                    f'border-left:3px solid {PRIMARY};{prose};font-style:italic">'
                    + "<br>".join(lines)
                    + "</p>"
                )
        elif rows := [row for row in map(fill, parts) if row.strip()]:
            if kind == "card":
                first, *rest = rows
                body.append(
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
                    f' border="0" class="zb-card" style="margin:8px 0 24px;background:{SURFACE};'
                    f'border:1px solid {LINE};border-left:4px solid {PRIMARY};border-radius:8px">'
                    '<tr><td style="padding:16px 20px">'
                    f'<p class="zb-ink" style="margin:0 0 4px;{prose};font-size:17px;'
                    f'font-weight:600">{first}</p>'
                    + "".join(
                        f'<p class="zb-ink" style="margin:0;{prose}">{row}</p>' for row in rest
                    )
                    + "</td></tr></table>"
                )
            else:
                preview = preview or " ".join(rows)
                body.append(
                    f'<p class="zb-ink" style="margin:0 0 16px;{prose}">'
                    + "<br>".join(rows)
                    + "</p>"
                )
    business = safe.get("business")
    eyebrow = (
        f'<p class="zb-muted" style="margin:0 0 8px;{small};font-weight:600;letter-spacing:.02em">'
        f"{business}</p>"
        if business
        else ""
    )
    return f"""<!doctype html>
<html lang="{locale}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{escape(subject)}</title>
{_HEAD}
</head>
<body class="zb-bg" style="margin:0;padding:0;background:{SURFACE}">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{preview}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="zb-bg"
 style="background:{SURFACE}"><tr><td align="center" class="zb-outer" style="padding:32px 16px">
<!--[if mso]><table role="presentation" width="560" align="center"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
 style="max-width:560px">
<tr><td class="zb-pad" style="padding:0 36px 16px">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="28" height="28" align="center" bgcolor="{PRIMARY}" style="width:28px;height:28px;
background:{PRIMARY};border-radius:7px;font-family:{SERIF};font-size:18px;line-height:28px;
color:#ffffff">z</td>
<td class="zb-ink" style="padding-left:10px;font-family:{SERIF};font-size:22px;line-height:28px;
color:{INK}">ziftbook</td></tr></table>
</td></tr>
<tr><td class="zb-panel zb-pad" style="background:#ffffff;border-radius:12px;padding:32px 36px">
{eyebrow}<h1 class="zb-ink zb-h1" style="margin:0 0 20px;font-family:{SERIF};font-size:28px;
font-weight:400;line-height:1.2;color:{INK}">{heading}</h1>
{"\n".join(body)}
</td></tr>
<tr><td class="zb-pad zb-muted" style="padding:20px 36px;{small}">{escape(FOOTER[locale])}</td></tr>
</table>
<!--[if mso]></td></tr></table><![endif]-->
</td></tr></table>
</body>
</html>
"""


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
    html: str,
    *,
    ics: bytes | None = None,
    reply_to: str | None = None,
) -> None:
    """Hand one message for one address to Mailgun (Mailpit in development), and log it by template.

    Click and open tracking are always off, whatever the domain's own setting: Mailgun would
    otherwise rewrite every link through its tracking domain (most carry a secret), and put a
    remote pixel in the HTML part that reports every open, with the reader's IP. The message is
    multipart/alternative, the plain-text body first and the HTML after it; with ics, that goes
    inside multipart/mixed with a calendar part (text/calendar; method=PUBLISH) beside it.

    The job's id comes from its bound context. Never the address, subject or body, and on failure
    the error's class only, plus the HTTP status when there was one (a 401 is a bad key, a 400 a bad
    message); the error still propagates, so the job is retried.

    reply_to is where the recipient's reply goes instead of the sending address.
    """
    try:
        _send(to, subject, body, html, ics, reply_to)
    except Exception as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        logger.warning(
            "email failed",
            extra={"template": template, "error": type(error).__name__}
            | ({"status": status} if status is not None else {}),
        )
        # template is one of the logical names in mail_templates/: render() and STATUS_FOR bound it.
        tracing.EMAIL_DELIVERIES.add(1, {"email.template": template, "email.outcome": "failed"})
        raise
    logger.info("email sent", extra={"template": template})
    tracing.EMAIL_DELIVERIES.add(1, {"email.template": template, "email.outcome": "sent"})


def _send(
    to: str, subject: str, body: str, html: str, ics: bytes | None, reply_to: str | None
) -> None:
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
    if reply_to is not None:
        # As `to`: a stored value written around the API must not become a list.
        reply_to = passwords.normalise_email(reply_to)
        message["Reply-To"] = reply_to
    message.set_content(body)
    message.add_alternative(html, subtype="html")
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
                    data={"to": to, "o:tracking-clicks": "no", "o:tracking-opens": "no"},
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
                "HTML": html,
            }
            if reply_to is not None:
                payload["ReplyTo"] = [{"Email": reply_to}]
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
PAGES = {
    "sign_up": "sign-up/complete",
    "password_reset": "reset-password",
    "account_sign_up": "account/complete",  # ZIF-146: a person's own account, no business
}
# What a sign-up for an email that already has an account gets instead, and where it signs in:
# the console's sign-in for a business sign-up, the account's for an account sign-up.
REGISTERED = {
    "sign_up": ("sign_up_registered", "sign-in"),
    "account_sign_up": ("account_registered", "account/sign-in"),
}


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
            template, page = REGISTERED[row.purpose]
            subject, body, html = compose(
                template, row.locale, {"sign_in": f"{app_url}/{row.locale}/{page}"}
            )
        else:
            template = row.purpose
            link = f"{app_url}/{row.locale}/{PAGES[row.purpose]}#{token}"
            subject, body, html = compose(template, row.locale, {"link": link})
        deliver(template, row.email, subject, body, html)


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
        # The invitee's own language when their account has one (invite_locale, worker-only, 0061),
        # else the business's.
        language = (
            session.scalar(text("SELECT invite_locale(:id)"), {"id": job.payload["invite_id"]})
            or business_settings.read(session).language
        )
        link = f"{app_url}/{language}/invite#{job.tenant_id}.{token}"
        subject, body, html = compose(
            "invite", language, {"link": link, "business": invite.business}
        )
        deliver("invite", invite.email, subject, body, html)


# The confirm page, under a segment no business slug can take (0035's reserved list).
CONTACT_EMAIL_PAGE = "account/contact-email"


def send_contact_email(job: Job) -> None:
    """The email.contact_email job (ZIF-232): mail the link that confirms a business's contact email
    to that address. Payload: request_id.

    Runs in the request's business. A changed, resent, cleared or confirmed request has another id
    or none, so a queued job for the old one sends nothing. Minting and sending share a transaction,
    as in send_invite: a failed send leaves no hash, and the retry mints a new one. No Reply-To and
    no outbox row, as for an invite.
    """
    if job.tenant_id is None:
        raise ValueError("email.contact_email needs a tenant")
    token = secrets.token_urlsafe(32)
    app_url = MailSettings().app_url.rstrip("/")
    with tenant_context(job.tenant_id) as session:
        # The PUT's own rule, again: a row written around it (0088 moved any string) must not reach
        # a list or a header. Checked before minting, so such a request gets no link at all.
        email = session.scalar(
            text("SELECT email FROM contact_email_requests WHERE id = :id"),
            {"id": job.payload["request_id"]},
        )
        if email is None:
            return
        try:
            business_settings.header_address(passwords.normalise_email(email))
        except ValueError:
            logger.warning("contact email link skipped", extra={"reason": "invalid_address"})
            return
        request = session.execute(
            text("SELECT email, business FROM mint_contact_email_token(:id, :hash)"),
            {"id": job.payload["request_id"], "hash": hashlib.sha256(token.encode()).digest()},
        ).first()
        if request is None:  # changed, resent, cleared, confirmed, or queued too long ago
            return
        language = business_settings.read(session).language
        link = f"{app_url}/{language}/{CONTACT_EMAIL_PAGE}#{job.tenant_id}.{token}"
        subject, body, html = compose(
            "contact_email_confirm", language, {"link": link, "business": request.business}
        )
        deliver("contact_email_confirm", request.email, subject, body, html)


# The app role may UPDATE only email_outbox's status, sent_at and subject, and never DELETE (0048):
# a new column these write needs its own GRANT UPDATE in a migration.
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
    that tenant (any more) gets nothing. A template may name the business ($business) and its
    Settings page ($link). Delivery is at least once: a crash between the Mailgun handoff and
    recording it sends again.
    """
    if job.tenant_id is None:
        raise ValueError("email.send needs a tenant")
    payload = job.payload

    # The outbox keeps who, which template and the subject; never the rendered body.
    with tenant_context(job.tenant_id) as session:
        recipient = session.execute(
            text(
                "SELECT u.email, u.locale FROM users u JOIN memberships m ON m.user_id = u.id"
                " WHERE u.id = :id AND m.role <> 'removed'"
            ),
            {"id": payload["recipient_id"]},
        ).first()
        if recipient is None:
            # Not (or no longer) a member of this tenant. If a crash left a 'pending' row after the
            # Mailgun handoff, it stays pending: only the audit trail is off, nobody is emailed.
            return
        # The person's own language, else their business's.
        locale = recipient.locale or business_settings.read(session).language
        business = session.scalar(
            text("SELECT name FROM tenants WHERE id = :id"), {"id": job.tenant_id}
        )
        app_url = MailSettings().app_url.rstrip("/")
        subject, body, html = compose(
            payload["template"],
            locale,
            {"business": business, "link": f"{app_url}/{locale}/settings"},
        )
        status = _outbox(session, job, payload["recipient_id"], payload["template"], subject)
    if status == "sent":
        return

    deliver(payload["template"], recipient.email, subject, body, html)
    _mark_sent(job)


# The status a template announces; the email goes only if the booking still has it.
STATUS_FOR = {
    "booking_received": {"pending"},
    "booking_request": {"pending"},
    "booking_confirmed": {"confirmed"},
    "booking_new": {"confirmed"},
    "booking_reminder": {"confirmed"},
    "booking_declined": {"declined"},
    "booking_cancelled": {"cancelled_by_merchant"},
    "booking_cancelled_by_client": {"cancelled_by_client"},
    "booking_client_cancelled": {"cancelled_by_client"},
    # ZIF-158: a paid booking that ended without being held (the business never answered, or the
    # payment landed after the checkout hold): the client is told the money is going back.
    "booking_expired": {"expired"},
    "booking_client_rescheduled": {"confirmed", "pending"},
    # ZIF-57: the team is told when a colleague moves a booking of theirs.
    "booking_moved_team": {"confirmed"},
    # ZIF-58: a refund Stripe refused or failed, on any ending that refunds (bookings.REFUNDING).
    "booking_refund_failed": {
        "declined",
        "expired",
        "cancelled_by_merchant",
        "cancelled_by_client",
    },
}

# ZIF-54 D1: the client templates whose $link is the guest booking link, minted here at send time.
LINKED = frozenset({"booking_received", "booking_confirmed", "booking_reminder"})

# ZIF-58: the only client emails an erased client still gets, at the address and in the locale its
# erasure kept (client_erasure_holds, which only the worker reads): a booking ended and the money
# owed, and a refund that failed (sent to no one else: a live client's is the business's to follow
# up). None carries a manage link. Every other email to an erased client is skipped.
HOLD_TEMPLATES = frozenset({"booking_cancelled", "booking_expired", "booking_refund_failed"})
# The reasons of the erasures whose cancellations the client is told of: an owner's erasure and an
# account deletion (0064's ERASURE_REASONS but erasure_reapplied: after a restore the client was
# told at the original erasure, so a re-apply never mails again).
ERASURE_REASONS = ("client_erased", "account_erased")
# A hold past its purge date is no hold, purged or not; the business named as well as the policy.
# A cancellation goes through it when the erasure made it, or when it was made before the erasure
# (the team's, while the client was live, its mail still queued): the person's to hear of. The team
# cancelling a booking the erasure kept, afterwards, is not what the hold is kept for.
HOLD = text(f"""
SELECT h.email, h.locale FROM client_erasure_holds h
WHERE h.tenant_id = current_setting('app.tenant_id')::uuid AND h.client_id = :id
  AND h.purge_after > now()
  AND (:template <> 'booking_cancelled'
       OR EXISTS (SELECT 1 FROM booking_events e
                  JOIN clients c ON c.tenant_id = e.tenant_id AND c.id = h.client_id
                  WHERE e.tenant_id = h.tenant_id AND e.booking_id = :booking
                    AND e.event = 'cancelled_by_merchant'
                    AND (e.details ->> 'reason' IN ({", ".join(f"'{r}'" for r in ERASURE_REASONS)})
                         OR e.created_at < c.erased_at)))
""")
# The failure the job announces: its amount, from the event that told it.
REFUND_FAILED = text("""
SELECT (e.details ->> 'amount_minor')::int AS amount_minor, b.price_currency AS currency
FROM booking_events e JOIN bookings b ON b.tenant_id = e.tenant_id AND b.id = e.booking_id
WHERE e.id = :event AND e.booking_id = :booking AND e.event = 'refund_failed'
""")
CONTACT_LINE = {
    "en": "You can reach them at $email.",
    "nl": "Het e-mailadres van het bedrijf is $email.",
    "pt": "O e-mail do negócio é $email.",
}

# FOR SHARE OF c (ZIF-58): a job that read the client while an erasure was in flight would mint a
# manage link after the erasure deleted them, and mail what it read before; a link's foreign key
# takes only KEY SHARE, which the erasure does not block. This waits for the erasure, then re-reads
# the client (no address, erased) and sends nothing. The job takes no other lock first.
BOOKING = text("""
SELECT c.email AS client_email, c.locale AS client_locale,
       coalesce(b.client_name, c.name) AS client_name, c.erased_at IS NOT NULL AS client_erased,
       b.client_message,
       t.name AS business, b.client_id, b.status, b.starts_at, b.ends_at, b.expires_at,
       b.service_name, b.reschedule_count, b.decline_message, b.worker_display_name, now() AS now
FROM bookings b
JOIN clients c ON c.tenant_id = b.tenant_id AND c.id = b.client_id
JOIN tenants t ON t.id = b.tenant_id
WHERE b.id = :id
FOR SHARE OF c
""")

# ZIF-158. The booking's checked or paid payment, with the refund percentage its cancel was shown
# (a client's own cancel stores it on the event).
PAYMENT_LINE = text("""
SELECT p.status, p.amount_minor, p.currency, p.stripe_fee_minor,
       left(p.intent_id, 5) = 'seti_' AS setup,
       (SELECT (e.details ->> 'refund_pct')::int FROM booking_events e
        WHERE e.booking_id = p.booking_id AND e.event = 'cancelled_by_client'
        ORDER BY e.id DESC LIMIT 1) AS client_pct
FROM booking_payments p
WHERE p.booking_id = :id AND p.status IN ('checked', 'paid')
""")

# What a booking's email says about the money, per recipient (the team or the client).
ARRIVAL = {"booking_request", "booking_new", "booking_received", "booking_confirmed"}
ENDED = {"booking_declined", "booking_cancelled", "booking_cancelled_by_client"}
PAYMENT_LINES = {
    "team_checked": {
        "en": "Card checked for $amount. Nothing was charged.",
        "nl": "Kaart gecontroleerd voor een bedrag van $amount. Er is niets afgeschreven.",
        "pt": "Cartão verificado no valor de $amount. Nada foi cobrado.",
    },
    "team_checked_setup": {
        "en": "Card checked. Nothing was charged.",
        "nl": "Kaart gecontroleerd. Er is niets afgeschreven.",
        "pt": "Cartão verificado. Nada foi cobrado.",
    },
    "team_paid": {
        "en": "Paid online: $amount.",
        "nl": "Online betaald: $amount.",
        "pt": "Pago online: $amount.",
    },
    "checked": {
        "en": "We checked your card for $amount. Nothing was charged.",
        "nl": "We hebben je kaart gecontroleerd voor een bedrag van $amount. "
        "Er is niets afgeschreven.",
        "pt": "Verificamos seu cartão no valor de $amount. Nada foi cobrado.",
    },
    "checked_setup": {
        "en": "We checked your card. Nothing was charged.",
        "nl": "We hebben je kaart gecontroleerd. Er is niets afgeschreven.",
        "pt": "Verificamos seu cartão. Nada foi cobrado.",
    },
    "paid": {
        "en": "You paid $amount.",
        "nl": "Je hebt $amount betaald.",
        "pt": "Você pagou $amount.",
    },
    "refund": {
        "en": "$amount will be refunded to your payment method. "
        "Your bank may take a few days to show it.",
        "nl": "$amount wordt teruggestort op je betaalmethode. "
        "Het kan een paar dagen duren voordat je bank dit laat zien.",
        "pt": "$amount será reembolsado na sua forma de pagamento. "
        "Seu banco pode levar alguns dias para mostrar o valor.",
    },
    "fee_kept": {
        "en": "Payment processing fees of $amount are not refunded.",
        "nl": "Betalingskosten van $amount worden niet terugbetaald.",
        "pt": "A taxa de processamento do pagamento ($amount) não é reembolsada.",
    },
}
SYMBOLS = {"BRL": "R$", "EUR": "€", "GBP": "£", "USD": "$"}

# Who a client's booking is with, worded as their booking page words it (frontend/messages).
WITH = {
    "en": ("With $name", "With a member of the team"),
    "nl": ("Bij $name", "Bij iemand van het team"),
    "pt": ("Com $name", "Com alguém da equipe"),
}


def _money(amount_minor: int, currency: str, locale: str) -> str:
    """The web app's money format (frontend/lib/money.ts: Intl.NumberFormat, currency style, narrow
    symbol) for en, nl and pt: the business's currency in the reader's number format, with as many
    decimals as the currency has minor units (app/currencies.py; KeyError for one not listed). A
    negative amount carries its minus where Intl puts it: before the sign in en and pt, before the
    digits in nl."""
    units = currencies.minor_units(currency)
    minus = "-" if amount_minor < 0 else ""
    whole, part = divmod(abs(amount_minor), 10**units)
    fraction = f"{part:0{units}d}" if units else ""
    symbol = SYMBOLS.get(currency)
    if locale == "en":
        number = f"{whole:,}" + (f".{fraction}" if units else "")
        # Intl: a symbol touches the amount; a code stands in its place, then a no-break space.
        return f"{minus}{symbol}{number}" if symbol else f"{minus}{currency}\u00a0{number}"
    number = f"{whole:,}".replace(",", ".") + (f",{fraction}" if units else "")
    # The narrow symbol, as the web shows it: "$", never "US$" (ZIF-102, owner's ruling).
    if locale == "nl":
        return f"{symbol or currency}\u00a0{minus}{number}"
    return f"{minus}{symbol or currency}\u00a0{number}"


def _line(key: str, locale: str, amount_minor: int, currency: str) -> str:
    return Template(PAYMENT_LINES[key][locale]).substitute(
        amount=_money(amount_minor, currency, locale)
    )


def payment_line(
    session: Session, template: str, booking_id: str, status: str, locale: str, team: bool
) -> str:
    """The sentence about the money that `$payment` fills in, else ""."""
    from app import payments  # here: payments pulls in far more than a mail job needs

    if template not in ARRIVAL | ENDED:
        return ""
    paid = session.execute(PAYMENT_LINE, {"id": booking_id}).first()
    if paid is None:
        return ""
    if template in ARRIVAL:
        key = "checked_setup" if paid.setup else paid.status
        if team:
            key = "team_" + key
        return _line(key, locale, paid.amount_minor, paid.currency)
    if paid.status != "paid":
        return ""
    pct = (paid.client_pct or 0) if status == "cancelled_by_client" else 100
    due = payments.refund_due(paid.amount_minor, pct, status, paid.stripe_fee_minor)
    if not due:
        return ""
    said = _line("refund", locale, due, paid.currency)
    kept = paid.amount_minor * pct // 100 - due
    if kept > 0:  # only with R6's flag on: refund_due deducts nothing otherwise
        said += " " + _line("fee_kept", locale, kept, paid.currency)
    return said


INSERT_LINK = text("""
INSERT INTO booking_links (token_hash, tenant_id, booking_id)
VALUES (:hash, current_setting('app.tenant_id')::uuid, :booking_id)
""")

MERCHANT = text("""
SELECT u.email, u.locale, m.role FROM users u JOIN memberships m ON m.user_id = u.id
WHERE u.id = :id AND m.role <> 'removed'
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
        if row.status not in STATUS_FOR[template]:
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
        held = None  # the erased client's contact hold, ZIF-58
        if user_id is None:
            recipient_id = row.client_id
            recipient_email, recipient_locale = row.client_email, row.client_locale
            if row.client_erased:
                held = (
                    session.execute(
                        HOLD,
                        {
                            "id": row.client_id,
                            "template": template,
                            "booking": payload["booking_id"],
                        },
                    ).first()
                    if template in HOLD_TEMPLATES
                    else None
                )
                if held is None:  # no longer to be told anything, or not this
                    if template == "booking_refund_failed":  # money a person may never hear of
                        logger.info(
                            "email skipped", extra={"template": template, "reason": "no_hold"}
                        )
                    return
                recipient_email, recipient_locale = held.email, held.locale
            elif template == "booking_refund_failed":  # through a hold only
                return
            if recipient_email is None:  # merchant-created with no address
                return
        else:
            # ZIF-58: the team hears nothing more about an erased client's booking; the erasure
            # tells the owners itself.
            if row.client_erased:
                return
            merchant = session.execute(MERCHANT, {"id": user_id}).first()
            if merchant is None:  # RLS and the filter: a live member of THIS tenant only
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
        values["payment"] = payment_line(
            session, template, payload["booking_id"], row.status, locale, user_id is not None
        )
        token: str | None = None
        if user_id is not None:
            # Merchant emails keep the console link, never the manage link (D10): a manage link
            # reaching staff is a threat the console link cannot be.
            values["client"] = row.client_name
            values["message"] = row.client_message or ""  # the template's "> " block quotes it
            values["link"] = f"{app_url}/{locale}"
            if "previous_starts_at" in payload:
                previous_local = datetime.fromisoformat(payload["previous_starts_at"]).astimezone(
                    ZoneInfo(zone)
                )
                values["old_date"] = previous_local.strftime(DATE_FORMAT[locale])
                values["old_time"] = previous_local.strftime("%H:%M")
        else:
            named, team = WITH[locale]
            name = row.worker_display_name
            values["with"] = Template(named).substitute(name=name) if name else team
        if user_id is None and template in LINKED:
            # D1: mint and set values["link"] BEFORE render() (the body needs it), and only insert
            # the hash if _outbox (below) reports 'pending' -- a token minted for a dedupe no-op is
            # dropped unused, never stored.
            token = secrets.token_urlsafe(32)
            values["link"] = f"{app_url}/{locale}/booking#{job.tenant_id}.{token}"
        # ZIF-54 D10: a reply quotes the body, so never on an email carrying a manage token. Keyed
        # on the token, not a template list, so a template joining LINKED loses it by itself.
        reply_to = settings.contact_email if user_id is None and token is None else None
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
            # quoted (the template's "> " block) so a merchant cannot pass their words off as the
            # platform's own footer.
            file = "booking_declined_message"
            values["message"] = row.decline_message
        if template == "booking_expired":
            # Only the file differs: a payment that landed after the checkout hold never was a
            # booking for the business, so its text must not blame it.
            paid = session.execute(PAYMENT_LINE, {"id": payload["booking_id"]}).first()
            if paid is None:  # the refund that queued this is recorded with its payment
                return
            values["amount"] = _money(paid.amount_minor, paid.currency, locale)
            if payload.get("late"):
                file = "booking_expired_late"
        if template == "booking_refund_failed":
            failed = session.execute(
                REFUND_FAILED, {"event": payload["event_id"], "booking": payload["booking_id"]}
            ).first()
            if failed is None:
                return
            values["amount"] = _money(failed.amount_minor, failed.currency, locale)
            values["contact"] = (
                ""
                if settings.contact_email is None
                else "\n\n"
                + Template(CONTACT_LINE[locale]).substitute(
                    email=passwords.normalise_email(settings.contact_email)
                )
            )
        subject, body, html = compose(file, locale, values)
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
    # ZIF-58: never a hold's mail (see bookings.email): the only channel to an erased client.
    if (
        user_id is None
        and held is None
        and limits.hit(
            {
                limits.email_key(
                    "mail_rcpt", f"{job.tenant_id}|{recipient_email.lower()}"
                ): CLIENT_MAIL_PER_RECIPIENT_HOUR,
                limits.email_key(
                    "mail_rcpt_any", recipient_email.lower()
                ): CLIENT_MAIL_PER_RECIPIENT_ANY_HOUR,
            },
            timedelta(hours=1),
        )
    ):
        logger.info("email skipped", extra={"template": template, "reason": "recipient_quota"})
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
    deliver(template, recipient_email, subject, body, html, ics=booking_ics, reply_to=reply_to)
    _mark_sent(job)


def send_booking_flood(job: Job) -> None:
    """The email.booking_flood job (ZB-05): tell an owner that their business's guest bookings are
    paused for the rest of the hour. Payload: user_id. Someone no longer an owner of the job's
    business gets nothing. Staff mail, so no Reply-To; and nothing about any client.
    """
    if job.tenant_id is None:
        raise ValueError("email.booking_flood needs a tenant")
    user_id = job.payload["user_id"]
    app_url = MailSettings().app_url.rstrip("/")
    with tenant_context(job.tenant_id) as session:
        owner = session.execute(MERCHANT, {"id": user_id}).first()
        if owner is None or owner.role != "owner":  # RLS and the filter: a live owner of THIS one
            return
        locale = owner.locale or business_settings.read(session).language
        business = session.scalar(
            text("SELECT name FROM tenants WHERE id = :id"), {"id": job.tenant_id}
        )
        subject, body, html = compose(
            "booking_flood", locale, {"business": business, "link": f"{app_url}/{locale}/calendar"}
        )
        status = _outbox(session, job, user_id, "booking_flood", subject)
    if status == "sent":
        return
    deliver("booking_flood", owner.email, subject, body, html)
    _mark_sent(job)
