"""Isolate all automatically saved Web results from real local history."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_local_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from workflow_studio.storage import RunStore
    from workflow_studio.web import history, persistence, app
    store = RunStore(tmp_path / "outputs")
    monkeypatch.setattr(persistence, "default_store", lambda: store)
    monkeypatch.setattr(history, "default_store", lambda: store)
    monkeypatch.setattr(app, "configure_logging", lambda: None)
