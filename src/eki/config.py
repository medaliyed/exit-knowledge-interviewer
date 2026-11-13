"""Application settings.

Every value comes from an ``EKI_``-prefixed environment variable (or the local
``.env`` file). Nothing secret has a default, and secrets are held as
``SecretStr`` so they never appear in logs, reprs or JSON dumps.
"""

from functools import lru_cache
from pathlib import Path
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

    # The expert being interviewed and where their source data lives (used by the `eki` CLI).
    expert_name: str | None = None
    expert_email: str | None = None
    work_email_domains: str = "example.com"
    """Comma separated. Email addresses on these domains are kept; all others are redacted."""
    source_dir: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def work_domains(settings: Settings) -> frozenset[str]:
    return frozenset(d.strip().lower() for d in settings.work_email_domains.split(",") if d.strip())
