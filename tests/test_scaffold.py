"""Deliverable a: the package imports, the toolchain is pinned, Python is 3.12."""

import importlib
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBPACKAGES = ["api", "graph", "ingest", "mapping", "runbook", "search"]
PINNED = re.compile(r"^[A-Za-z0-9_.\-\[\]]+==\d[\w.]*$")


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name: str) -> None:
    importlib.import_module(f"eki.{name}")


def test_version() -> None:
    import eki

    assert eki.__version__ == "0.1.0"


def test_every_dependency_is_pinned() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [
        *pyproject["project"]["dependencies"],
        *pyproject["dependency-groups"]["dev"],
    ]
    assert requirements, "expected at least one dependency"
    unpinned = [r for r in requirements if not PINNED.match(r)]
    assert unpinned == []


def test_python_is_3_12() -> None:
    assert sys.version_info[:2] == (3, 12)
