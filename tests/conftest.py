import os
os.environ["TZ"] = "Asia/Shanghai"

import pytest

import app.modules.m7_retention.store as m7_store


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """M7 每用例独立临时库，避免写入默认 data/runs.sqlite3。"""
    path = tmp_path / "m7.sqlite3"
    monkeypatch.setattr(m7_store, "DATABASE_PATH", path)
    return path

