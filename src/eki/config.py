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

    api_tokens: SecretStr = SecretStr("[]")
    api_token: SecretStr | None = None

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

    # Models, reached through LiteLLM. No default on purpose: the choice is the operator's.
    # The provider key is read by LiteLLM from the provider's own variable, never from here.
    model_interviewer: str | None = None
    model_drafter: str | None = None
    model_timeout_seconds: float = 60.0


class ConfigError(Exception):
    """A setting needed for this command is missing."""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def work_domains(settings: Settings) -> frozenset[str]:
    return frozenset(d.strip().lower() for d in settings.work_email_domains.split(",") if d.strip())


def model_name(settings: Settings, role: Literal["interviewer", "drafter"]) -> str:
    """The configured model for a role. Raises ``ConfigError`` when it is not set."""
    name = settings.model_interviewer if role == "interviewer" else settings.model_drafter
    if not name:
        raise ConfigError(f"EKI_MODEL_{role.upper()} is not set (a LiteLLM model name)")
    return name
