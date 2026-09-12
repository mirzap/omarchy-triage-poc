"""Global test isolation from the user's live ``.triage`` directory."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make every relative default store/cache path disposable."""
    monkeypatch.chdir(tmp_path)
