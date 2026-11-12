"""Application settings.

Every value comes from an ``EKI_``-prefixed environment variable (or the local
``.env`` file). Nothing secret has a default, and secrets are held as
``SecretStr`` so they never appear in logs, reprs or JSON dumps.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EKI_",
        env_file=".env",
        env_file_encoding="utf-8",
        # .env is shared with Docker Compose, so it holds keys that are not ours.
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: SecretStr
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
