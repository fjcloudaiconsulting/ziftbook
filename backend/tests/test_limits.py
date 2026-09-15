import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from app import limits
from app.db import SessionLocal, tenant_context
from tests.conftest import People

WINDOW = timedelta(minutes=15)


@pytest.fixture
def action(app_engine: Engine) -> Iterator[str]:
    """A fresh key prefix per test, and a bound session factory; the rows go afterwards."""
    SessionLocal.configure(bind=app_engine)
    prefix = f"test-{uuid.uuid4()}"
    yield prefix
    with app_engine.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE key LIKE :p"), {"p": f"{prefix}%"})
    SessionLocal.configure(bind=None)


def test_the_attempt_after_the_limit_is_refused(action: str) -> None:
    key = f"{action}:k"

    results = [limits.hit({key: 3}, WINDOW) for _ in range(4)]

    assert results == [False, False, False, True]


def test_a_new_window_starts_counting_again(action: str, app_engine: Engine) -> None:
    key = f"{action}:k"
    for _ in range(3):
        limits.hit({key: 3}, WINDOW)
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE rate_limits SET window_start = now() - :w WHERE key = :k"),
            {"w": WINDOW + timedelta(seconds=1), "k": key},
        )

    assert limits.hit({key: 3}, WINDOW) is False
    with app_engine.connect() as conn:
        assert conn.scalar(text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key}) == 1


def test_any_key_over_its_limit_refuses_the_attempt(action: str) -> None:
    for _ in range(2):
        limits.hit({f"{action}:email": 2, f"{action}:ip": 50}, WINDOW)

    assert limits.hit({f"{action}:email": 2, f"{action}:ip": 50}, WINDOW) is True
    assert limits.hit({f"{action}:other-email": 2, f"{action}:ip": 50}, WINDOW) is False


def test_concurrent_attempts_are_counted_exactly(action: str) -> None:
    key = f"{action}:k"
    start = threading.Barrier(20)
    refused: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        start.wait()
        result = limits.hit({key: 10}, WINDOW)
        with lock:
            refused.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert refused.count(False) == 10


def test_an_attempt_counts_even_when_the_request_fails(
    action: str, people: People, app_engine: Engine
) -> None:
    key = f"{action}:k"

    with pytest.raises(RuntimeError), tenant_context(people.a):
        limits.hit({key: 3}, WINDOW)
        raise RuntimeError("the endpoint failed after counting")

    with app_engine.connect() as conn:
        assert conn.scalar(text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key}) == 1


def test_old_rows_are_purged_and_recent_ones_kept(action: str, app_engine: Engine) -> None:
    old, recent = f"{action}:old", f"{action}:recent"
    limits.hit({old: 3, recent: 3}, WINDOW)
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE rate_limits SET window_start = now() - interval '2 days' WHERE key = :k"),
            {"k": old},
        )

    limits.hit({f"{action}:trigger": 3}, WINDOW)

    with app_engine.connect() as conn:
        kept = set(
            conn.scalars(text("SELECT key FROM rate_limits WHERE key LIKE :p"), {"p": f"{action}%"})
        )
    assert kept == {recent, f"{action}:trigger"}


def test_an_email_key_does_not_hold_the_address() -> None:
    key = limits.email_key("sign_in", "ana@studioana.nl")

    assert "ana" not in key and key == limits.email_key("sign_in", "ana@studioana.nl")
    assert key != limits.email_key("sign_up", "ana@studioana.nl")


@pytest.mark.parametrize(
    ("first", "second", "shared"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", True),  # one IPv6 /64
        ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
        ("192.0.2.1", "192.0.2.2", False),
        ("::ffff:192.0.2.1", "192.0.2.1", True),  # an IPv4 client seen through IPv6
        ("::ffff:192.0.2.1", "::ffff:192.0.2.2", False),
        ("fe80::1%eth0", "fe80::2", True),
    ],
)
def test_ip_keys_group_an_ipv6_network_and_keep_ipv4_clients_apart(
    first: str, second: str, shared: bool
) -> None:
    assert (limits.ip_key("sign_in", first) == limits.ip_key("sign_in", second)) is shared


def test_an_unparseable_address_has_no_key() -> None:
    assert limits.ip_key("sign_in", "testclient") is None
    assert limits.ip_key("sign_in", None) is None
