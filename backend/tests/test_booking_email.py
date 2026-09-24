"""ZIF-53: booking emails (received, confirmed, declined, cancelled, reminder) to clients, and
new/request emails to merchants. See docs/specs/2026-09-24-zif-53-spec.md SS7 for what each test
protects. Handler tests drive mail.send_booking directly; route tests go through POST/PATCH and
then run the jobs."""

import asyncio
import json
import os
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import auth, mail
from app.db import tenant_context
from app.jobs import Job, run_once
from app.mail import STATUS_FOR
from app.worker import KINDS
from tests.conftest import (
    People,
    add_membership,
    add_user,
    member_id,
    new_client,
    put_settings,
    save_setting,
    set_role,
    signed_in,
)
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_bookings_api import at, post_booking
from tests.test_bookings_approval_api import expire, make_pending, patch
from tests.test_working_hours import seed

MAILPIT = f"http://{os.environ['ZIF_SMTP_HOST']}:8025"


@pytest.fixture(autouse=True)
def clean_outbox(people: People) -> Iterator[None]:
    yield
    for tenant_id in (people.a, people.b):
        with tenant_context(tenant_id) as session:
            session.execute(text("DELETE FROM email_outbox"))


@pytest.fixture
def app(people: People) -> FastAPI:
    from app.main import create_app

    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


# ---------------------------------------------------------------------------
# Mailpit
# ---------------------------------------------------------------------------


def _search(email: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"query": f"to:{email}"})
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/search?{query}", timeout=5) as response:
        messages: list[dict[str, Any]] = json.load(response)["messages"]
        return messages


def subjects_sent_to(email: str) -> list[str]:
    return [message["Subject"] for message in _search(email)]


def mail_for(email: str) -> dict[str, Any]:
    """The one message Mailpit got for email, its parsed summary."""
    (sent,) = _search(email)
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{sent['ID']}", timeout=5) as body:
        message: dict[str, Any] = json.load(body)
        return message


def raw_for(email: str) -> str:
    """The one message Mailpit got for email, its raw source."""
    (sent,) = _search(email)
    with urllib.request.urlopen(f"{MAILPIT}/api/v1/message/{sent['ID']}/raw", timeout=5) as raw:
        return raw.read().decode()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Booking / job helpers
# ---------------------------------------------------------------------------


def run_jobs() -> None:
    async def run_until_idle() -> None:
        while await run_once(KINDS):
            pass

    asyncio.run(run_until_idle())


def run_send_booking(tenant_id: uuid.UUID, booking_id: str, template: str, **extra: Any) -> Job:
    job = Job(
        uuid.uuid4(),
        "email.booking",
        tenant_id,
        {"booking_id": str(booking_id), "template": template, **extra},
    )
    mail.send_booking(job)
    return job


def outbox_for(tenant_id: uuid.UUID, job_id: uuid.UUID) -> Any:
    with tenant_context(tenant_id) as session:
        return (
            session.execute(text("SELECT * FROM email_outbox WHERE job_id = :id"), {"id": job_id})
            .mappings()
            .first()
        )


def client_id_of(tenant_id: uuid.UUID, booking_id: str) -> uuid.UUID:
    with tenant_context(tenant_id) as session:
        found: uuid.UUID = session.scalar(
            text("SELECT client_id FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


def set_client(tenant_id: uuid.UUID, client_id: uuid.UUID, **changes: Any) -> None:
    assignments = ", ".join(f"{key} = :{key}" for key in changes)
    with tenant_context(tenant_id) as session:
        session.execute(
            text(f"UPDATE clients SET {assignments} WHERE id = :id"), {**changes, "id": client_id}
        )


def set_times(
    tenant_id: uuid.UUID, booking_id: str, starts_at: datetime, ends_at: datetime
) -> None:
    with tenant_context(tenant_id) as session:
        session.execute(
            text("UPDATE bookings SET starts_at = :s, ends_at = :e WHERE id = :id"),
            {"s": starts_at, "e": ends_at, "id": booking_id},
        )


def jobs_for_booking(
    app_engine: Engine, tenant_id: uuid.UUID, booking_id: str
) -> list[dict[str, Any]]:
    with app_engine.begin() as conn:
        rows = conn.execute(
            text("""
            SELECT kind, dedupe_key, payload, due_at FROM jobs
            WHERE tenant_id = :t AND payload->>'booking_id' = :b
            """),
            {"t": tenant_id, "b": str(booking_id)},
        ).mappings()
        return [dict(row) for row in rows]


def clear_jobs(app_engine: Engine, tenant_id: uuid.UUID) -> None:
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM jobs WHERE tenant_id = :t"), {"t": tenant_id})


def add_worker(
    app_engine: Engine, tenant_id: uuid.UUID, role: str = "worker", locale: str | None = None
) -> uuid.UUID:
    user_id = add_user(app_engine, locale=locale)
    add_membership(tenant_id, user_id, role)
    return user_id


def email_of(user_id: uuid.UUID) -> str:
    return f"{user_id}@example.com"


def template_of(job_row: dict[str, Any]) -> str:
    payload = job_row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    template: str = payload["template"]
    return template


# ---------------------------------------------------------------------------
# 1-19: the handler, mail.send_booking
# ---------------------------------------------------------------------------


# 1. fence. Kills: ungated send (received after confirmed, out of order).
def test_received_after_confirmed_sends_nothing(people: People, app: FastAPI, ready: str) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200

    job = run_send_booking(people.a, booking_id, "booking_received")

    assert outbox_for(people.a, job.id) is None


# 2. fence. Kills: gating pending templates on status alone (ignoring expires_at).
@pytest.mark.parametrize("template", ["booking_received", "booking_request"])
def test_expired_pending_sends_nothing(
    people: People, app: FastAPI, ready: str, template: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    expire(people.a, booking_id)

    job = run_send_booking(people.a, booking_id, template)

    assert outbox_for(people.a, job.id) is None


# 3. fence. Kills: reminder not re-checking status at run time.
def test_reminder_for_cancelled_booking_sends_nothing(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200
    with tenant_context(people.a) as session:
        starts_at = session.scalar(
            text("SELECT starts_at FROM bookings WHERE id = :id"), {"id": booking_id}
        )

    job = run_send_booking(
        people.a, booking_id, "booking_reminder", starts_at=starts_at.isoformat()
    )

    assert outbox_for(people.a, job.id) is None


# 4. fence. Kills: reminder ignoring a reschedule (payload starts_at stale).
def test_reminder_with_different_starts_at_sends_nothing(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    other = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    job = run_send_booking(people.a, booking_id, "booking_reminder", starts_at=other)

    assert outbox_for(people.a, job.id) is None


# 5. fence. Kills: reminder mailed after the appointment (grace is 24h).
def test_reminder_after_start_sends_nothing(people: People, app: FastAPI, ready: str) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    with tenant_context(people.a) as session:
        row = session.execute(
            text("SELECT starts_at, ends_at FROM bookings WHERE id = :id"), {"id": booking_id}
        ).one()
    past_start = datetime.now(UTC) - timedelta(hours=1)
    past_end = past_start + (row.ends_at - row.starts_at)
    set_times(people.a, booking_id, past_start, past_end)

    job = run_send_booking(
        people.a, booking_id, "booking_reminder", starts_at=past_start.isoformat()
    )

    assert outbox_for(people.a, job.id) is None


# 6. fence, POSITIVE. Kills: string comparison of starts_at; a gate that rejects every reminder.
def test_reminder_matches_a_different_offset_of_the_same_instant(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    with tenant_context(people.a) as session:
        starts_at = session.scalar(
            text("SELECT starts_at FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    client_email = _client_email(people.a, booking_id)
    same_instant_other_offset = starts_at.astimezone(timezone(timedelta(hours=2))).isoformat()

    job = run_send_booking(
        people.a, booking_id, "booking_reminder", starts_at=same_instant_other_offset
    )

    assert subjects_sent_to(client_email) == [_subject("booking_reminder", "en")]
    assert outbox_for(people.a, job.id)["status"] == "sent"


def _client_email(tenant_id: uuid.UUID, booking_id: str) -> str:
    with tenant_context(tenant_id) as session:
        email: str = session.scalar(
            text(
                "SELECT c.email FROM clients c JOIN bookings b ON b.client_id = c.id "
                "WHERE b.id = :id"
            ),
            {"id": booking_id},
        )
    return email


def _subject(template: str, locale: str) -> str:
    from app.mail import render

    subject, _ = render(template, locale)
    return subject


# 7. fence. Kills: missing outbox 'sent' check on retry.
def test_running_the_same_job_twice_mails_once(people: People, app: FastAPI, ready: str) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client_email = _client_email(people.a, booking_id)

    job = Job(
        uuid.uuid4(),
        "email.booking",
        people.a,
        {"booking_id": str(booking_id), "template": "booking_confirmed"},
    )
    mail.send_booking(job)
    mail.send_booking(job)

    assert subjects_sent_to(client_email) == [_subject("booking_confirmed", "en")]
    assert outbox_for(people.a, job.id)["status"] == "sent"


# 8. fence. Kills: times rendered in UTC or server zone.
def test_times_render_in_the_business_zone(people: People, app: FastAPI, ready: str) -> None:
    starts_at = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # Amsterdam is CEST (+2) in October
    owner = signed_in(app, people.a, people.both)

    # Both created (and confirmed) while the business is still on its default zone, so the zone
    # change below never affects availability - only rendering.
    booking_id = make_pending(app, people.a, ready)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    booking_id2 = make_pending(app, people.a, ready, starts_at=at("11:00"))
    assert patch(owner, booking_id2, "confirmed").status_code == 200

    set_times(people.a, booking_id, starts_at, starts_at + timedelta(minutes=30))
    client_email = _client_email(people.a, booking_id)
    run_send_booking(people.a, booking_id, "booking_confirmed")
    message = mail_for(client_email)
    assert "14:00" in message["Text"]
    assert "Europe/Amsterdam" in message["Text"]

    save_setting(people.a, "timezone", "America/Sao_Paulo")
    starts_at2 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    set_times(people.a, booking_id2, starts_at2, starts_at2 + timedelta(minutes=30))
    client_email2 = _client_email(people.a, booking_id2)
    run_send_booking(people.a, booking_id2, "booking_confirmed")
    message = mail_for(client_email2)
    assert "09:00" in message["Text"]
    assert "America/Sao_Paulo" in message["Text"]

    # Winter: Amsterdam is CET (+1) in December, not the CEST (+2) offset a fixed-offset
    # conversion would keep using year-round.
    assert put_settings(owner, {"timezone": "Europe/Amsterdam"}).status_code == 200
    booking_id3 = make_pending(app, people.a, ready, starts_at=at("13:00"))
    assert patch(owner, booking_id3, "confirmed").status_code == 200
    starts_at3 = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)  # Amsterdam is CET (+1) in December
    set_times(people.a, booking_id3, starts_at3, starts_at3 + timedelta(minutes=30))
    client_email3 = _client_email(people.a, booking_id3)
    run_send_booking(people.a, booking_id3, "booking_confirmed")
    message = mail_for(client_email3)
    assert "13:00" in message["Text"]
    assert "Europe/Amsterdam" in message["Text"]


# 9. fence. Kills: ignoring client locale / hardcoded en.
def test_locale_comes_from_the_client_else_the_business(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready, locale="nl")
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client_email = _client_email(people.a, booking_id)

    run_send_booking(people.a, booking_id, "booking_confirmed")
    assert subjects_sent_to(client_email) == [_subject("booking_confirmed", "nl")]

    booking_id2 = make_pending(app, people.a, ready, starts_at=at("11:00"))  # no client locale
    save_setting(people.a, "language", "pt")
    owner2 = signed_in(app, people.a, people.both)
    assert patch(owner2, booking_id2, "confirmed").status_code == 200
    client_email2 = _client_email(people.a, booking_id2)

    run_send_booking(people.a, booking_id2, "booking_confirmed")
    assert subjects_sent_to(client_email2) == [_subject("booking_confirmed", "pt")]


# 10. fence. Kills: attachment replacing the body; ics on every template; missing charset.
def test_confirmed_mail_carries_an_ics_attachment_received_does_not(
    people: People, app: FastAPI, ready: str
) -> None:
    confirmed_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, confirmed_id, "confirmed").status_code == 200
    confirmed_email = _client_email(people.a, confirmed_id)
    run_send_booking(people.a, confirmed_id, "booking_confirmed")

    raw = raw_for(confirmed_email)
    assert "multipart/mixed" in raw
    assert 'Content-Type: text/calendar; method="PUBLISH"; charset="utf-8"' in raw
    message = mail_for(confirmed_email)
    assert message["Text"].strip() != ""
    assert "The attached calendar file" in message["Text"]

    received_id = make_pending(app, people.a, ready, starts_at=at("11:00"))
    received_email = _client_email(people.a, received_id)
    run_send_booking(people.a, received_id, "booking_received")
    received_raw = raw_for(received_email)
    assert "text/calendar" not in received_raw


# 11. fence. Kills: LF endings; local time or offset form; random UID.
def test_ics_lines_and_stamps() -> None:
    booking_id = uuid.uuid4()
    starts_at = datetime(2026, 10, 3, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    ends_at = starts_at + timedelta(minutes=30)
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

    first = mail.ics(booking_id, starts_at, ends_at, "Cut", now)
    second = mail.ics(booking_id, starts_at, ends_at, "Cut", now)

    text_ = first.decode()
    lines = text_.split("\r\n")[:-1]  # trailing empty element after the final \r\n
    assert "\n" not in text_.replace("\r\n", "")
    import re

    stamps = [
        line.split(":", 1)[1] for line in lines if line.startswith(("DTSTAMP", "DTSTART", "DTEND"))
    ]
    for stamp in stamps:
        assert re.fullmatch(r"\d{8}T\d{6}Z", stamp)
    dtstart = next(line for line in lines if line.startswith("DTSTART"))
    assert dtstart == "DTSTART:20261003T100000Z"  # 12:00+02:00 == 10:00Z
    dtend = next(line for line in lines if line.startswith("DTEND"))
    assert dtend == "DTEND:20261003T103000Z"
    assert f"UID:{booking_id}@ziftbook.com" in lines
    assert first == second  # stable UID across two calls


# 12. fence. Kills: char-based folding; missing escaping.
def test_ics_folds_by_octet_and_escapes_text() -> None:
    booking_id = uuid.uuid4()
    starts_at = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=30)
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    summary = "ã" * 120 + "A, B; C\\"

    data = mail.ics(booking_id, starts_at, ends_at, summary, now)
    text_ = data.decode()
    lines = text_.split("\r\n")
    physical_lines = [line for line in lines if line]
    for line in physical_lines:
        assert len(line.encode()) <= 75

    # Unfold: a folded line's continuation starts with a single space.
    unfolded_lines: list[str] = []
    continuation_lines = 0
    for line in lines:
        if line.startswith(" ") and unfolded_lines:
            continuation_lines += 1
            unfolded_lines[-1] += line[1:]
        elif line:
            unfolded_lines.append(line)
    assert continuation_lines >= 2  # a 120-char "ã" summary must fold across 3+ physical lines
    summary_line = next(line_ for line_ in unfolded_lines if line_.startswith("SUMMARY:"))
    escaped = "ã" * 120 + "A\\, B\\; C\\\\"
    assert summary_line == f"SUMMARY:{escaped}"


# fence. Kills: control characters (other than CR/LF) left in a TEXT value.
def test_ics_strips_control_characters_from_summary() -> None:
    booking_id = uuid.uuid4()
    starts_at = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=30)
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    summary = "Cut\x0b Hair"

    data = mail.ics(booking_id, starts_at, ends_at, summary, now)
    text_ = data.decode()
    lines = text_.split("\r\n")
    summary_line = next(line for line in lines if line.startswith("SUMMARY:"))
    assert "\x0b" not in summary_line
    assert " " not in summary_line


# 13. guard. RLS: the recipient read runs inside tenant_context(job.tenant_id).
def test_merchant_job_for_a_user_outside_the_business_sends_nothing(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200

    job = run_send_booking(people.a, booking_id, "booking_new", user_id=str(people.only_b))

    assert outbox_for(people.a, job.id) is None
    assert subjects_sent_to(email_of(people.only_b)) == []


# 14. fence. Kills: KeyError/raise leading to 5 retries and last_error.
def test_erased_client_email_sends_nothing_and_completes(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client_id = client_id_of(people.a, booking_id)
    set_client(people.a, client_id, email=None)

    job = run_send_booking(people.a, booking_id, "booking_confirmed")

    assert outbox_for(people.a, job.id) is None


# 15. fence, POSITIVE. Kills: wrong STATUS_FOR entry (silently never sends).
def test_declined_booking_sends_the_declined_template(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "declined").status_code == 200
    client_email = _client_email(people.a, booking_id)

    run_send_booking(people.a, booking_id, "booking_declined")

    assert subjects_sent_to(client_email) == [_subject("booking_declined", "en")]
    assert "text/calendar" not in raw_for(client_email)


# 16. fence, POSITIVE. Kills: wrong STATUS_FOR entry; .ics on the cancel.
def test_cancelled_booking_sends_the_cancelled_template(
    people: People, app: FastAPI, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200
    client_email = _client_email(people.a, booking_id)

    run_send_booking(people.a, booking_id, "booking_cancelled")

    assert subjects_sent_to(client_email) == [_subject("booking_cancelled", "en")]
    assert "text/calendar" not in raw_for(client_email)


# 17. guard. The outbox row's shape for a client send.
def test_outbox_row_for_a_client_send(people: People, app: FastAPI, ready: str) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    client_id = client_id_of(people.a, booking_id)

    job = run_send_booking(people.a, booking_id, "booking_confirmed")

    row = outbox_for(people.a, job.id)
    assert row["recipient_id"] == client_id
    assert row["template"] == "booking_confirmed"
    assert row["subject"] == _subject("booking_confirmed", "en")


# 18. guard. Every template in STATUS_FOR exists in every locale.
def test_every_status_for_template_exists_in_every_locale() -> None:
    from app.mail import LOCALES, render

    for template in STATUS_FOR:
        for locale in LOCALES:
            subject, body = render(template, locale)
            assert subject and body.strip()


# fence. Kills: a booking template (client or merchant) missing the service or date/time line.
def test_every_booking_template_renders_service_and_when() -> None:
    from app.mail import LOCALES, render

    values = {
        "business": "Biz",
        "service": "SVC-MARKER",
        "date": "DATE-MARKER",
        "time": "TIME-MARKER",
        "zone": "Europe/Amsterdam",
        "client": "Client Name",
        "link": "https://example.com/en",
        "expires": "EXPIRES-MARKER",  # never the date/time markers: booking_request prints it too
    }
    for template in STATUS_FOR:
        for locale in LOCALES:
            _, body = render(template, locale, values)
            assert "SVC-MARKER" in body, (template, locale)
            assert "DATE-MARKER" in body, (template, locale)
            assert "TIME-MARKER" in body, (template, locale)


# 19. guard. email.booking is registered.
def test_email_booking_is_a_registered_kind() -> None:
    assert "email.booking" in KINDS


# ---------------------------------------------------------------------------
# 20-29: the enqueue points, app/bookings.py
# ---------------------------------------------------------------------------


# 20. fence. Kills: enqueue on its own session or committed ahead of the change.
def test_a_failure_after_the_enqueue_rolls_the_job_back_with_the_change(
    people: People, app: FastAPI, ready: str, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import failing

    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    before = jobs_for_booking(app_engine, people.a, booking_id)  # booking_received, booking_request
    failing(monkeypatch, auth, "record")

    response = patch(owner, booking_id, "confirmed")

    assert response.status_code == 500
    # Nothing from the failed transition: the confirmed/reminder enqueue rolled back with it.
    assert jobs_for_booking(app_engine, people.a, booking_id) == before


# 21. fence. Kills: wrong template or job set on one branch.
def test_create_enqueues_the_right_jobs_for_auto_confirm_and_pending(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    save_setting(people.a, "auto_confirm", True)
    confirmed_id = make_pending_status(app, people.a, ready)  # actually confirmed; see helper
    jobs = jobs_for_booking(app_engine, people.a, confirmed_id)
    assert sorted(template_of(j) for j in jobs) == [
        "booking_confirmed",
        "booking_new",
        "booking_reminder",
    ]

    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"auto_confirm": False}).status_code == 200
    pending_id = make_pending(app, people.a, ready, starts_at=at("11:00"))
    pending_jobs = jobs_for_booking(app_engine, people.a, pending_id)
    assert sorted(template_of(j) for j in pending_jobs) == ["booking_received", "booking_request"]


def make_pending_status(app: FastAPI, tenant_id: uuid.UUID, service_id: str) -> str:
    response = post_booking(new_client(app), tenant_id, service_id)
    assert response.status_code == 201, response.json()
    assert response.json()["status"] == "confirmed"
    id_: str = response.json()["id"]
    return id_


# 22. fence. Kills: enqueue on any transition into confirmed/cancelled.
def test_restore_and_cancel_from_completed_enqueue_nothing(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    from tests.test_bookings_approval_api import confirmed_in_the_past

    owner = signed_in(app, people.a, people.both)
    booking_id = confirmed_in_the_past(app, owner, people.a, ready)
    assert patch(owner, booking_id, "completed").status_code == 200
    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM jobs WHERE tenant_id = :t"), {"t": people.a})

    assert patch(owner, booking_id, "confirmed").status_code == 200
    assert jobs_for_booking(app_engine, people.a, booking_id) == []

    assert patch(owner, booking_id, "completed").status_code == 200
    with tenant_context(people.a) as session:
        session.execute(text("DELETE FROM jobs WHERE tenant_id = :t"), {"t": people.a})
    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200
    assert jobs_for_booking(app_engine, people.a, booking_id) == []


# 23. fence. Kills: declined path not wired, or wired to the wrong template.
def test_declined_transition_enqueues_the_declined_template(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    clear_jobs(app_engine, people.a)  # drop the received/request jobs from create()
    assert patch(owner, booking_id, "declined").status_code == 200

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    templates = [template_of(j) for j in jobs]
    assert templates == ["booking_declined"]


# 24. fence. Kills: source filter too narrow (only from confirmed, or only from pending).
@pytest.mark.parametrize("via_confirmed", [False, True])
def test_cancelled_transition_enqueues_from_pending_and_confirmed(
    people: People, app: FastAPI, ready: str, app_engine: Engine, via_confirmed: bool
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    if via_confirmed:
        assert patch(owner, booking_id, "confirmed").status_code == 200
    clear_jobs(app_engine, people.a)

    assert patch(owner, booking_id, "cancelled_by_merchant").status_code == 200

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    templates = [template_of(j) for j in jobs]
    assert templates == ["booking_cancelled"]


# 25. fence. Kills: due_at = now; reminder mailed right after confirmation.
def test_reminder_due_at_is_24h_before_start_and_skipped_when_too_close(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    reminder = next(j for j in jobs if template_of(j) == "booking_reminder")
    with tenant_context(people.a) as session:
        starts_at = session.scalar(
            text("SELECT starts_at FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    assert reminder["due_at"] == starts_at - timedelta(hours=24)

    # A booking confirmed close to its start (< 24h out) gets no reminder job.
    near_id = make_pending(app, people.a, ready, starts_at=at("11:00"))
    near_start = datetime.now(UTC) + timedelta(hours=3)
    with tenant_context(people.a) as session:
        end = session.scalar(
            text("SELECT ends_at - starts_at FROM bookings WHERE id = :id"), {"id": near_id}
        )
    set_times(people.a, near_id, near_start, near_start + end)
    assert patch(owner, near_id, "confirmed").status_code == 200
    near_jobs = jobs_for_booking(app_engine, people.a, near_id)
    near_templates = [template_of(j) for j in near_jobs]
    assert "booking_reminder" not in near_templates


# 26. fence, POSITIVE. Kills: "all members"; owners only; business language instead of the
# user's; duplicate mail to the solo owner.
def test_merchant_recipients_are_every_owner_plus_the_assigned_worker(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    worker1 = add_worker(app_engine, people.a)
    worker2 = add_worker(app_engine, people.a)
    seed(people.a, worker1, weekdays("09:00", "17:00"))
    assign(people.a, ready, member_id(people.a, worker1))
    owner_email = email_of(people.both)  # people.both owns tenant a, locale nl (People fixture)

    make_pending(app, people.a, ready, member_id=str(member_id(people.a, worker1)))
    run_jobs()

    assert subjects_sent_to(owner_email) == [_subject("booking_request", "nl")]
    assert subjects_sent_to(email_of(worker1)) == [_subject("booking_request", "en")]
    assert subjects_sent_to(email_of(worker2)) == []

    # Solo business: the owner is also the assigned worker -> exactly one email. Guard, not a
    # fence: DISTINCT user_id in _email_merchants's query, plus the dedupe key already holding
    # user_id, rules out a duplicate before this ever runs.
    set_role(people.b, people.only_b, "owner")
    solo_service = new_service(signed_in(app, people.b, people.only_b))
    seed(people.b, people.only_b, weekdays("09:00", "17:00"))
    assign(people.b, solo_service, member_id(people.b, people.only_b))
    make_pending(app, people.b, solo_service)
    run_jobs()
    assert subjects_sent_to(email_of(people.only_b)) == [_subject("booking_request", "en")]


# 27. fence. Kills: address or name in the global jobs table (ZIF-93).
def test_enqueued_payloads_hold_only_ids_no_pii(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready, name="Secret Name", email="secret@example.com")
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    assert jobs
    for job in jobs:
        payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])
        assert set(payload) <= {"booking_id", "template", "user_id", "starts_at", "traceparent"}
        blob = json.dumps(payload)
        assert "Secret Name" not in blob
        assert "secret@example.com" not in blob


# 28. fence. See tests/test_logs_private.py's booking-email addition.


# 29. guard. Double PATCH confirmed: second is 409, one confirmed job.
def test_double_confirm_enqueues_one_confirmed_job(
    people: People, app: FastAPI, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    owner = signed_in(app, people.a, people.both)
    assert patch(owner, booking_id, "confirmed").status_code == 200
    assert patch(owner, booking_id, "confirmed").status_code == 409

    jobs = jobs_for_booking(app_engine, people.a, booking_id)
    confirmed_jobs = [j for j in jobs if template_of(j) == "booking_confirmed"]
    assert len(confirmed_jobs) == 1
