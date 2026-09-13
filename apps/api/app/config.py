from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Deployment configuration from environment variables. Business settings live in the DB."""

    app_version: str = "dev"
