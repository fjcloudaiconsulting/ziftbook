"""The visitor's address, as ZIF_TRUSTED_PROXIES and uvicorn's ProxyHeadersMiddleware decide it."""

import uuid

import pytest
from fastapi import FastAPI, Request
from sqlalchemy import Engine, text
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.main import create_app
from tests.conftest import events, fresh_address, fresh_email, new_client


def app_trusting(monkeypatch: pytest.MonkeyPatch, trusted: str | None) -> FastAPI:
    if trusted is None:
        monkeypatch.delenv("ZIF_TRUSTED_PROXIES", raising=False)
    else:
        monkeypatch.setenv("ZIF_TRUSTED_PROXIES", trusted)
    app = create_app()

    # A tag is required: operation_id uses route.tags[0].
    @app.get("/api/test/client", tags=["test"])
    def client_host(request: Request) -> str | None:
        return request.client.host if request.client else None

    return app


def seen(app: FastAPI, peer: str, headers: dict[str, str]) -> str:
    host: str = new_client(app, peer).get("/api/test/client", headers=headers).json()
    return host


def test_an_untrusted_peers_headers_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = fresh_address()
    app = app_trusting(monkeypatch, fresh_address())  # trusted = someone else

    seen_as = seen(
        app,
        peer,
        {
            "X-Forwarded-For": "198.51.100.1",
            "Forwarded": "for=198.51.100.2",
            "X-Real-IP": "198.51.100.3",
        },
    )

    assert seen_as == peer


def test_with_no_trusted_proxies_the_peer_is_believed(monkeypatch: pytest.MonkeyPatch) -> None:
    app = app_trusting(monkeypatch, None)

    seen_as = seen(app, "127.0.0.1", {"X-Forwarded-For": "198.51.100.1"})

    assert seen_as == "127.0.0.1"


def test_a_trusted_peers_forwarded_address_is_believed(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = fresh_address()
    app = app_trusting(monkeypatch, peer)

    seen_as = seen(app, peer, {"X-Forwarded-For": "203.0.113.7"})

    assert seen_as == "203.0.113.7"


def test_the_rightmost_untrusted_entry_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = fresh_address()
    app = app_trusting(monkeypatch, peer)

    seen_as = seen(app, peer, {"X-Forwarded-For": "203.0.113.7, 203.0.113.8"})

    assert seen_as == "203.0.113.8"


@pytest.mark.parametrize(
    "forwarded", ["2001:db8::5", "[2001:db8::5]:443"], ids=["bare", "bracketed_with_port"]
)
def test_an_ipv6_forwarded_address_is_kept_whole(
    monkeypatch: pytest.MonkeyPatch, forwarded: str
) -> None:
    peer = fresh_address()
    app = app_trusting(monkeypatch, peer)

    seen_as = seen(app, peer, {"X-Forwarded-For": forwarded})

    assert seen_as == "2001:db8::5"


def test_an_unparseable_forwarded_value_is_stored_as_no_ip(
    monkeypatch: pytest.MonkeyPatch, bound: None, migrate_engine: Engine
) -> None:
    peer = fresh_address()
    user_agent = f"zif82-{uuid.uuid4()}"
    # Every run of this test shares "sign_in:ip:unknown" (a garbage value never parses); clear it
    # first as the migrate role, since the app role's 50/h limit would otherwise fail the test.
    with migrate_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE key = 'sign_in:ip:unknown'"))
    app = app_trusting(monkeypatch, peer)
    headers = {"X-Forwarded-For": "garbage", "User-Agent": user_agent}

    seen_as = seen(app, peer, headers)
    new_client(app, peer).post(
        "/api/session",
        json={"email": fresh_email(), "password": "x"},
        headers=headers,
    )

    assert seen_as == "garbage"
    [event] = events(migrate_engine, action="sign_in_failed", user_agent=user_agent)
    assert event["ip"] is None


@pytest.mark.parametrize("trusted", ["*", "frontend", "10.42.0.1/16"])
def test_an_invalid_trusted_proxies_entry_stops_startup(
    monkeypatch: pytest.MonkeyPatch, trusted: str
) -> None:
    monkeypatch.setenv("ZIF_TRUSTED_PROXIES", trusted)

    with pytest.raises(ValueError):
        create_app()


def test_addresses_and_networks_together_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_TRUSTED_PROXIES", "172.28.0.10, 10.42.0.0/16")

    create_app()  # doesn't raise


def test_proxy_headers_is_the_outermost_middleware() -> None:
    outermost: object = create_app().user_middleware[0].cls
    assert outermost is ProxyHeadersMiddleware


def test_the_sign_up_bucket_ignores_a_forged_header_from_an_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    peer = fresh_address()
    app = app_trusting(monkeypatch, fresh_address())  # trusted = someone else
    client = new_client(app, peer)

    statuses = [
        client.post(
            "/api/sign-up",
            json={"email": fresh_email(), "locale": "en"},
            headers={"X-Forwarded-For": fresh_address()},  # a fresh, forged address every time
        ).status_code
        for _ in range(11)
    ]

    assert statuses == [202] * 10 + [429]


def test_the_sign_up_bucket_is_keyed_on_the_trusted_forwarded_address(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    peer = fresh_address()
    app = app_trusting(monkeypatch, peer)
    client = new_client(app, peer)
    address_a, address_b = fresh_address(), fresh_address()

    statuses = [
        client.post(
            "/api/sign-up",
            json={"email": fresh_email(), "locale": "en"},
            headers={"X-Forwarded-For": address_a},
        ).status_code
        for _ in range(11)
    ]
    from_b = client.post(
        "/api/sign-up",
        json={"email": fresh_email(), "locale": "en"},
        headers={"X-Forwarded-For": address_b},
    )

    assert statuses == [202] * 10 + [429]
    assert from_b.status_code == 202
