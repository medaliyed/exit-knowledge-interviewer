"""The CI evaluation runner excludes live calls and propagates failed gates."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


def runner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "evals" / "run.py"
    spec = importlib.util.spec_from_file_location("eki_eval_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("returncode", [0, 1, 5])
def test_eval_runner_excludes_live_and_propagates_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    module = runner()
    seen: list[list[str]] = []
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def run(command: list[str], **options: Any) -> SimpleNamespace:
        seen.append(command)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main(["--junit", str(tmp_path / "out.xml"), "--unit-only"]) == returncode
    selection = seen[0][seen[0].index("--strict-markers") + 2]
    assert "not live" in selection and "not infra" in selection and "not db" in selection
    assert not any("golden" in arg or "scenarios" in arg for arg in seen[0])
