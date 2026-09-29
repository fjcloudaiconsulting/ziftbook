"""GET /api/public/booking-pages/{slug} (ZIF-56): public, no session."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, text

from app import clients, limits
from app.db import tenant_context
from app.main import create_app
from tests.conftest import (
    People,
    add_membership,
    add_user,
    email_of,
    events,
    fresh_address,
    member_id,
    new_client,
    put_settings,
    save_setting,
    signed_in,
)
from tests.test_availability_api import assign, get, new_service, set_display_name, weekdays
from tests.test_bookings_api import post_booking
from tests.test_working_hours import seed


@pytest.fixture
def app(people: People) -> FastAPI:
    return create_app()


@pytest.fixture
def owner(app: FastAPI, people: People) -> TestClient:
    return signed_in(app, people.a, people.both)


@pytest.fixture(autouse=True)
def _published(people: People) -> None:
    """ZIF-145: every test in this file exercises the public booking page, gated on published.
    Seeded once, here, rather than in every test; a fence that needs an unpublished business (F1)
    uses its own fresh tenant instead of people.a, and one that flips it (F4/F5) does so through
    the owner PUT, never a second save_setting."""
    save_setting(people.a, "published", True)


def slug_of(app_engine: Engine, tenant_id: uuid.UUID) -> str:
    with app_engine.connect() as conn:
        slug: str = conn.scalar(text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id})
    return slug


# F1. fence
def test_an_unpublished_business_is_byte_identical_to_an_unknown_slug(
    app: FastAPI, migrate_engine: Engine
) -> None:
    """A brand-new business (no settings row: published defaults False) answers exactly like a
    slug nobody owns. Kills: published defaulting True, or a distinct 403/410/`unpublished` code
    that would tell a prober the slug exists."""
    tag = uuid.uuid4().hex[:8]
    with migrate_engine.begin() as conn:
        tenant = conn.scalar(
            text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"), {"n": f"Fresh Biz {tag}"}
        )
    slug = slug_of(migrate_engine, tenant)
    client = new_client(app)
    try:
        unpublished = page(client, slug)
        unknown = page(client, f"nobody-here-{tag}")
        assert unpublished.status_code == 404
        assert (unpublished.status_code, unpublished.content) == (
            unknown.status_code,
            unknown.content,
        )
        for header in ("content-type", "cache-control"):
            assert unpublished.headers.get(header) == unknown.headers.get(header)
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


def page(client: TestClient, slug: str) -> Response:
    return client.get(f"/api/public/booking-pages/{slug}")


@pytest.fixture
def slug(people: People, app_engine: Engine) -> str:
    return slug_of(app_engine, people.a)


@pytest.fixture
def ready(people: People, owner: TestClient) -> str:
    """A 30-minute service performed by the owner, who works 09:00-17:00 every day."""
    service_id = new_service(owner)
    seed(people.a, people.both, weekdays("09:00", "17:00"))
    assign(people.a, service_id, member_id(people.a, people.both))
    return service_id


# 13. guard
def test_the_page_answers_exactly_the_documented_fields(
    people: People, app: FastAPI, owner: TestClient, slug: str, ready: str
) -> None:
    seed(people.a, people.only_a, weekdays("09:00", "12:00"))
    second = new_service(owner, name={"en": "Colour", "nl": "Kleur"}, description={"en": "Long"})
    assign(people.a, second, member_id(people.a, people.both), member_id(people.a, people.only_a))

    response = page(new_client(app), slug)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "slug",
        "name",
        "timezone",
        "language",
        "booking_horizon_days",
        "policy_version",
        "cancellation",
        "services",
        "auto_confirm",
    }
    assert (body["id"], body["slug"], body["name"]) == (str(people.a), slug, "a")
    assert set(body["cancellation"]) == {
        "text",
        "free_cancellation_hours",
        "reschedule_cutoff_hours",
        "max_reschedules",
    }
    assert [s["id"] for s in body["services"]] == sorted([ready, second])
    for service in body["services"]:
        assert set(service) == {"id", "name", "description", "duration_minutes", "price", "workers"}
        assert set(service["price"]) == {"amount_minor", "currency"}
        for worker in service["workers"]:
            assert set(worker) == {"id", "display_name"}
    by_id = {s["id"]: s for s in body["services"]}
    assert by_id[second]["name"] == {"en": "Colour", "nl": "Kleur"}
    assert by_id[second]["description"] == {"en": "Long"}
    assert by_id[ready]["description"] == {}
    assert by_id[ready]["price"] == {"amount_minor": 2500, "currency": "EUR"}
    assert len(by_id[second]["workers"]) == 2


# F4/F5. fence. Kills: a one-way latch (publishing once can never be undone); an ungated PUT (a
# worker can flip it); a missing or misshapen audit row.
def test_publishing_gates_all_three_routes_and_only_an_owner_may_flip_it(
    people: People, app: FastAPI, owner: TestClient, slug: str, ready: str, migrate_engine: Engine
) -> None:
    worker = signed_in(app, people.a, people.only_a)
    client = new_client(app)

    assert page(client, slug).status_code == 200  # this file's autouse already published people.a

    assert put_settings(owner, {"published": False}).status_code == 200
    assert page(client, slug).status_code == 404

    denied = put_settings(worker, {"published": True})

    assert (denied.status_code, denied.json()) == (403, {"code": "owner_only"})
    assert page(client, slug).status_code == 404  # the refused PUT wrote nothing
    assert get(client, people.a, ready).status_code == 404
    assert post_booking(client, people.a, ready).status_code == 404

    turned_on = put_settings(owner, {"published": True})

    assert turned_on.status_code == 200
    assert page(client, slug).status_code == 200
    assert get(client, people.a, ready).status_code == 200
    assert post_booking(client, people.a, ready).status_code == 201

    assert put_settings(owner, {"published": False}).status_code == 200
    assert page(client, slug).status_code == 404  # not a one-way latch

    changed = [
        e["details"]
        for e in events(migrate_engine, tenant_id=people.a)
        if e["action"] == "setting_changed" and e["target"] == "setting:published"
    ]
    assert changed == [
        {"old": True, "new": False},
        {"old": False, "new": True},
        {"old": True, "new": False},
    ]


# F9. fence
@pytest.mark.parametrize("auto_confirm", [True, False])
def test_auto_confirm_reflects_the_setting(
    people: People, app: FastAPI, owner: TestClient, slug: str, ready: str, auto_confirm: bool
) -> None:
    assert put_settings(owner, {"auto_confirm": auto_confirm}).status_code == 200
    assert page(new_client(app), slug).json()["auto_confirm"] is auto_confirm


# 14. fence
def test_an_archived_service_is_not_listed(
    app: FastAPI, owner: TestClient, slug: str, ready: str
) -> None:
    assert owner.patch(f"/api/services/{ready}", json={"archived": True}).status_code == 200
    assert page(new_client(app), slug).json()["services"] == []


# 15. fence
def test_only_workers_with_hours_are_listed_and_a_service_nobody_can_do_is_not(
    people: People, app: FastAPI, owner: TestClient, slug: str, ready: str
) -> None:
    new_service(owner, name={"en": "Nobody"})  # no worker assigned
    idle = new_service(owner, name={"en": "Idle"})
    assign(people.a, idle, member_id(people.a, people.only_a))  # no working hours
    assign(people.a, ready, member_id(people.a, people.only_a))

    services = page(new_client(app), slug).json()["services"]

    assert [s["id"] for s in services] == [ready]
    assert [w["id"] for w in services[0]["workers"]] == [str(member_id(people.a, people.both))]


# 16. fence
def test_workers_are_ordered_by_display_name_nulls_last(
    people: People, app: FastAPI, app_engine: Engine, owner: TestClient, slug: str, ready: str
) -> None:
    extra = [add_worker(people.a, app_engine) for _ in range(2)]
    members = [member_id(people.a, people.both), *extra]
    assign(people.a, ready, *extra)
    # Id order is Bea, null, Ana: neither id order nor nulls first gives Ana, Bea, null.
    ordered_ids = sorted(members)
    set_display_name(people.a, ordered_ids[0], "Bea")
    set_display_name(people.a, ordered_ids[1], None)
    set_display_name(people.a, ordered_ids[2], "Ana")

    workers = page(new_client(app), slug).json()["services"][0]["workers"]

    assert [w["display_name"] for w in workers] == ["Ana", "Bea", None]


def add_worker(tenant_id: uuid.UUID, app_engine: Engine) -> uuid.UUID:
    user = add_user(app_engine)
    add_membership(tenant_id, user)
    seed(tenant_id, user, weekdays("09:00", "12:00"))
    return member_id(tenant_id, user)


# 17. fence
def test_no_member_email_or_user_id_leaks(
    people: People, app: FastAPI, owner: TestClient, slug: str, ready: str
) -> None:
    seed(people.a, people.only_a, weekdays("09:00", "12:00"))
    assign(people.a, ready, member_id(people.a, people.only_a))

    response = page(new_client(app), slug)

    for user in (people.both, people.only_a):
        assert email_of(user) not in response.text
        assert str(user) not in response.text


# 18. fence
def test_another_business_s_services_are_not_listed(
    people: People, app: FastAPI, app_engine: Engine, slug: str, ready: str
) -> None:
    with tenant_context(people.b) as session:
        session.execute(
            text("UPDATE memberships SET role = 'owner' WHERE user_id = :u"), {"u": people.only_b}
        )
    b_owner = signed_in(app, people.b, people.only_b)
    b_service = new_service(b_owner, name={"en": "B only"})
    seed(people.b, people.only_b, weekdays("09:00", "12:00"))
    assign(people.b, b_service, member_id(people.b, people.only_b))
    save_setting(people.b, "published", True)

    a_page = page(new_client(app), slug).json()
    b_page = page(new_client(app), slug_of(app_engine, people.b)).json()

    assert [s["id"] for s in a_page["services"]] == [ready]
    assert [s["id"] for s in b_page["services"]] == [b_service]


# 19. fence
@pytest.mark.parametrize(
    "wrong", ["nope-{tag}", "a--b", "a%20b", "a_b", "-ab", "ab", "x" * 41, "api", "API"]
)
def test_every_wrong_slug_is_the_same_404(app: FastAPI, wrong: str) -> None:
    response = page(new_client(app), wrong.format(tag=uuid.uuid4().hex[:8]))
    assert (response.status_code, response.content) == (404, b'{"code":"not_found"}')


# 19b. fence — review nit: Python's str.lower() is Unicode-aware, so a non-ASCII letter can fold
# to an ASCII one under a different code point (the Kelvin sign U+212A -> "k"), aliasing a real
# slug. Kills checking isascii() after lower() instead of before, or not at all.
def test_a_non_ascii_look_alike_does_not_alias_a_real_slug(
    app: FastAPI, migrate_engine: Engine
) -> None:
    tag = uuid.uuid4().hex[:8]
    with migrate_engine.begin() as conn:
        tenant = conn.scalar(
            text("INSERT INTO tenants (name, slug) VALUES ('Karen', :s) RETURNING id"),
            {"s": f"karen-{tag}"},
        )
    try:
        response = page(new_client(app), f"Karen-{tag}")
        assert (response.status_code, response.content) == (404, b'{"code":"not_found"}')
    finally:
        with migrate_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


# 20. fence
def test_the_slug_is_case_insensitive(
    app: FastAPI, app_engine: Engine, people: People, migrate_engine: Engine
) -> None:
    tag = uuid.uuid4().hex[:8]
    with migrate_engine.begin() as conn:
        tenant = conn.scalar(
            text("INSERT INTO tenants (name) VALUES (:n) RETURNING id"),
            {"n": f"Salão da Ana {tag}"},
        )
    save_setting(tenant, "published", True)
    try:
        response = page(new_client(app), f"Salao-Da-Ana-{tag}")
        assert response.status_code == 200
        assert response.json()["slug"] == f"salao-da-ana-{tag}"
    finally:
        with migrate_engine.begin() as conn:
            # save_setting wrote a settings row for this tenant; the foreign key needs it gone
            # first.
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
            conn.execute(text("DELETE FROM settings"))
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})


def hits(migrate_engine: Engine, key: str) -> int | None:
    with migrate_engine.connect() as conn:
        found: int | None = conn.scalar(
            text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key}
        )
    return found


# 21. fence
def test_the_61st_page_load_in_a_minute_is_limited_even_for_unknown_slugs(
    app: FastAPI, migrate_engine: Engine, slug: str
) -> None:
    address = fresh_address()
    client = new_client(app, address)
    for _ in range(60):
        assert page(client, slug).status_code == 200
    assert page(client, "no-such-business").json() == {"code": "rate_limited"}
    assert page(client, "no-such-business").status_code == 429
    assert hits(migrate_engine, limits.ip_key("availability", address)) is None


# 21b. fence — review nit: the limit is per IP, not a shared bucket. Kills a global key.
def test_the_limit_is_per_ip_not_global(app: FastAPI, slug: str) -> None:
    exhausted = new_client(app, fresh_address())
    for _ in range(60):
        assert page(exhausted, slug).status_code == 200
    assert page(exhausted, slug).status_code == 429

    fresh = new_client(app, fresh_address())
    assert page(fresh, slug).status_code == 200


# 21c. fence — review nit: the limiter runs before slug format validation. Kills a limiter moved
# after the format check (a malformed slug would 404 instead of 429 on the 61st request).
def test_the_61st_request_is_limited_even_with_a_malformed_slug(app: FastAPI, slug: str) -> None:
    address = fresh_address()
    client = new_client(app, address)
    for _ in range(60):
        assert page(client, slug).status_code == 200
    response = page(client, "a--b")
    assert response.status_code == 429
    assert response.json() == {"code": "rate_limited"}


# 22. fence
def test_the_cancellation_terms_are_the_saved_settings(
    app: FastAPI, owner: TestClient, slug: str
) -> None:
    assert page(new_client(app), slug).json()["cancellation"]["text"] is None
    saved = {
        "cancellation_policy_text": "Cancel 2 days ahead.",
        "free_cancellation_hours": 12,
        "reschedule_cutoff_hours": 6,
        "max_reschedules": 5,
    }
    assert put_settings(owner, saved).status_code == 200

    assert page(new_client(app), slug).json()["cancellation"] == {
        "text": "Cancel 2 days ahead.",
        "free_cancellation_hours": 12,
        "reschedule_cutoff_hours": 6,
        "max_reschedules": 5,
    }
    assert put_settings(owner, {"cancellation_policy_text": ""}).status_code == 200
    assert page(new_client(app), slug).json()["cancellation"]["text"] is None


# 23. fence
def test_the_policy_version_is_one_a_booking_accepts(
    people: People, app: FastAPI, slug: str, ready: str
) -> None:
    version = page(new_client(app), slug).json()["policy_version"]
    response = post_booking(new_client(app), people.a, ready, policy_version=version)
    assert response.status_code == 201, response.content


# 23b. fence — review nit: the offered version is the newest one, max(CONSENT_TEXTS). Kills min().
def test_the_policy_version_is_the_newest_one(
    app: FastAPI, slug: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched = dict(clients.CONSENT_TEXTS) | {"9999-12-31": clients.CONSENT_TEXTS["2026-09-01"]}
    monkeypatch.setattr(clients, "CONSENT_TEXTS", patched)
    assert page(new_client(app), slug).json()["policy_version"] == "9999-12-31"


# 24. guard
def test_language_timezone_and_horizon_follow_saved_settings(
    app: FastAPI, owner: TestClient, slug: str
) -> None:
    changes = {"language": "pt", "timezone": "America/Sao_Paulo", "booking_horizon_days": 30}
    assert put_settings(owner, changes).status_code == 200
    body = page(new_client(app), slug).json()
    assert (body["language"], body["timezone"], body["booking_horizon_days"]) == (
        "pt",
        "America/Sao_Paulo",
        30,
    )


# 25. guard
def test_a_business_with_no_bookable_service_answers_an_empty_list(app: FastAPI, slug: str) -> None:
    response = page(new_client(app), slug)
    assert (response.status_code, response.json()["services"]) == (200, [])


# 26. guard
def test_every_answer_is_no_store(app: FastAPI, slug: str) -> None:
    client = new_client(app)
    assert page(client, slug).headers.get("cache-control") == "no-store"
    assert page(client, "no-such-business").headers.get("cache-control") == "no-store"
    assert page(client, "a--b").headers.get("cache-control") == "no-store"
    for _ in range(58):
        page(client, "no-such-business")
    limited = page(client, "no-such-business")
    assert limited.status_code == 429
    assert limited.headers.get("cache-control") == "no-store"
