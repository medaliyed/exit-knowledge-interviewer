"""Deliverable b: the Compose file is pinned, localhost-only and free of default secrets.

These tests inspect the resolved configuration; they do not start containers.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

EXPECTED_SERVICES = {
    "postgres",
    "clickhouse",
    "redis",
    "minio",
    "langfuse-worker",
    "langfuse-web",
}
SECRET_WORDS = ("PASSWORD", "SECRET", "KEY", "SALT", "AUTH")

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")


def env_example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            keys.add(stripped.split("=", 1)[0])
    return keys


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config: dict[str, Any] = json.loads(result.stdout)
    resolved: dict[str, Any] = config["services"]
    return resolved


@needs_docker
def test_expected_services(services: dict[str, Any]) -> None:
    assert set(services) == EXPECTED_SERVICES


@needs_docker
def test_postgres_is_pg16_with_pgvector(services: dict[str, Any]) -> None:
    assert services["postgres"]["image"] == "pgvector/pgvector:0.8.7-pg16"


@needs_docker
def test_all_images_pinned(services: dict[str, Any]) -> None:
    unpinned = []
    for name, service in services.items():
        image: str = service["image"]
        if re.search(r"@sha256:[0-9a-f]{64}$", image):
            continue
        last_segment = image.rsplit("/", 1)[-1]
        tag = last_segment.split(":", 1)[1] if ":" in last_segment else ""
        if tag == "latest" or not re.search(r"\d+\.\d+", tag):
            unpinned.append(f"{name}: {image}")
    assert unpinned == []


@needs_docker
def test_all_ports_bound_to_localhost(services: dict[str, Any]) -> None:
    exposed = [
        f"{name}: {port.get('published')}"
        for name, service in services.items()
        for port in service.get("ports", [])
        if port.get("host_ip") != "127.0.0.1"
    ]
    assert exposed == []


def test_compose_file_has_no_default_secrets() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    with_default = re.findall(r"\$\{([A-Z0-9_]+):?-", text)
    offenders = [name for name in with_default if any(word in name for word in SECRET_WORDS)]
    assert offenders == []


def test_env_example_defines_every_variable() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z0-9_]+)", text))
    assert referenced, "expected the Compose file to take its secrets from the environment"
    assert referenced - env_example_keys() == set()
