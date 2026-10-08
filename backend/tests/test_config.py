"""Pure unit tests for app.config: no database, no network."""

import pytest

from app.config import MailSettings, TurnstileSettings, WorkerSettings


def test_empty_env_var_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes an unset optional variable as "" (${VAR:-}); that must not override the
    # default, or ZIF_MAILGUN_REGION="" would refuse to start instead of meaning "eu".
    monkeypatch.setenv("ZIF_MAILGUN_REGION", "")
    assert MailSettings().mailgun_region == "eu"


def test_empty_env_var_where_the_default_is_already_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "")
    assert TurnstileSettings().turnstile_secret == ""


def test_empty_optional_string_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_MAILPIT_URL", "")
    monkeypatch.setenv("ZIF_DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")
    assert MailSettings().mailpit_url is None
    monkeypatch.setenv("ZIF_HEALTHCHECK_URL", "")
    assert WorkerSettings().healthcheck_url is None


def test_a_real_value_still_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_MAILGUN_REGION", "us")
    assert MailSettings().mailgun_region == "us"
