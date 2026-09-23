"""Pure unit tests for app.config: no database, no network."""

import pytest

from app.config import MailSettings, TurnstileSettings, WorkerSettings


def test_empty_env_var_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes an unset optional variable as "" (${VAR:-}); that must not override the
    # default, or ZIF_SMTP_FROM="" would send mail with an empty From header.
    monkeypatch.setenv("ZIF_SMTP_FROM", "")
    assert MailSettings().smtp_from == "ziftbook <no-reply@ziftbook.com>"


def test_empty_env_var_where_the_default_is_already_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_TURNSTILE_SECRET", "")
    assert TurnstileSettings().turnstile_secret == ""


def test_empty_optional_string_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_SMTP_USERNAME", "")
    monkeypatch.setenv("ZIF_DATABASE_URL", "postgresql+psycopg://x:x@localhost/x")
    assert MailSettings().smtp_username is None
    monkeypatch.setenv("ZIF_HEALTHCHECK_URL", "")
    assert WorkerSettings().healthcheck_url is None


def test_a_real_value_still_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIF_SMTP_FROM", "ziftbook <hello@ziftbook.com>")
    assert MailSettings().smtp_from == "ziftbook <hello@ziftbook.com>"
