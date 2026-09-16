"""Changing a setting is recorded with its old and new value, in the same transaction as the
change."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text

from app import auth, business_settings
from app.main import create_app
from tests.conftest import People, events, failing, put_settings, saved_settings, signed_in


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

    assert put_settings(owner, {"timezone": "Asia/Tokyo", "auto_confirm": True}).status_code == 200

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

    assert put_settings(owner, {"timezone": "Asia/Tokyo"}).status_code == 200
    assert put_settings(owner, {"timezone": "Africa/Lagos"}).status_code == 200

    assert changes(migrate_engine, people.a) == [
        ("setting:timezone", {"old": "Europe/Amsterdam", "new": "Asia/Tokyo"}),
        ("setting:timezone", {"old": "Asia/Tokyo", "new": "Africa/Lagos"}),
    ]


def test_a_setting_saved_as_it_already_was_is_not_recorded(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert put_settings(owner, {"timezone": "Asia/Tokyo"}).status_code == 200
    # The same timezone again, alongside a change: only the change is worth an event.
    assert put_settings(owner, {"timezone": "Asia/Tokyo", "auto_confirm": True}).status_code == 200

    assert changes(migrate_engine, people.a) == [
        ("setting:timezone", {"old": "Europe/Amsterdam", "new": "Asia/Tokyo"}),
        ("setting:auto_confirm", {"old": False, "new": True}),
    ]


def test_a_change_whose_event_fails_is_not_saved(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = signed_in(app, people.a, people.both)
    failing(monkeypatch, auth, "record")

    assert put_settings(owner, {"timezone": "Asia/Tokyo"}).status_code == 500

    assert saved_settings(people.a) == {}
    assert changes(migrate_engine, people.a) == []


def test_an_owner_reads_what_changed_in_their_log(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)
    assert put_settings(owner, {"auto_confirm": True}).status_code == 200

    log = owner.get("/api/audit-events").json()

    assert [(e["action"], e["target"], e["details"]) for e in log] == [
        ("setting_changed", "setting:auto_confirm", {"old": False, "new": True})
    ]


def test_an_event_that_changed_nothing_has_no_details_at_all(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    # SQL NULL, not a JSON null: json.dumps(None) would store the string "null" instead.
    owner = signed_in(app, people.a, people.both)

    assert owner.request("DELETE", "/api/sessions", json={}).status_code == 204

    with migrate_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.audit_review = 'on'"))
        empty = conn.scalar(
            text("""
            SELECT count(*) FROM audit_events
            WHERE tenant_id = :t AND action = 'signed_out_everywhere' AND details IS NULL
            """),
            {"t": people.a},
        )
    assert empty == 1


def test_a_change_that_fails_after_its_event_records_nothing(
    people: People, app: FastAPI, migrate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # read() runs after the events are written, so only this direction catches an event committed
    # on its own: the change is gone, and its event must be gone with it.
    owner = signed_in(app, people.a, people.both)
    failing(monkeypatch, business_settings, "read")

    assert put_settings(owner, {"timezone": "Asia/Tokyo"}).status_code == 500

    assert saved_settings(people.a) == {}
    assert changes(migrate_engine, people.a) == []
