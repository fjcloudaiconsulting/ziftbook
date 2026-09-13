from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Deployment configuration from ZIF_* environment variables.

    Business settings live in the database, not here.
    """

    model_config = SettingsConfigDict(env_prefix="ZIF_")

    app_version: str = "dev"
