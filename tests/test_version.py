from __future__ import annotations

import re
from pathlib import Path

import aiomoysklad

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_the_package_version_matches_pyproject() -> None:
    match = re.search(r'^version = "([^"]+)"$', PYPROJECT.read_text(encoding="utf-8"), re.M)
    assert match is not None
    assert aiomoysklad.__version__ == match.group(1)


def test_the_public_api_is_exported() -> None:
    for name in aiomoysklad.__all__:
        assert hasattr(aiomoysklad, name), name
    assert "__version__" in aiomoysklad.__all__
