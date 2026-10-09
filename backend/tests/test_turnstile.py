"""app.turnstile.verify: fails closed, refuses with no secret unless explicitly disabled (ZIF-116),
and never retries a spent token (spec C9)."""

import http.client
import json
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from app import turnstile
from app.config import TurnstileSettings
from app.main import create_app
from tests.conftest import new_client


@pytest.fixture(autouse=True)
def _no_secret_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_TURNSTILE_SECRET", raising=False)


def never_called(*args: object, **kwargs: object) -> Any:
    raise AssertionError("urlopen must not be called")


# 44: GUARD. Development and the suite (conftest) opt out explicitly and need no network.
def test_verification_is_skipped_when_no_secret_is_configured_and_it_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_DISABLED", "true")
    monkeypatch.setattr(urllib.request, "urlopen", never_called)

    assert turnstile.verify(None, None) is True
    assert turnstile.verify("a-token", "203.0.113.1") is True


# ZIF-116 FENCE. Wrong impl: `if not secret: return True` (fail open, the old default), or reading
# the flag as a truthy string (`bool(os.environ.get(...))`, so "false" opts out).
@pytest.mark.parametrize("disabled", [None, "false", "0"])
def test_no_secret_refuses_unless_turnstile_is_disabled(
    monkeypatch: pytest.MonkeyPatch, disabled: str | None
) -> None:
    if disabled is None:
        monkeypatch.delenv("ZIF_TURNSTILE_DISABLED", raising=False)
    else:
        monkeypatch.setenv("ZIF_TURNSTILE_DISABLED", disabled)
    monkeypatch.setattr(urllib.request, "urlopen", never_called)

    assert turnstile.verify("a-token", "203.0.113.1") is False


# ZIF-116 FENCE. Wrong impl: the flag compared as a string (`== "true"`), so a typo silently refuses
# every booking instead of stopping startup like any other malformed setting.
def test_a_malformed_disabled_flag_is_an_error_not_a_quiet_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_DISABLED", "yes-please")

    with pytest.raises(ValidationError):
        turnstile.verify("a-token", "203.0.113.1")


# ZIF-116 FENCE. Wrong impl: the flag checked before the secret, so a forgotten opt-out turns off a
# configured secret.
def test_a_configured_secret_is_checked_even_when_turnstile_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    monkeypatch.setenv("ZIF_TURNSTILE_DISABLED", "true")
    calls: list[object] = []

    def fake_urlopen(request: object, timeout: float | None = None) -> Any:
        calls.append(request)
        return json_answer({"success": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert turnstile.verify("a-token", "203.0.113.1") is False
    assert len(calls) == 1


class FakeAnswer:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeAnswer:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def json_answer(body: dict[str, Any]) -> FakeAnswer:
    return FakeAnswer(json.dumps(body).encode())


# 45: FENCE. Wrong impl: (a) `return True` in the except branch; (b) drop
# http.client.HTTPException from the except tuple - IncompleteRead is NOT an OSError, it subclasses
# HTTPException only, so a body truncated mid-read escapes and 500s; (c) drop the isinstance(dict)
# guard and go straight to result.get - `[]`, `null` and `"ok"` are all valid JSON that json.load
# accepts and .get raises AttributeError on. (b) and (c) each turn the product's first
# unauthenticated write into a 500 with a traceback instead of a refusal.
@pytest.mark.parametrize(
    "make_urlopen",
    [
        lambda: (_ for _ in ()).throw(OSError("network unreachable")),
        lambda: FakeAnswer(b"not json"),
        lambda: json_answer({"success": False, "error-codes": ["timeout-or-duplicate"]}),
        lambda: (_ for _ in ()).throw(http.client.IncompleteRead(b'{"succ')),
        lambda: FakeAnswer(b"[]"),
        lambda: FakeAnswer(b"null"),
        lambda: FakeAnswer(b'"ok"'),
    ],
    ids=[
        "oserror",
        "bad-json",
        "success-false",
        "incomplete-read",
        "json-array",
        "json-null",
        "json-string",
    ],
)
def test_verification_fails_closed(monkeypatch: pytest.MonkeyPatch, make_urlopen: Any) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")

    def fake_urlopen(request: object, timeout: float | None = None) -> Any:
        return make_urlopen()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert turnstile.verify("a-token", "203.0.113.1") is False


def test_verification_succeeds_with_a_true_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # So the fail-closed test above cannot pass by always failing.
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: json_answer({"success": True})
    )

    assert turnstile.verify("a-token", "203.0.113.1") is True


# 46: GUARD.
def test_a_missing_or_over_long_token_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    monkeypatch.setattr(urllib.request, "urlopen", never_called)

    assert turnstile.verify(None, None) is False
    assert turnstile.verify("x" * 2049, None) is False


# 47: FENCE. Wrong impl: omit the `turnstile` field from main.lifespan's log call.
def test_the_api_started_line_says_whether_turnstile_is_on(
    log_lines: Callable[[], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    with new_client(create_app()):
        pass
    off = [line for line in log_lines() if line["msg"] == "api started"]
    assert off[-1]["turnstile"] == "off"

    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "a-very-secret-value")
    with new_client(create_app()):
        pass
    on = [line for line in log_lines() if line["msg"] == "api started"]
    assert on[-1]["turnstile"] == "on"
    assert "a-very-secret-value" not in json.dumps(log_lines())


# ZIF-116 FENCE. Wrong impl: no ERROR (only the INFO "off" field, which nobody acted on), an ERROR
# also when disabled or configured, or refusing to start (the whole API down over public booking).
@pytest.mark.parametrize(
    ("secret", "disabled", "errors"),
    [(None, None, 1), (None, "true", 0), ("shhh", None, 0)],
    ids=["unset", "disabled", "configured"],
)
def test_startup_logs_an_error_while_public_bookings_are_refused(
    log_lines: Callable[[], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    secret: str | None,
    disabled: str | None,
    errors: int,
) -> None:
    for name, value in (("ZIF_TURNSTILE_SECRET", secret), ("ZIF_TURNSTILE_DISABLED", disabled)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with new_client(create_app()) as client:
        assert client.get("/api/healthz").status_code == 200

    refused = [line for line in log_lines() if "public bookings refused" in line["msg"]]
    assert [line["level"] for line in refused] == ["ERROR"] * errors


# 49: FENCE — C9. Wrong impl: wrap the urlopen call in a retry loop, or add a caller-side second
# turnstile.verify(...) call in app/bookings.py.
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (lambda: (_ for _ in ()).throw(OSError("boom")), False),
        (lambda: json_answer({"success": False, "error-codes": ["timeout-or-duplicate"]}), False),
        (lambda: json_answer({"success": True}), True),
    ],
    ids=["oserror", "timeout-or-duplicate", "success"],
)
def test_verification_calls_siteverify_at_most_once(
    monkeypatch: pytest.MonkeyPatch, answer: Any, expected: bool
) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    calls: list[bytes] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        calls.append(request.data)
        return answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert turnstile.verify("a-single-use-token", "203.0.113.1") is expected
    assert len(calls) == 1


def test_enabled_reflects_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    assert turnstile.enabled() is False
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    assert turnstile.enabled() is True
    assert TurnstileSettings().turnstile_secret == "shhh"
