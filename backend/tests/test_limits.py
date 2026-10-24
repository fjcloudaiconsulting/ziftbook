import hashlib
import hmac
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

    results = [limits.hit({key: 3}, WINDOW) for _ in range(5)]

    assert results == [False, False, False, True, True]


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
    # The key over its limit sorts last, so checking only the first key would miss it.
    tight, loose = f"{action}:z-tight", f"{action}:a-loose"
    for _ in range(2):
        limits.hit({loose: 50, tight: 2}, WINDOW)

    assert limits.hit({loose: 50, tight: 2}, WINDOW) is True
    assert limits.hit({loose: 50, f"{action}:z-other": 2}, WINDOW) is False


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


def test_limits_count_reads_without_writing(action: str) -> None:
    assert limits.count(f"{action}:k", timedelta(minutes=15)) == 0
    limits.hit({f"{action}:k": 5}, timedelta(minutes=15))
    assert limits.count(f"{action}:k", timedelta(minutes=15)) == 1
    assert limits.count(f"{action}:k", timedelta(minutes=15)) == 1


def test_count_ignores_a_window_hit_would_start_again(action: str, app_engine: Engine) -> None:
    key = f"{action}:k"
    limits.hit({key: 5}, WINDOW)
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE rate_limits SET window_start = now() - :w WHERE key = :k"),
            {"w": WINDOW + timedelta(seconds=1), "k": key},
        )

    assert limits.count(key, WINDOW) == 0


def test_callers_listing_the_same_keys_in_any_order_do_not_deadlock(action: str) -> None:
    first, second = f"{action}:a", f"{action}:b"
    start = threading.Barrier(16)
    errors: list[Exception] = []

    def attempt(reverse: bool) -> None:
        start.wait()
        keys = {second: 1000, first: 1000} if reverse else {first: 1000, second: 1000}
        for _ in range(20):
            try:
                limits.hit(keys, WINDOW)
            except Exception as error:
                errors.append(error)

    threads = [threading.Thread(target=attempt, args=(n % 2 == 0,)) for n in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_an_attempt_counts_even_when_the_request_fails(
    action: str, people: People, app_engine: Engine
) -> None:
    key = f"{action}:k"

    with pytest.raises(RuntimeError), tenant_context(people.a):
        limits.hit({key: 3}, WINDOW)
        raise RuntimeError("the endpoint failed after counting")

    with app_engine.connect() as conn:
        assert conn.scalar(text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key}) == 1


def test_an_attempt_counted_in_the_callers_transaction_commits_or_rolls_back_with_it(
    action: str, people: People, app_engine: Engine
) -> None:
    key = f"{action}:k"

    with tenant_context(people.a) as db:
        assert limits.hit({key: 1}, WINDOW, db=db) is False
    with pytest.raises(RuntimeError), tenant_context(people.a) as db:
        assert limits.hit({key: 1}, WINDOW, db=db) is True
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
        # Old enough to be outside any window, young enough to keep: a short cutoff would drop it.
        conn.execute(
            text(
                "UPDATE rate_limits SET window_start = now() - interval '23 hours' WHERE key = :k"
            ),
            {"k": recent},
        )

    limits.hit({f"{action}:trigger": 3}, WINDOW)

    with app_engine.connect() as conn:
        kept = set(
            conn.scalars(text("SELECT key FROM rate_limits WHERE key LIKE :p"), {"p": f"{action}%"})
        )
    assert kept == {recent, f"{action}:trigger"}


def test_an_email_key_does_not_hold_the_address() -> None:
    limits.email_key("sign_in", "a\ud800@studioana.nl")  # a lone surrogate doesn't raise
    key = limits.email_key("sign_in", "ana@studioana.nl")

    assert "ana" not in key and key == limits.email_key("sign_in", "ana@studioana.nl")
    assert key != limits.email_key("sign_up", "ana@studioana.nl")


PEPPER = "test-only-rate-limit-key-not-a-secret-0123456789"


@pytest.fixture
def peppered(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", PEPPER)
    return PEPPER


def test_a_peppered_email_key_holds_neither_the_address_nor_its_plain_hash(peppered: str) -> None:
    key = limits.email_key("a", "x@example.com")

    assert "x@example.com" not in key
    assert hashlib.sha256(b"x@example.com").hexdigest() not in key
    expected = hmac.new(peppered.encode(), b"x@example.com", hashlib.sha256).hexdigest()
    assert key == f"a:email:{expected}"
    assert key == limits.email_key("a", "x@example.com")  # deterministic
    assert key != limits.email_key("a", "y@example.com")
    limits.email_key("a", "a\ud800@example.com")  # a lone surrogate doesn't raise


@pytest.mark.parametrize(
    ("host", "raw"),
    [
        ("192.0.2.1", "192.0.2.1"),
        ("2001:db8:1:2::1", "2001:db8"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("not-an-ip", "unknown"),
    ],
)
def test_a_peppered_ip_key_holds_no_address(peppered: str, host: str, raw: str) -> None:
    key = limits.ip_key("a", host)

    assert raw not in key
    assert key == limits.ip_key("a", host)
    assert key.startswith("a:ip:") and len(key) == len("a:ip:") + 64


def test_peppered_ip_keys_keep_the_buckets(peppered: str) -> None:
    assert limits.ip_key("a", "2001:db8:1:2::1") == limits.ip_key("a", "2001:db8:1:2:ffff::9")
    assert limits.ip_key("a", "192.0.2.1") != limits.ip_key("a", "192.0.2.2")
    assert limits.ip_key("a", "::ffff:192.0.2.1") == limits.ip_key("a", "192.0.2.1")
    assert limits.ip_key("a", "x", v6_prefix=48) == limits.ip_key("a", None)
    assert limits.ip_key("a", "2001:db8:1:2::1", v6_prefix=48) == limits.ip_key(
        "a", "2001:db8:1:ffff::9", v6_prefix=48
    )


def test_a_different_pepper_gives_different_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", "a" * 32)
    first = limits.email_key("a", "x@example.com"), limits.ip_key("a", "192.0.2.1")
    monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", "b" * 32)

    assert first != (limits.email_key("a", "x@example.com"), limits.ip_key("a", "192.0.2.1"))


def test_without_the_key_the_current_form_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_RATE_LIMIT_KEY", raising=False)

    assert limits.key_state() == "unset"
    assert limits.email_key("a", "x@example.com") == (
        f"a:email:{hashlib.sha256(b'x@example.com').hexdigest()}"
    )
    assert limits.ip_key("a", "192.0.2.1") == "a:ip:192.0.2.1"
    assert limits.ip_key("a", "nope") == "a:ip:unknown"


def test_a_key_shorter_than_32_characters_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", "x" * 31)

    assert limits.key_state() == "weak"
    assert limits.ip_key("a", "192.0.2.1") == "a:ip:192.0.2.1"
    monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", "x" * 32)
    assert limits.key_state() == "set"


DEV_PEPPER = "dev-only-rate-limit-key-not-a-secret-0000"  # docker-compose.yaml's development value


# fence: the development key counts as unset on a public URL, and peppers on localhost.
def test_the_development_key_on_a_public_url_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_RATE_LIMIT_KEY", DEV_PEPPER)
    monkeypatch.setenv("ZIF_APP_URL", "https://app.example.com")

    assert limits.key_state() == "dev"
    assert limits.ip_key("a", "192.0.2.1") == "a:ip:192.0.2.1"
    assert limits.email_key("a", "x@example.com") == (
        f"a:email:{hashlib.sha256(b'x@example.com').hexdigest()}"
    )
    for local in ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"]:
        monkeypatch.setenv("ZIF_APP_URL", local)
        assert limits.key_state() == "set", local
        assert limits.ip_key("a", "192.0.2.1") != "a:ip:192.0.2.1", local


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


def test_addresses_that_do_not_parse_share_one_key() -> None:
    assert limits.ip_key("sign_in", "testclient") == limits.ip_key("sign_in", None)
    assert limits.ip_key("sign_in", "[::1]") == limits.ip_key("sign_in", "testclient")
    assert limits.ip_key("sign_in", "testclient") != limits.ip_key("sign_in", "192.0.2.1")


def test_auth_keys_group_ipv6_by_48() -> None:
    a = limits.ip_key("sign_in", "2001:db8:1:2::1", v6_prefix=48)
    assert a == limits.ip_key("sign_in", "2001:db8:1:ffff::9", v6_prefix=48)
    assert a != limits.ip_key("sign_in", "2001:db8:2::1", v6_prefix=48)


def test_default_keys_still_group_by_64() -> None:
    assert limits.ip_key("x", "2001:db8:1:2::1") != limits.ip_key("x", "2001:db8:1:3::1")


def test_unparseable_and_mapped_addresses_still_key() -> None:
    assert limits.ip_key("x", "not-an-ip", v6_prefix=48) == "x:ip:unknown"
    assert limits.ip_key("x", "::ffff:192.0.2.1", v6_prefix=48) == "x:ip:192.0.2.1"


def test_aliases_share_one_limit_key() -> None:
    assert limits.address_for_limits("a.b+1@gmail.com") == limits.address_for_limits(
        "ab+2@googlemail.com"
    )
    assert limits.address_for_limits("a+x@example.com") == "a@example.com"
    assert limits.address_for_limits("A.B@Example.com") == "a.b@example.com"  # dots count off Gmail


# ZB-19. Wrong impl: folding that indexes into a split it assumes has an '@', or that encodes.
@pytest.mark.parametrize("email", ["no-at-sign", "ünï.cødé+tag@exämple.com", "a\ud800@x.nl", "@"])
def test_folding_never_raises(email: str) -> None:
    assert isinstance(limits.address_for_limits(email), str)


# ZB-19. Wrong impls: a refund that takes more than one hit, goes below zero, or touches a key it
# was not given.
def test_a_refund_gives_back_one_hit_per_key(action: str) -> None:
    once, twice, absent, other = (f"{action}:{n}" for n in ("once", "twice", "absent", "other"))
    limits.hit({once: 5, twice: 5, other: 5}, WINDOW)
    limits.hit({twice: 5}, WINDOW)

    limits.refund([once, twice, absent], WINDOW)
    limits.refund([once], WINDOW)  # already at zero

    assert [limits.count(k, WINDOW) for k in (once, twice, absent, other)] == [0, 1, 0, 1]


# ZB-19. Wrong impl: a refund that reaches into a window hit() would start again.
def test_a_refund_leaves_an_expired_window_alone(action: str, app_engine: Engine) -> None:
    key = f"{action}:k"
    limits.hit({key: 5}, WINDOW)
    with app_engine.begin() as conn:
        conn.execute(
            text("UPDATE rate_limits SET window_start = now() - :w WHERE key = :k"),
            {"w": WINDOW + timedelta(seconds=1), "k": key},
        )

    limits.refund([key], WINDOW)

    with app_engine.connect() as conn:
        assert conn.scalar(text("SELECT hits FROM rate_limits WHERE key = :k"), {"k": key}) == 1


# ZB-19. Wrong impl: a refund that locks its keys in another order than hit() does.
def test_refunds_and_hits_on_the_same_keys_do_not_deadlock(action: str) -> None:
    first, second = f"{action}:a", f"{action}:b"
    limits.hit({second: 1000}, WINDOW)  # stored out of key order, as a scan may meet them
    limits.hit({first: 1000}, WINDOW)
    start = threading.Barrier(16)
    errors: list[Exception] = []

    def attempt(refunding: bool) -> None:
        start.wait()
        for _ in range(20):
            try:
                if refunding:
                    limits.refund([second, first], WINDOW)
                else:
                    limits.hit({first: 1000, second: 1000}, WINDOW)
            except Exception as error:
                errors.append(error)

    threads = [threading.Thread(target=attempt, args=(n % 2 == 0,)) for n in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
