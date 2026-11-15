"""Deliverable c: settings come from the environment only, and secrets stay masked."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from eki.config import Settings, get_settings, work_domains

REPO_ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = "postgresql://someuser:s3cr3t-pw@dbhost:5432/eki"
EKI_VARIABLES = (
    "EKI_ENV",
    "EKI_PUBLIC_ORIGIN",
    "EKI_SESSION_COOKIE_SECURE",
    "EKI_SESSION_IDLE_HOURS",
    "EKI_SESSION_MAX_DAYS",
    "EKI_API_TOKENS",
    "EKI_API_TOKEN",
    "EKI_MODEL_EMBEDDING",
    "EKI_MODEL_ANSWERER",
    "EKI_SEARCH_MIN_SIMILARITY",
    "EKI_DATABASE_URL",
    "EKI_LANGFUSE_HOST",
    "EKI_LANGFUSE_PUBLIC_KEY",
    "EKI_LANGFUSE_SECRET_KEY",
    "EKI_EXPERT_NAME",
    "EKI_EXPERT_EMAIL",
    "EKI_WORK_EMAIL_DOMAINS",
    "EKI_SOURCE_DIR",
    "EKI_MODEL_INTERVIEWER",
    "EKI_MODEL_DRAFTER",
    "EKI_MODEL_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in EKI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def settings_without_dotenv() -> Settings:
    return Settings(_env_file=None)


def test_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKI_DATABASE_URL", DATABASE_URL)
    settings = settings_without_dotenv()
    assert settings.database_url.get_secret_value() == DATABASE_URL
    assert settings.env == "dev"
    assert settings.langfuse_host == "http://localhost:3000"


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError) as excinfo:
        settings_without_dotenv()
    assert [error["loc"] for error in excinfo.value.errors()] == [("database_url",)]


def test_secrets_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKI_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("EKI_LANGFUSE_SECRET_KEY", "sk-lf-very-secret")
    settings = settings_without_dotenv()
    for rendered in (repr(settings), str(settings), settings.model_dump_json()):
        assert "s3cr3t-pw" not in rendered
        assert "sk-lf-very-secret" not in rendered


def test_rejects_unknown_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKI_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("EKI_ENV", "staging")
    with pytest.raises(ValidationError):
        settings_without_dotenv()


def test_dotenv_with_unrelated_keys_loads(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"POSTGRES_PASSWORD=compose-only\nEKI_DATABASE_URL={DATABASE_URL}\n", encoding="utf-8"
    )
    settings = Settings(_env_file=dotenv)
    assert settings.database_url.get_secret_value() == DATABASE_URL


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKI_DATABASE_URL", DATABASE_URL)
    assert get_settings() is get_settings()


def test_env_example_covers_every_field() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    expected = {f"EKI_{name.upper()}" for name in Settings.model_fields}
    assert expected - keys == set()


def test_work_domains_are_split_and_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EKI_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("EKI_WORK_EMAIL_DOMAINS", "Example.com, corp.test ,")
    assert work_domains(settings_without_dotenv()) == {"example.com", "corp.test"}


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "//host",
        "ftp://host",
        "https://user:pw@host",
        "https://host/path",
        "https://host?token=x",
        "https://host#token=x",
        "http://host:bad",
    ],
)
def test_session_origin_must_be_a_bare_http_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=DATABASE_URL, public_origin=origin)


def test_production_requires_https_and_secure_cookies() -> None:
    for values in (
        {"public_origin": "http://host"},
        {"public_origin": "https://host", "session_cookie_secure": False},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, database_url=DATABASE_URL, env="prod", **values)
    settings = Settings(
        _env_file=None, database_url=DATABASE_URL, env="prod", public_origin="https://host/"
    )
    assert settings.public_origin == "https://host"
