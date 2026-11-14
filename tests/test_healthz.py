"""The API exposes a liveness probe, and since phase 2 the plan and session routes."""

import httpx2
import pytest
from fastapi.testclient import TestClient

from eki.api.app import app, create_app


def test_healthz_ok() -> None:
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_routes_are_exactly_these() -> None:
    assert set(create_app().openapi()["paths"]) == {
        "/healthz",
        "/runs",
        "/runs/{run_id}/plans",
        "/runbook/identity",
        "/runbook/entries",
        "/runs/{run_id}/audit",
        "/runbook/search",
        "/runbook/ask",
        "/runbook/export",
        "/entries/{entry_id}/markdown",
        "/runs/{run_id}/access",
        "/runs/{run_id}/owner",
        "/sessions/{session_id}/synthesise",
        "/sessions/{session_id}/entries",
        "/entries/{entry_id}",
        "/entries/{entry_id}/approve",
        "/entries/{entry_id}/reject",
        "/runs/{run_id}/plan",
        "/plans/{plan_id}",
        "/plans/{plan_id}/approve",
        "/sessions/{session_id}",
        "/sessions/{session_id}/messages",
        "/sessions/{session_id}/messages/{message_id}",
        "/sessions/{session_id}/messages/{message_id}/resume",
        "/sessions/{session_id}/resume",
    }


def test_app_starts_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EKI_DATABASE_URL", raising=False)
    assert TestClient(create_app()).get("/healthz").status_code == 200


def test_module_level_app_serves_healthz() -> None:
    assert TestClient(app).get("/healthz").status_code == 200


def test_unknown_path_is_404() -> None:
    assert TestClient(create_app()).get("/nope").status_code == 404


def test_test_client_runs_on_httpx2() -> None:
    assert issubclass(TestClient, httpx2.Client)
