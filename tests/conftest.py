from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def analytics_module(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    monkeypatch.setenv("ANALYTICS_DB_PATH", str(db))
    import analytics
    # Close any stale connection cached from a previous test fixture
    # before reload, otherwise the old sqlite handle leaks until GC.
    old_conn = getattr(analytics, "_conn", None)
    if old_conn is not None:
        try:
            old_conn.close()
        except Exception:
            pass
    importlib.reload(analytics)
    return analytics
