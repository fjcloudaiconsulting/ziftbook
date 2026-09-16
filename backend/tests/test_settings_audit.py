"""Changing a setting is recorded with its old and new value, in the same transaction as the
change."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from app import auth
from app.db import tenant_context
from app.main import create_app
from tests.conftest import People, signed_in
from tests.test_audit_recording import events, failing
from tests.test_settings_api import put, stored


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def changes(migrate_engine: Engine, tenant_id: uuid.UUID) -> list[tuple[str, Any]]:
    """Every setting_changed event of a business: what it was about, and its old and new value."""
    return [
        (event["target"], event["details"])
        for event in events(migrate_engine, tenant_id=tenant_id, action="setting_changed")
    ]


def test_a_changed_setting_is_recorded_with_its_old_and_new_value(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put(owner, {"timezone": "Asia/Tokyo", "auto_confirm": True}).status_code == 200

    assert changes(migrate_engine, people.a) == [
        ("setting:timezone", {"old": "Europe/Amsterdam", "new": "Asia/Tokyo"}),
        ("setting:auto_confirm", {"old": False, "new": True}),
    ]
    actors = {e["actor_user_id"] for e in events(migrate_engine, tenant_id=people.a)}
    assert actors == {people.both}


def test_a_second_change_records_what_it_replaced(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put(owner, {"timezone": "Asia/Tokyo"}).status_code == 200
    assert put(owner, {"timezone": "Africa/Lagos"}).status_code == 200

    assert changes(migrate_engine, people.a) == [
        ("setting:timezone", {"old": "Europe/Amsterdam", "new": "Asia/Tokyo"}),
        ("setting:timezone", {"old": "Asia/Tokyo", "new": "Africa/Lagos"}),
    ]


def test_a_setting_saved_as_it_already_was_is_not_recorded(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put(owner, {"timezone": "Asia/Tokyo"}).status_code == 200
    # The same timezone again, alongside a change: only the change is worth an event.
    assert put(owner, {"timezone": "Asia/Tokyo", "auto_confirm": True}).status_code == 200

    assert changes(migrate_engine, people.a) == [
        ("setting:timezone", {"old": "Europe/Amsterdam", "new": "Asia/Tokyo"}),
        ("setting:auto_confirm", {"old": False, "new": True}),
    ]


def test_a_change_whose_event_fails_is_not_saved(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signed_in(app, people.a, people.both)
    failing(monkeypatch, auth, "record")

    assert put(owner, {"timezone": "Asia/Tokyo"}).status_code == 500

    assert stored(people.a) == {}
    assert changes(migrate_engine, people.a) == []


def test_an_owner_reads_what_changed_in_their_log(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    assert put(owner, {"auto_confirm": True}).status_code == 200

    log = owner.get("/api/audit-events").json()

    assert [(e["action"], e["target"], e["details"]) for e in log] == [
        ("setting_changed", "setting:auto_confirm", {"old": False, "new": True})
    ]


def test_events_without_details_keep_none(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    # SQL NULL, not a JSON null: the column is empty for every event that carries no values.
    with tenant_context(people.a) as session:
        session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(people.a)})
        auth.record(
            session,
            _request(),
            "signed_out_everywhere",
            actor_user_id=people.both,
        )

    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        assert (
            conn.scalar(
                text("""
            SELECT count(*) FROM audit_events
            WHERE tenant_id = :t AND details IS NOT NULL
            """),
                {"t": people.a},
            )
            == 0
        )


def _request() -> Any:
    """The little of a request that record() reads."""

    class Client:
        host = "192.0.2.1"

    class Request:
        client = Client()
        headers: dict[str, str] = {}

    return Request()
