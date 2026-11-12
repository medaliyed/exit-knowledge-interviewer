"""Deliverable g: the API exposes a liveness probe and nothing else."""

import pytest
from fastapi.testclient import TestClient

from eki.api.app import app, create_app


def test_healthz_ok() -> None:
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_the_only_api_route() -> None:
    assert set(create_app().openapi()["paths"]) == {"/healthz"}


def test_app_starts_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EKI_DATABASE_URL", raising=False)
    assert TestClient(create_app()).get("/healthz").status_code == 200


def test_module_level_app_serves_healthz() -> None:
    assert TestClient(app).get("/healthz").status_code == 200


def test_unknown_path_is_404() -> None:
    assert TestClient(create_app()).get("/nope").status_code == 404
