"""Application settings.

Every value comes from an ``EKI_``-prefixed environment variable (or the local
``.env`` file). Nothing secret has a default, and secrets are held as
``SecretStr`` so they never appear in logs, reprs or JSON dumps.
"""

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
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
    public_origin: str = "http://localhost:8000"
    session_cookie_secure: bool = True
    session_idle_hours: float = Field(default=8, gt=0, le=168)
    session_max_days: float = Field(default=7, gt=0, le=365)
    trusted_proxies: str = ""
    """Comma-separated proxy addresses or networks whose X-Forwarded-For is believed.
    Empty (default): the connecting address is the client, even if it is a proxy."""
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
    search_min_similarity: float = Field(default=0.18, ge=0, le=1)
    model_answerer: str | None = None
    model_embedding: str | None = None
    model_artifact_embedding: str | None = None
    model_interviewer: str | None = None
    model_drafter: str | None = None
    model_timeout_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_session_configuration(self) -> "Settings":
        origin = urlsplit(self.public_origin)
        try:
            port = origin.port
        except ValueError:
            raise ValueError("EKI_PUBLIC_ORIGIN must be a valid HTTP origin") from None
        if (
            origin.scheme not in ("http", "https")
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("EKI_PUBLIC_ORIGIN must be a valid HTTP origin")
        self.public_origin = self.public_origin.rstrip("/")
        if self.env == "prod" and (not self.session_cookie_secure or origin.scheme != "https"):
            raise ValueError("production sessions require HTTPS and secure cookies")
        try:
            proxy_networks(self)
        except ValueError:
            raise ValueError("EKI_TRUSTED_PROXIES must list IP addresses or networks") from None
        return self


class ConfigError(Exception):
    """A setting needed for this command is missing."""


@lru_cache
def get_settings() -> Settings:
    return Settings()


def proxy_networks(settings: Settings) -> tuple[IPv4Network | IPv6Network, ...]:
    return tuple(
        ip_network(item.strip()) for item in settings.trusted_proxies.split(",") if item.strip()
    )


def work_domains(settings: Settings) -> frozenset[str]:
    return frozenset(d.strip().lower() for d in settings.work_email_domains.split(",") if d.strip())


def model_name(settings: Settings, role: Literal["interviewer", "drafter"]) -> str:
    """The configured model for a role. Raises ``ConfigError`` when it is not set."""
    name = settings.model_interviewer if role == "interviewer" else settings.model_drafter
    if not name:
        raise ConfigError(f"EKI_MODEL_{role.upper()} is not set (a LiteLLM model name)")
    return name
