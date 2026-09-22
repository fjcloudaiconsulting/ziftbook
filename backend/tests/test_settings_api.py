"""Business settings: typed, with defaults, readable by everyone in the business and changed only by
an owner."""

from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from app.main import create_app
from tests.conftest import (
    People,
    events,
    put_settings,
    save_setting,
    saved_settings,
    set_role,
    signed_in,
)

DEFAULTS = {
    "timezone": "Europe/Amsterdam",
    "auto_confirm": False,
    "language": "en",
    "workers_edit_own_hours": False,
    "buffer_pct": 10,
    "slot_step_minutes": 15,
    "min_notice_minutes": 60,
    "booking_horizon_days": 60,
    "max_pending_per_email": 3,
    "pending_ttl_hours": 24,
    "cancellation_policy_text": "",
    "free_cancellation_hours": 48,
    "reschedule_cutoff_hours": 24,
}


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


def test_everyone_in_the_business_reads_the_defaults(people: People, app: FastAPI) -> None:
    response = signed_in(app, people.a, people.only_a).get("/api/settings")

    assert (response.status_code, response.json()) == (200, DEFAULTS)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "body", [{"auto_confirm": True}, {"auto_confirm": "yes"}, []], ids=["valid", "invalid", "list"]
)
def test_only_an_owner_changes_settings(people: People, app: FastAPI, body: Any) -> None:
    response = put_settings(signed_in(app, people.a, people.only_a), body)

    assert (response.status_code, response.json()) == (403, {"code": "owner_only"})
    assert saved_settings(people.a) == {}


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Olympus"},
        {"timezone": "europe/amsterdam"},
        {"timezone": "../../etc/passwd"},
        {"timezone": ""},
        {"timezone": 5},
        {"timezone": None},
        {"auto_confirm": "true"},
        {"auto_confirm": 1},
        {"auto_confirm": None},
        {"language": "de"},
        {"language": "PT"},
        {"language": None},
        {"nope": 1},
        {"workers_edit_own_hours": "true"},
        {"workers_edit_own_hours": 1},
        {"workers_edit_own_hours": None},
        {"buffer_pct": 101},
        {"buffer_pct": -1},
        {"buffer_pct": "10"},
        {"buffer_pct": 10.5},
        {"buffer_pct": None},
        {"slot_step_minutes": 7},
        {"slot_step_minutes": "15"},
        {"slot_step_minutes": 15.0},
        {"slot_step_minutes": True},
        {"min_notice_minutes": -1},
        {"min_notice_minutes": 10081},
        {"booking_horizon_days": 0},
        {"booking_horizon_days": 366},
        {"pending_ttl_hours": 0},
        {"pending_ttl_hours": 169},
        {"pending_ttl_hours": "24"},
        {"pending_ttl_hours": 24.0},
        {"pending_ttl_hours": None},
        [],
    ],
    ids=[
        "unknown zone",
        "lowercase zone",
        "path",
        "empty zone",
        "number zone",
        "null zone",
        "string true",
        "number one",
        "null auto_confirm",
        "unknown language",
        "uppercase language",
        "null language",
        "unknown key",
        "string true hours",
        "number one hours",
        "null hours",
        "buffer over 100",
        "negative buffer",
        "string buffer",
        "fractional buffer",
        "null buffer",
        "off-grid step",
        "string step",
        "float step",
        "boolean step",
        "negative notice",
        "notice over a week",
        "zero horizon",
        "horizon over a year",
        "zero ttl",
        "ttl over a week",
        "string ttl",
        "float ttl",
        "null ttl",
        "list body",
    ],
)
def test_a_setting_of_the_wrong_kind_is_refused(people: People, app: FastAPI, body: Any) -> None:
    response = put_settings(signed_in(app, people.a, people.both), body)

    assert (response.status_code, response.json()) == (422, {"code": "invalid_request"})
    assert saved_settings(people.a) == {}


def test_a_change_leaves_the_other_settings_as_they_were(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)

    first = put_settings(owner, {"auto_confirm": True})
    second = put_settings(owner, {"timezone": "America/Sao_Paulo"})

    expected = {**DEFAULTS, "timezone": "America/Sao_Paulo", "auto_confirm": True}
    assert (first.status_code, first.json()) == (200, {**DEFAULTS, "auto_confirm": True})
    assert (second.status_code, second.json()) == (200, expected)
    assert owner.get("/api/settings").json() == expected


def test_a_saved_setting_can_be_changed_and_set_back_to_its_default(
    people: People, app: FastAPI
) -> None:
    owner = signed_in(app, people.a, people.both)

    for changes in (
        {"timezone": "Asia/Tokyo", "auto_confirm": True},
        {"timezone": "Africa/Lagos"},
        {"timezone": "Europe/Amsterdam", "auto_confirm": False},
    ):
        response = put_settings(owner, changes)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"

    assert response.json() == DEFAULTS
    # Saving the default keeps it saved: a later change of default doesn't reach this business.
    # Only what this test wrote: the other keys were never saved.
    assert saved_settings(people.a) == {"timezone": "Europe/Amsterdam", "auto_confirm": False}


def test_each_business_has_its_own_settings(people: People, app: FastAPI) -> None:
    set_role(people.b, people.only_b, "owner")
    owner_a = signed_in(app, people.a, people.both)
    owner_b = signed_in(app, people.b, people.only_b)

    assert put_settings(owner_a, {"timezone": "Asia/Tokyo"}).status_code == 200
    assert put_settings(owner_b, {"timezone": "Africa/Lagos"}).status_code == 200

    assert owner_a.get("/api/settings").json()["timezone"] == "Asia/Tokyo"
    assert owner_b.get("/api/settings").json()["timezone"] == "Africa/Lagos"


def test_a_stored_setting_no_longer_in_the_registry_is_ignored(
    people: People, app: FastAPI
) -> None:
    save_setting(people.a, "retired", 1)
    save_setting(people.a, "auto_confirm", True)

    response = signed_in(app, people.a, people.only_a).get("/api/settings")

    assert (response.status_code, response.json()) == (200, {**DEFAULTS, "auto_confirm": True})


@pytest.mark.parametrize(
    ("key", "value"), [("auto_confirm", "yes"), ("timezone", "Mars/Olympus")], ids=["bool", "zone"]
)
def test_a_stored_setting_that_no_longer_fits_fails_loudly(
    people: People, app: FastAPI, key: str, value: str
) -> None:
    # Never the default instead: that would quietly change how the business works.
    save_setting(people.a, key, value)

    response = signed_in(app, people.a, people.only_a).get("/api/settings")

    assert response.status_code == 500


def test_an_old_timezone_name_is_kept_as_sent(people: People, app: FastAPI) -> None:
    owner = signed_in(app, people.a, people.both)

    assert (
        put_settings(owner, {"timezone": "America/Sao_Paulo"}).json()["timezone"]
        == "America/Sao_Paulo"
    )
    assert saved_settings(people.a) == {"timezone": "America/Sao_Paulo"}


def test_the_contract_requires_every_setting_back_and_none_sent() -> None:
    schemas = create_app().openapi()["components"]["schemas"]

    assert schemas["BusinessSettings-Output"]["required"] == [
        "timezone",
        "auto_confirm",
        "language",
        "workers_edit_own_hours",
        "buffer_pct",
        "slot_step_minutes",
        "min_notice_minutes",
        "booking_horizon_days",
        "max_pending_per_email",
        "pending_ttl_hours",
        "cancellation_policy_text",
        "free_cancellation_hours",
        "reschedule_cutoff_hours",
    ]
    assert "required" not in schemas["BusinessSettings-Input"]


def test_changing_the_language_is_recorded(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    response = put_settings(owner, {"language": "pt"})

    assert (response.status_code, response.json()["language"]) == (200, "pt")
    changed = [
        e["details"]
        for e in events(migrate_engine, tenant_id=people.a)
        if e["action"] == "setting_changed"
    ]
    assert changed == [{"old": "en", "new": "pt"}]


# G (T11) - the two ZIF-55 keys are in the registry with the ticket's defaults, and a change to
# each is audited like every other setting. Guard: the whole-body DEFAULTS assertions above
# already fail if either key is missing.
def test_the_cancellation_thresholds_default_to_48_and_24(people: People, app: FastAPI) -> None:
    client = signed_in(app, people.a, people.only_a)

    body = client.get("/api/settings").json()

    assert (body["free_cancellation_hours"], body["reschedule_cutoff_hours"]) == (48, 24)


def test_changing_a_cancellation_threshold_is_audited(
    people: People, app: FastAPI, migrate_engine: Engine
) -> None:
    owner = signed_in(app, people.a, people.both)

    assert (
        put_settings(
            owner, {"free_cancellation_hours": 12, "reschedule_cutoff_hours": 6}
        ).status_code
        == 200
    )

    for key in ("free_cancellation_hours", "reschedule_cutoff_hours"):
        assert (
            len(events(migrate_engine, action="setting_changed", target=f"setting:{key}")) == 1
        ), key
