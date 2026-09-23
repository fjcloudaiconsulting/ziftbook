from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment configuration from ZIF_* environment variables.

    Business settings live in the database, not here.
    """

    # env_ignore_empty: compose passes an unset optional variable as "" (${VAR:-}), which
    # pydantic-settings would otherwise treat as an explicit value, overriding the default.
    model_config = SettingsConfigDict(env_prefix="ZIF_", env_ignore_empty=True)

    app_version: str = "dev"
    # Addresses or networks (comma-separated) whose X-Forwarded-For the API believes: the web app
    # only. Empty trusts nobody. Compose: the frontend's fixed address. Staging: the pod network.
    trusted_proxies: str = ""


class DatabaseSettings(Settings):
    """Configuration of the processes that use the database: the API and the worker."""

    database_url: str


class WorkerSettings(DatabaseSettings):
    """The worker's configuration."""

    # Pinged after every successful loop, so a dead or stuck worker raises an alert. Unset in dev.
    healthcheck_url: str | None = None


class LogSettings(Settings):
    """Logging, read by app.logs.configure(). Development runs DEBUG/text (docker-compose.yaml)."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    # SQL statements (never their values: every engine has hide_parameters), at DEBUG only.
    log_sql: bool = False


class TurnstileSettings(Settings):
    """Cloudflare Turnstile. Unset means verification is skipped, so development and the test suite
    need no network; that state is printed on the `api started` line so a deployment that forgot the
    secret is visible in the logs rather than silently open."""

    # Resolved at runtime from the environment and never committed. In a deployment it comes from
    # Secrets Manager as {{resolve:secretsmanager:...:SecretString:json-key}} so the value never
    # enters a repository or a transcript (ZIF-38).
    turnstile_secret: str = ""


class MailSettings(Settings):
    """SMTP for outgoing email. Deployed: Mailgun EU (smtp.eu.mailgun.org:587, STARTTLS)."""

    smtp_host: str = "smtp.eu.mailgun.org"
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str | None = None
    smtp_password: str = ""
    smtp_from: str = "ziftbook <no-reply@ziftbook.com>"
    # Where links in emails point: the web app, as people reach it.
    app_url: str = "http://localhost:3000"
