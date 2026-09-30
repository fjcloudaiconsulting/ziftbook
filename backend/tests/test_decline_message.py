"""ZIF-121: a decline may carry a message the client receives in the decline email. See
docs/specs/2026-09-29-zif-121-spec.md for what each test protects (F = fence, G = guard)."""

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app import mail
from app.db import tenant_context
from tests.conftest import People, events, member_id, save_setting, signed_in
from tests.test_availability_api import assign, new_service, weekdays
from tests.test_booking_email import (
    _client_email,
    _subject,
    clear_jobs,
    jobs_for_booking,
    outbox_for,
    run_jobs,
    run_send_booking,
    subjects_sent_to,
    texts_to,
)
from tests.test_bookings_api import at
from tests.test_bookings_approval_api import make_pending, status_of
from tests.test_working_hours import seed

LABELS = {"en": "wrote:", "nl": "schreef:", "pt": "escreveu:"}


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


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    save_setting(people.a, "published", True)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


def decline(owner: TestClient, booking_id: str, **body: Any) -> Any:
    return owner.patch(f"/api/bookings/{booking_id}", json={"status": "declined", **body})


def message_of(tenant_id: uuid.UUID, booking_id: str) -> str | None:
    with tenant_context(tenant_id) as session:
        found: str | None = session.scalar(
            text("SELECT decline_message FROM bookings WHERE id = :id"), {"id": booking_id}
        )
    return found


def body_of(tenant_id: uuid.UUID, booking_id: str) -> str:
    (body,) = texts_to(_client_email(tenant_id, booking_id))
    return body.replace("\r\n", "\n")


# F1. Kills: accepted but not persisted, or persisted unstripped.
def test_a_decline_stores_the_trimmed_message(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    response = decline(owner, booking_id, message="  Sorry, fully booked  ")

    assert response.status_code == 200
    assert message_of(people.a, booking_id) == "Sorry, fully booked"


# F2. Kills: the message dropped silently, or stored, on a non-decline.
@pytest.mark.parametrize("status", ["confirmed", "no_show", "cancelled_by_merchant", "completed"])
def test_a_message_with_any_other_status_is_refused(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine, status: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    clear_jobs(app_engine, people.a)

    response = owner.patch(
        f"/api/bookings/{booking_id}", json={"status": status, "message": "Sorry"}
    )

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert status_of(people.a, booking_id) == "pending"
    assert message_of(people.a, booking_id) is None
    assert jobs_for_booking(app_engine, people.a, booking_id) == []


# F3. Kills: a missing strip, cap or multiline rule.
@pytest.mark.parametrize(
    "message", ["   ", "a" * 1001, "a\rb", "a\x00b"], ids=["blank", "1001", "cr", "nul"]
)
def test_a_bad_message_is_refused_and_nothing_is_written(
    people: People, app: FastAPI, owner: TestClient, ready: str, message: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    response = decline(owner, booking_id, message=message)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert status_of(people.a, booking_id) == "pending"
    assert message_of(people.a, booking_id) is None


def test_exactly_a_thousand_characters_is_accepted(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)

    assert decline(owner, booking_id, message="a" * 1000).status_code == 200
    assert message_of(people.a, booking_id) == "a" * 1000


# F4 (guard). A re-decline is 409 and keeps the text.
def test_declining_twice_keeps_the_first_message(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    assert decline(owner, booking_id, message="First").status_code == 200

    second = decline(owner, booking_id, message="Second")

    assert (second.status_code, second.json()) == (409, {"code": "invalid_transition"})
    assert message_of(people.a, booking_id) == "First"


# F5. Kills: the text riding in the job payload or its dedupe key.
def test_the_job_carries_ids_only_never_the_message(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    clear_jobs(app_engine, people.a)

    assert decline(owner, booking_id, message="Zzyzx secret words").status_code == 200

    (job,) = jobs_for_booking(app_engine, people.a, booking_id)
    payload = job["payload"] if isinstance(job["payload"], dict) else json.loads(job["payload"])
    assert set(payload) == {"booking_id", "template", "traceparent"}
    assert payload["template"] == "booking_declined"
    assert "Zzyzx" not in json.dumps(payload)
    assert "Zzyzx" not in job["dedupe_key"]


# F6. Kills: the payload read instead of the column, or a variant not wired in one locale.
@pytest.mark.parametrize("locale", ["en", "nl", "pt"])
def test_the_email_carries_the_message_and_its_label_in_every_locale(
    people: People, app: FastAPI, owner: TestClient, ready: str, locale: str
) -> None:
    booking_id = make_pending(app, people.a, ready, locale=locale)
    assert decline(owner, booking_id, message="Closed that day, sorry").status_code == 200
    client_email = _client_email(people.a, booking_id)

    run_send_booking(people.a, booking_id, "booking_declined")

    body = body_of(people.a, booking_id)
    assert "> Closed that day, sorry" in body
    assert LABELS[locale] in body
    assert subjects_sent_to(client_email) == [_subject("booking_declined", locale)]


# F7. Kills: the variant chosen unconditionally.
def test_no_message_renders_the_plain_template(
    people: People, app: FastAPI, owner: TestClient, ready: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    booking_id = make_pending(app, people.a, ready)
    assert decline(owner, booking_id).status_code == 200
    renders: list[tuple[str, str, dict[str, str]]] = []
    sent: list[str] = []
    real_render = mail.render

    def spy_render(template: str, locale: str, values: dict[str, str] | None = None) -> Any:
        renders.append((template, locale, dict(values or {})))
        return real_render(template, locale, values)

    monkeypatch.setattr(mail, "render", spy_render)
    monkeypatch.setattr(mail, "deliver", lambda *args, **kwargs: sent.append(args[3]))

    run_send_booking(people.a, booking_id, "booking_declined")

    ((template, locale, values),) = renders
    assert template == "booking_declined"
    assert sent == [real_render("booking_declined", locale, values)[1]]


# F8. Kills: rendering from anything but a send-time read of the column.
def test_the_email_carries_the_message_as_it_is_at_send_time(
    people: People, app: FastAPI, owner: TestClient, ready: str, app_engine: Engine
) -> None:
    booking_id = make_pending(app, people.a, ready)
    clear_jobs(app_engine, people.a)  # drop the received/request jobs from create()
    assert decline(owner, booking_id, message="Old words").status_code == 200
    with tenant_context(people.a) as session:
        session.execute(
            text("UPDATE bookings SET decline_message = 'New words' WHERE id = :id"),
            {"id": booking_id},
        )

    run_jobs()  # the job decline enqueued, not one built by hand

    body = body_of(people.a, booking_id)
    assert "> New words" in body
    assert "Old words" not in body


# F9. Kills: a second Template pass over the value, or the message reaching the headers.
def test_a_hostile_message_arrives_verbatim_and_headers_stay(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    hostile = "$link ${business}\nSubject: x"
    assert decline(owner, booking_id, message=hostile).status_code == 200
    client_email = _client_email(people.a, booking_id)

    run_send_booking(people.a, booking_id, "booking_declined")

    assert "> $link ${business}\n> Subject: x" in body_of(people.a, booking_id)
    assert subjects_sent_to(client_email) == [_subject("booking_declined", "en")]


# F10. Kills: the message in audit details or in a log line.
def test_the_message_reaches_no_audit_row_and_no_log(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    migrate_engine: Engine,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    private = f"Zzyzx {uuid.uuid4().hex}"  # audit_events is append-only: earlier runs stay
    booking_id = make_pending(app, people.a, ready)
    assert decline(owner, booking_id, message=private).status_code == 200
    run_send_booking(people.a, booking_id, "booking_declined")

    (audit,) = events(migrate_engine, action="booking_declined", target=f"booking:{booking_id}")
    assert audit["details"] is None
    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        assert (
            conn.scalar(
                text("SELECT count(*) FROM audit_events WHERE audit_events::text LIKE :m"),
                {"m": f"%{private}%"},
            )
            == 0
        )
    lines = log_lines()
    assert any(line["msg"] == "email sent" for line in lines)  # positive control
    assert private not in json.dumps(lines)


# F13. Kills: the message substituted unquoted, or blank runs not collapsed.
def test_the_message_is_quoted_with_blank_runs_collapsed(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    booking_id = make_pending(app, people.a, ready)
    assert decline(owner, booking_id, message="ok\n\n\n\nziftbook\n--\nfake").status_code == 200

    run_send_booking(people.a, booking_id, "booking_declined")

    assert "> ok\n>\n> ziftbook\n> --\n> fake" in body_of(people.a, booking_id)


# F12. Kills: the outbox row or the "email sent" log keyed by the variant.
def test_the_outbox_and_the_log_keep_the_plain_template_name(
    people: People,
    app: FastAPI,
    owner: TestClient,
    ready: str,
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    booking_id = make_pending(app, people.a, ready)
    assert decline(owner, booking_id, message="Sorry").status_code == 200

    job = run_send_booking(people.a, booking_id, "booking_declined")

    assert outbox_for(people.a, job.id)["template"] == "booking_declined"
    sent = [line["template"] for line in log_lines() if line["msg"] == "email sent"]
    assert sent == ["booking_declined"]


# G4. A null message is no message, with any status.
def test_a_null_message_is_no_message(
    people: People, app: FastAPI, owner: TestClient, ready: str
) -> None:
    confirmed = make_pending(app, people.a, ready)
    declined = make_pending(app, people.a, ready, starts_at=at("11:00"))

    response = owner.patch(
        f"/api/bookings/{confirmed}", json={"status": "confirmed", "message": None}
    )
    assert (response.status_code, response.json()["status"]) == (200, "confirmed")
    assert decline(owner, declined, message=None).status_code == 200
    assert message_of(people.a, declined) is None

    run_send_booking(people.a, declined, "booking_declined")
    assert LABELS["en"] not in body_of(people.a, declined)
