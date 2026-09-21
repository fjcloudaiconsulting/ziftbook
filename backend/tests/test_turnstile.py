"""app.turnstile.verify: fails closed, skips cleanly with no secret, and never retries a spent
token (spec C9)."""

import json
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from app import turnstile
from app.config import TurnstileSettings
from app.main import create_app
from tests.conftest import new_client


@pytest.fixture(autouse=True)
def _no_secret_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZIF_TURNSTILE_SECRET", raising=False)


def never_called(*args: object, **kwargs: object) -> Any:
    raise AssertionError("urlopen must not be called")


# 44: GUARD.
def test_verification_is_skipped_when_no_secret_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", never_called)

    assert turnstile.verify(None, None) is True
    assert turnstile.verify("a-token", "203.0.113.1") is True


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


# 45: FENCE. Wrong impl: `return True` in the except branch.
@pytest.mark.parametrize(
    "make_urlopen",
    [
        lambda: (_ for _ in ()).throw(OSError("network unreachable")),
        lambda: FakeAnswer(b"not json"),
        lambda: json_answer({"success": False, "error-codes": ["timeout-or-duplicate"]}),
    ],
    ids=["oserror", "bad-json", "success-false"],
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
    assert len(set(calls)) == len(calls)  # never the same token string passed twice


def test_enabled_reflects_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    assert turnstile.enabled() is False
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "shhh")
    assert turnstile.enabled() is True
    assert TurnstileSettings().turnstile_secret == "shhh"
