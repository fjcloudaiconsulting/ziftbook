from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment configuration from ZIF_* environment variables.

    Business settings live in the database, not here.
    """

    model_config = SettingsConfigDict(env_prefix="ZIF_")

    app_version: str = "dev"


class DatabaseSettings(Settings):
    """Configuration of the processes that use the database: the API and the worker."""

    database_url: str


class WorkerSettings(DatabaseSettings):
    """The worker's configuration."""

    # Pinged after every successful loop, so a dead or stuck worker raises an alert. Unset in dev.
    healthcheck_url: str | None = None


class MailSettings(Settings):
    """SMTP for outgoing email. Deployed: Mailgun EU (smtp.eu.mailgun.org:587, STARTTLS)."""

    smtp_host: str = "smtp.eu.mailgun.org"
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_username: str | None = None
    smtp_password: str = ""
    smtp_from: str = "ziftbook <no-reply@ziftbook.com>"
